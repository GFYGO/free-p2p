import socket
import threading
import time
import unittest

from p2punch.connection import HolePunchError, PeerConnection
from p2punch.protocol import Endpoint, derive_session_key


def make_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    return sock


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.sockets = []

    def tearDown(self):
        for sock in self.sockets:
            try:
                sock.close()
            except OSError:
                pass

    def _socket(self):
        sock = make_socket()
        self.sockets.append(sock)
        return sock

    @staticmethod
    def _endpoint(sock):
        return Endpoint("127.0.0.1", sock.getsockname()[1], 4)

    def test_punch_learns_real_endpoint(self):
        """One side advertises a useless endpoint; the other learns it from traffic."""
        sock_a = self._socket()
        sock_b = self._socket()
        blackhole = self._socket()

        key = derive_session_key("a" * 32, "b" * 32)
        conn_a = PeerConnection(sock_a, "bob", [self._endpoint(sock_b)], key, punch_interval=0.05)
        # Bob's advertised endpoint points at a black hole (simulates a
        # symmetric NAT whose STUN mapping is not valid for this peer).
        conn_b = PeerConnection(sock_b, "alice", [self._endpoint(blackhole)], key, punch_interval=0.05)

        errors = []

        def run_b():
            try:
                conn_b.connect(timeout=5)
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        thread = threading.Thread(target=run_b)
        thread.start()
        conn_a.connect(timeout=5)
        thread.join(timeout=5)

        self.assertFalse(errors, errors)
        self.assertTrue(conn_a.connected)
        self.assertTrue(conn_b.connected)
        self.assertEqual(conn_a.remote_endpoint.sockaddr, self._endpoint(sock_b).sockaddr)
        self.assertEqual(conn_b.remote_endpoint.sockaddr, self._endpoint(sock_a).sockaddr)

        conn_a.send_text("hello from alice")
        self.assertEqual(conn_b.recv(2), b"hello from alice")
        conn_b.send_text("hello from bob")
        self.assertEqual(conn_a.recv(2), b"hello from bob")

        conn_a.close()
        conn_b.close()

    def test_timeout(self):
        sock = self._socket()
        blackhole = self._socket()
        key = derive_session_key("a" * 32, "b" * 32)
        conn = PeerConnection(sock, "nobody", [self._endpoint(blackhole)], key, punch_interval=0.05)
        with self.assertRaises(HolePunchError):
            conn.connect(timeout=0.5)
        self.assertFalse(conn.connected)

    def test_send_before_connect(self):
        sock = self._socket()
        key = derive_session_key("a" * 32, "b" * 32)
        conn = PeerConnection(sock, "nobody", [self._endpoint(sock)], key)
        with self.assertRaises(ConnectionError):
            conn.send_text("nope")

    def test_bye_closes_peer(self):
        sock_a = self._socket()
        sock_b = self._socket()
        key = derive_session_key("a" * 32, "b" * 32)
        conn_a = PeerConnection(sock_a, "bob", [self._endpoint(sock_b)], key, punch_interval=0.05)
        conn_b = PeerConnection(sock_b, "alice", [self._endpoint(sock_a)], key, punch_interval=0.05)

        thread = threading.Thread(target=conn_b.connect, kwargs={"timeout": 5})
        thread.start()
        conn_a.connect(timeout=5)
        thread.join(timeout=5)
        self.assertTrue(conn_b.connected)

        conn_a.close()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and conn_b.connected:
            time.sleep(0.05)
        self.assertFalse(conn_b.connected)
        conn_b.close()


if __name__ == "__main__":
    unittest.main()
