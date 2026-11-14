"""Wire format for the UDP data plane.

Every datagram is authenticated with a truncated HMAC-SHA256 tag derived from
a session key that both peers compute from their public punch keys. The same
key also makes off-path packet injection impractical.

    +--------+------+--------+--------+---------+--------+
    | magic  | type | seq    | length | payload | tag    |
    | 4 B    | 1 B  | 4 B    | 2 B    | N B     | 16 B   |
    +--------+------+--------+--------+---------+--------+
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass
from typing import Optional

MAGIC = b"P2P1"
MAX_PAYLOAD = 1100
TAG_SIZE = 16

_HEADER = struct.Struct("!4sBIH")

TYPE_PUNCH = 1
TYPE_ACK = 2
TYPE_DATA = 3
TYPE_PING = 4
TYPE_PONG = 5
TYPE_BYE = 6

TYPE_NAMES = {
    TYPE_PUNCH: "PUNCH",
    TYPE_ACK: "ACK",
    TYPE_DATA: "DATA",
    TYPE_PING: "PING",
    TYPE_PONG: "PONG",
    TYPE_BYE: "BYE",
}


class ProtocolError(ValueError):
    """Raised when a datagram is malformed or fails authentication."""


@dataclass(frozen=True)
class Endpoint:
    """A UDP endpoint. `family` is 4 or 6."""

    host: str
    port: int
    family: int = 4

    def __post_init__(self) -> None:
        if self.family not in (4, 6):
            raise ValueError("family must be 4 or 6")
        if not (1 <= int(self.port) <= 65535):
            raise ValueError("port must be 1-65535")

    @property
    def sockaddr(self):
        if self.family == 6:
            return (self.host, self.port, 0, 0)
        return (self.host, self.port)

    def to_dict(self) -> dict:
        return {"host": self.host, "port": self.port, "family": self.family}

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["Endpoint"]:
        if not data:
            return None
        return cls(
            host=str(data["host"]),
            port=int(data["port"]),
            family=int(data.get("family", 4)),
        )

    def __str__(self) -> str:
        if self.family == 6:
            return f"[{self.host}]:{self.port}"
        return f"{self.host}:{self.port}"


def endpoint_from_sockaddr(addr, family: int) -> Endpoint:
    """Convert a socket recvfrom() address into an Endpoint."""
    return Endpoint(host=addr[0], port=addr[1], family=family)


def derive_session_key(key_a: str, key_b: str) -> bytes:
    """Derive a deterministic key from the two peers' public punch keys."""
    left, right = sorted((key_a.encode("utf-8"), key_b.encode("utf-8")))
    digest = hashlib.sha256()
    digest.update(b"p2punch/session/v1\0")
    digest.update(left)
    digest.update(b"\0")
    digest.update(right)
    return digest.digest()


def _tag(key: bytes, header: bytes, payload: bytes) -> bytes:
    return hmac.new(key, header + payload, hashlib.sha256).digest()[:TAG_SIZE]


def encode_packet(packet_type: int, seq: int, payload: bytes, key: bytes) -> bytes:
    if not (0 <= packet_type <= 255):
        raise ProtocolError("invalid packet type")
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError(f"payload too large (max {MAX_PAYLOAD} bytes)")
    header = _HEADER.pack(MAGIC, packet_type, seq & 0xFFFFFFFF, len(payload))
    return header + payload + _tag(key, header, payload)


def decode_packet(data: bytes, key: bytes):
    """Return (type, seq, payload) or raise ProtocolError."""
    if len(data) < _HEADER.size + TAG_SIZE:
        raise ProtocolError("packet too short")
    magic, packet_type, seq, length = _HEADER.unpack(data[: _HEADER.size])
    if magic != MAGIC:
        raise ProtocolError("bad magic")
    if length > MAX_PAYLOAD:
        raise ProtocolError("payload too large")
    expected = _HEADER.size + length + TAG_SIZE
    if len(data) != expected:
        raise ProtocolError("length mismatch")

    header = data[: _HEADER.size]
    payload = data[_HEADER.size : _HEADER.size + length]
    tag = data[-TAG_SIZE:]
    if not hmac.compare_digest(tag, _tag(key, header, payload)):
        raise ProtocolError("bad authentication tag")
    return packet_type, seq, payload
