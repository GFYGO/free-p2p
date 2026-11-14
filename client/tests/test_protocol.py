import unittest

from p2punch.protocol import (
    MAX_PAYLOAD,
    TYPE_DATA,
    TYPE_PUNCH,
    Endpoint,
    ProtocolError,
    decode_packet,
    derive_session_key,
    encode_packet,
)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.key = derive_session_key("a" * 32, "b" * 32)

    def test_roundtrip(self):
        packet = encode_packet(TYPE_DATA, 42, b"hello", self.key)
        packet_type, seq, payload = decode_packet(packet, self.key)
        self.assertEqual(packet_type, TYPE_DATA)
        self.assertEqual(seq, 42)
        self.assertEqual(payload, b"hello")

    def test_session_key_is_order_independent(self):
        self.assertEqual(
            derive_session_key("alice", "bob"), derive_session_key("bob", "alice")
        )
        self.assertNotEqual(
            derive_session_key("alice", "bob"), derive_session_key("alice", "carol")
        )

    def test_tampered_payload_is_rejected(self):
        packet = bytearray(encode_packet(TYPE_PUNCH, 1, b"data", self.key))
        packet[12] ^= 0xFF
        with self.assertRaises(ProtocolError):
            decode_packet(bytes(packet), self.key)

    def test_wrong_key_is_rejected(self):
        packet = encode_packet(TYPE_PUNCH, 1, b"", self.key)
        with self.assertRaises(ProtocolError):
            decode_packet(packet, derive_session_key("x", "y"))

    def test_oversized_payload_is_rejected(self):
        with self.assertRaises(ProtocolError):
            encode_packet(TYPE_DATA, 1, b"x" * (MAX_PAYLOAD + 1), self.key)

    def test_truncated_packet_is_rejected(self):
        packet = encode_packet(TYPE_PUNCH, 1, b"abc", self.key)
        with self.assertRaises(ProtocolError):
            decode_packet(packet[:-1], self.key)

    def test_endpoint_serialization(self):
        endpoint = Endpoint("2001:db8::1", 443, 6)
        self.assertEqual(Endpoint.from_dict(endpoint.to_dict()), endpoint)
        self.assertEqual(str(endpoint), "[2001:db8::1]:443")
        self.assertIsNone(Endpoint.from_dict(None))


if __name__ == "__main__":
    unittest.main()
