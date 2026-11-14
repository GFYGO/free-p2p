"""Minimal STUN client (RFC 5389) and NAT-behaviour detection.

Only the parts needed for hole punching are implemented:

* Binding requests / responses
* XOR-MAPPED-ADDRESS and MAPPED-ADDRESS
* CHANGE-REQUEST (RFC 5780), used opportunistically to detect full-cone NAT

A simple but effective NAT classification:

* same public mapping towards several different STUN servers -> cone NAT
  (endpoint-independent mapping)
* different public mappings -> symmetric NAT (endpoint-dependent mapping)

For "cone" we additionally send a CHANGE-REQUEST; if the reply arrives from a
different IP/port the NAT is full-cone, otherwise it is restricted /
port-restricted (we cannot distinguish those without a cooperating second
host).
"""

from __future__ import annotations

import os
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .protocol import Endpoint, endpoint_from_sockaddr

STUN_MAGIC_COOKIE = 0x2112A442
BINDING_REQUEST = 0x0001
BINDING_SUCCESS = 0x0101

ATTR_MAPPED_ADDRESS = 0x0001
ATTR_CHANGE_REQUEST = 0x0003
ATTR_XOR_MAPPED_ADDRESS = 0x0020

CHANGE_IP = 0x04
CHANGE_PORT = 0x02

DEFAULT_STUN_SERVERS: Tuple[Tuple[str, int], ...] = (
    ("stun.cloudflare.com", 3478),
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
)


@dataclass
class StunMapping:
    endpoint: Endpoint
    source: Endpoint
    server: str
    server_port: int
    rtt_ms: float


@dataclass
class NatProfile:
    public_endpoint: Optional[Endpoint]
    nat_type: str
    detail: str
    mappings: List[StunMapping] = field(default_factory=list)

    def summary(self) -> str:
        if not self.public_endpoint:
            return f"unknown ({self.detail})"
        return f"{self.public_endpoint} ({self.nat_type}; {self.detail})"


def parse_server(value: str) -> Tuple[str, int]:
    """Parse `host`, `host:port` or `[v6]:port`."""
    value = value.strip()
    if not value:
        raise ValueError("empty STUN server")
    if value.startswith("["):
        host, sep, rest = value[1:].partition("]")
        if not sep:
            raise ValueError(f"bad STUN server: {value}")
        return host, int(rest[1:]) if rest.startswith(":") else 3478
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        return host, int(port)
    return value, 3478


def query(
    sock: socket.socket,
    server: Tuple[str, int],
    *,
    timeout: float = 1.2,
    change_flags: int = 0,
    retries: int = 2,
) -> Optional[StunMapping]:
    """Send one binding request and return the mapped endpoint, if any."""
    host, port = server
    try:
        infos = socket.getaddrinfo(host, port, sock.family, socket.SOCK_DGRAM)
    except OSError:
        return None
    if not infos:
        return None
    server_addr = infos[0][4]

    transaction_id = os.urandom(12)
    attrs = b""
    if change_flags:
        attrs = struct.pack("!HHI", ATTR_CHANGE_REQUEST, 4, change_flags)
    header = struct.pack(
        "!HHI12s", BINDING_REQUEST, len(attrs), STUN_MAGIC_COOKIE, transaction_id
    )
    packet = header + attrs

    old_timeout = sock.gettimeout()
    try:
        for _ in range(max(1, retries)):
            started = time.monotonic()
            try:
                sock.sendto(packet, server_addr)
            except OSError:
                return None
            deadline = started + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(min(0.2, remaining))
                try:
                    data, src = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    return None
                if len(data) < 20:
                    continue
                msg_type, msg_len, cookie, response_tid = struct.unpack("!HHI12s", data[:20])
                if cookie != STUN_MAGIC_COOKIE or response_tid != transaction_id:
                    continue
                if msg_type != BINDING_SUCCESS:
                    continue
                mapped = _parse_mapped(data[20 : 20 + msg_len], transaction_id)
                if mapped is None:
                    continue
                family = 6 if sock.family == socket.AF_INET6 else 4
                return StunMapping(
                    endpoint=mapped,
                    source=endpoint_from_sockaddr(src, family),
                    server=host,
                    server_port=port,
                    rtt_ms=(time.monotonic() - started) * 1000.0,
                )
    finally:
        sock.settimeout(old_timeout)
    return None


