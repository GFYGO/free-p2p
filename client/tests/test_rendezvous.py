import json
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from p2punch.protocol import Endpoint
from p2punch.rendezvous import RendezvousClient, RendezvousError, whoami


class _State:
    peers = {}
    lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence test output
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self):
        if self.path == "/api/announce":
            body = self._read()
            peer = {
                "peerId": body["peerId"],
                "punchKey": body["punchKey"],
                "natType": body["natType"],
                "publicEndpoint": body["publicEndpoint"],
                "localEndpoint": body["localEndpoint"],
                "observedIp": "203.0.113.9",
                "lastSeen": int(time.time() * 1000),
                "online": True,
                "metadata": body.get("metadata"),
            }
            with _State.lock:
                _State.peers[body["peerId"]] = (body["secret"], peer)
                peers = [
                    item
                    for peer_id, (_secret, item) in _State.peers.items()
                    if peer_id != body["peerId"]
                ]
            self._send(
                200,
                {
                    "ok": True,
                    "room": body["room"],
                    "self": peer,
                    "peers": peers,
                    "ttlSeconds": 45,
                    "heartbeatSeconds": 15,
                    "serverTime": int(time.time() * 1000),
                },
            )
            return

        if self.path == "/api/leave":
            body = self._read()
            removed = False
            with _State.lock:
                item = _State.peers.get(body["peerId"])
                if item and item[0] == body["secret"]:
                    del _State.peers[body["peerId"]]
                    removed = True
            self._send(200, {"ok": True, "removed": removed})
            return

        self._send(404, {"ok": False, "error": "not found"})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/whoami":
            self._send(200, {"ok": True, "ip": "203.0.113.9"})
            return
        if parsed.path == "/api/peers":
            peer_id = self.headers.get("X-Peer-Id")
            secret = self.headers.get("X-Peer-Secret")
            with _State.lock:
                item = _State.peers.get(peer_id)
                if not item or item[0] != secret:
                    self._send(403, {"ok": False, "error": "bad secret"})
                    return
                peers = [
                    view
                    for other_id, (_secret, view) in _State.peers.items()
                    if other_id != peer_id
                ]
            self._send(
                200,
                {
                    "ok": True,
                    "room": parsed.query,
                    "peers": peers,
                    "ttlSeconds": 45,
                    "serverTime": int(time.time() * 1000),
                },
            )
            return
        self._send(404, {"ok": False, "error": "not found"})


class RendezvousTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        with _State.lock:
            _State.peers = {}

    def test_announce_discover_and_leave(self):
        alice = RendezvousClient(self.url, "room1", "alice", "a" * 64, timeout=2)
        bob = RendezvousClient(self.url, "room1", "bob", "b" * 64, timeout=2)

        first = alice.announce(
            punch_key="k" * 32,
            public_endpoint=Endpoint("203.0.113.1", 5000),
            local_endpoint=Endpoint("192.168.1.2", 5000),
            nat_type="cone",
        )
        self.assertEqual(first["peers"], [])

        second = bob.announce(
            punch_key="m" * 32,
            public_endpoint=Endpoint("203.0.113.2", 6000),
            local_endpoint=Endpoint("192.168.1.3", 6000),
            nat_type="symmetric",
        )
        self.assertEqual([peer["peerId"] for peer in second["peers"]], ["alice"])

        listing = alice.peers()
        self.assertEqual([peer["peerId"] for peer in listing["peers"]], ["bob"])
        self.assertEqual(listing["peers"][0]["publicEndpoint"]["port"], 6000)
        self.assertEqual(listing["peers"][0]["natType"], "symmetric")

        self.assertEqual(whoami(self.url)["ip"], "203.0.113.9")

        bob.leave()
        self.assertEqual(alice.peers()["peers"], [])

    def test_bad_secret_is_rejected(self):
        client = RendezvousClient(self.url, "room1", "alice", "a" * 64, timeout=2)
        client.announce(
            punch_key="k" * 32,
            public_endpoint=None,
            local_endpoint=None,
            nat_type="unknown",
        )
        impostor = RendezvousClient(self.url, "room1", "alice", "z" * 64, timeout=2)
        with self.assertRaises(RendezvousError) as context:
            impostor.peers()
        self.assertEqual(context.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
