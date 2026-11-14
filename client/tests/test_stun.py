import socket
import struct
import threading
import unittest

from p2punch.stun import (
    STUN_MAGIC_COOKIE,
    _parse_xor_mapped,
    detect_nat,
    parse_server,
    query,
)


class FakeStunServer(threading.Thread):
    """UDP STUN responder that reports a configurable mapped port."""

    def __init__(self, mapped_port=None):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.mapped_port = mapped_port
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 20:
                continue
            message_type, _length, cookie, transaction_id = struct.unpack(
                "!HHI12s", data[:20]
            )
            if message_type != 0x0001 or cookie != STUN_MAGIC_COOKIE:
                continue
            mapped_port = self.mapped_port or addr[1]
            xport = mapped_port ^ (STUN_MAGIC_COOKIE >> 16)
            xaddr = (
                struct.unpack("!I", socket.inet_aton(addr[0]))[0] ^ STUN_MAGIC_COOKIE
            )
            attr = struct.pack("!HHBBH", 0x0020, 8, 0, 1, xport) + struct.pack(
                "!I", xaddr
            )
            response = (
                struct.pack("!HHI12s", 0x0101, len(attr), STUN_MAGIC_COOKIE, transaction_id)
                + attr
            )
            self.sock.sendto(response, addr)

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        finally:
            self.join(timeout=1.0)


class StunTests(unittest.TestCase):
    def setUp(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.servers = []

    def tearDown(self):
        self.sock.close()
        for server in self.servers:
            server.close()

    def _server(self, mapped_port=None):
        server = FakeStunServer(mapped_port=mapped_port)
        server.start()
        self.servers.append(server)
        return server

    def test_query_returns_mapped_endpoint(self):
        server = self._server()
        mapping = query(self.sock, ("127.0.0.1", server.port), timeout=0.5, retries=2)
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.endpoint.port, self.sock.getsockname()[1])
        self.assertEqual(mapping.endpoint.host, "127.0.0.1")

    def test_cone_nat(self):
        first = self._server()
        second = self._server()
        profile = detect_nat(
            self.sock,
            [("127.0.0.1", first.port), ("127.0.0.1", second.port)],
            timeout=0.5,
        )
        self.assertEqual(profile.nat_type, "cone")
        self.assertEqual(profile.public_endpoint.port, self.sock.getsockname()[1])

    def test_symmetric_nat(self):
        first = self._server(mapped_port=11111)
        second = self._server(mapped_port=22222)
        profile = detect_nat(
            self.sock,
            [("127.0.0.1", first.port), ("127.0.0.1", second.port)],
            timeout=0.5,
        )
        self.assertEqual(profile.nat_type, "symmetric")
        self.assertIn("changes per destination", profile.detail)

    def test_no_stun_response(self):
        profile = detect_nat(self.sock, [("127.0.0.1", 9)], timeout=0.2)
        self.assertEqual(profile.nat_type, "unknown")
        self.assertIsNone(profile.public_endpoint)

    def test_ipv6_xor_mapped_parsing(self):
        transaction_id = b"\x01" * 12
        host = "2001:db8::1"
        port = 12345
        raw = socket.inet_pton(socket.AF_INET6, host)
        key = struct.pack("!I", STUN_MAGIC_COOKIE) + transaction_id
        xaddr = bytes(a ^ b for a, b in zip(raw, key))
        xport = port ^ (STUN_MAGIC_COOKIE >> 16)
        value = struct.pack("!BBH", 0, 2, xport) + xaddr
        endpoint = _parse_xor_mapped(value, transaction_id)
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.host, host)
        self.assertEqual(endpoint.port, port)
        self.assertEqual(endpoint.family, 6)

    def test_parse_server(self):
        self.assertEqual(parse_server("stun.example.com"), ("stun.example.com", 3478))
        self.assertEqual(parse_server("stun.example.com:19302"), ("stun.example.com", 19302))
        self.assertEqual(parse_server("[2001:db8::1]:3478"), ("2001:db8::1", 3478))


if __name__ == "__main__":
    unittest.main()