def detect_nat(
    sock: socket.socket,
    servers: Sequence[Tuple[str, int]] = DEFAULT_STUN_SERVERS,
    *,
    timeout: float = 1.2,
) -> NatProfile:
    """Classify the NAT in front of `sock`. Keeps the socket bound."""
    mappings: List[StunMapping] = []
    for server in servers:
        mapping = query(sock, server, timeout=timeout, retries=1)
        if mapping:
            mappings.append(mapping)

    if not mappings:
        return NatProfile(
            public_endpoint=None,
            nat_type="unknown",
            detail="no STUN server responded",
            mappings=[],
        )

    public = mappings[0].endpoint
    distinct = {(m.endpoint.host, m.endpoint.port) for m in mappings}

    if len(distinct) > 1:
        return NatProfile(
            public_endpoint=public,
            nat_type="symmetric",
            detail="public mapping changes per destination",
            mappings=mappings,
        )

    first = mappings[0]
    changed = query(
        sock,
        (first.server, first.server_port),
        timeout=timeout * 1.5,
        change_flags=CHANGE_IP | CHANGE_PORT,
        retries=1,
    )
    if changed and (changed.source.host, changed.source.port) != (
        first.source.host,
        first.source.port,
    ):
        return NatProfile(
            public_endpoint=public,
            nat_type="full-cone",
            detail="endpoint-independent mapping and filtering",
            mappings=mappings + [changed],
        )

    return NatProfile(
        public_endpoint=public,
        nat_type="cone",
        detail="endpoint-independent mapping (restricted filtering)",
        mappings=mappings,
    )


def detect_local_ip(family: int = 4) -> str:
    """Best-effort LAN address of this host (no packets are sent)."""
    af = socket.AF_INET6 if family == 6 else socket.AF_INET
    target = ("2001:4860:4860::8888", 80) if family == 6 else ("8.8.8.8", 80)
    probe = socket.socket(af, socket.SOCK_DGRAM)
    try:
        probe.connect(target)
        return probe.getsockname()[0]
    except OSError:
        return "::1" if family == 6 else "127.0.0.1"
    finally:
        probe.close()


def _parse_mapped(body: bytes, transaction_id: bytes) -> Optional[Endpoint]:
    offset = 0
    fallback: Optional[Endpoint] = None
    while offset + 4 <= len(body):
        attr_type, attr_len = struct.unpack("!HH", body[offset : offset + 4])
        offset += 4
        value = body[offset : offset + attr_len]
        offset += (attr_len + 3) & ~3
        if attr_type == ATTR_XOR_MAPPED_ADDRESS:
            mapped = _parse_xor_mapped(value, transaction_id)
            if mapped:
                return mapped
        elif attr_type == ATTR_MAPPED_ADDRESS and fallback is None:
            fallback = _parse_mapped_address(value)
    return fallback


def _parse_xor_mapped(value: bytes, transaction_id: bytes) -> Optional[Endpoint]:
    if len(value) < 4:
        return None
    family = value[1]
    port = struct.unpack("!H", value[2:4])[0] ^ (STUN_MAGIC_COOKIE >> 16)
    if family == 0x01 and len(value) >= 8:
        raw = struct.unpack("!I", value[4:8])[0] ^ STUN_MAGIC_COOKIE
        host = socket.inet_ntoa(struct.pack("!I", raw))
        return Endpoint(host, port, 4)
    if family == 0x02 and len(value) >= 20:
        key = struct.pack("!I", STUN_MAGIC_COOKIE) + transaction_id
        raw = bytes(a ^ b for a, b in zip(value[4:20], key))
        host = socket.inet_ntop(socket.AF_INET6, raw)
        return Endpoint(host, port, 6)
    return None


def _parse_mapped_address(value: bytes) -> Optional[Endpoint]:
    if len(value) < 4:
        return None
    family = value[1]
    port = struct.unpack("!H", value[2:4])[0]
    if family == 0x01 and len(value) >= 8:
        return Endpoint(socket.inet_ntoa(value[4:8]), port, 4)
    if family == 0x02 and len(value) >= 20:
        return Endpoint(socket.inet_ntop(socket.AF_INET6, value[4:20]), port, 6)
    return None
