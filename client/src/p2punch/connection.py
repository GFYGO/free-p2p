"""UDP hole punching and the authenticated data channel."""

from __future__ import annotations

import queue
import socket
import threading
import time
from typing import Iterable, List, Optional, Sequence

from .protocol import (
    MAX_PAYLOAD,
    TYPE_ACK,
    TYPE_BYE,
    TYPE_DATA,
    TYPE_PING,
    TYPE_PONG,
    TYPE_PUNCH,
    Endpoint,
    ProtocolError,
    decode_packet,
    encode_packet,
)

KEEPALIVE_SECONDS = 10.0
DEAD_AFTER_SECONDS = 45.0


class HolePunchError(RuntimeError):
    """Raised when no direct path could be established."""


class PeerConnection:
    """One UDP session with one peer.

    Both peers send PUNCH packets to every advertised candidate endpoint. Any
    authenticated packet that arrives also teaches us the peer's *actual*
    source address, which is what makes punching work when the peer sits
    behind a symmetric NAT (its advertised STUN port is useless, but the port
    it uses towards us is learned from its first packet).
    """

    def __init__(
        self,
        sock: socket.socket,
        peer_id: str,
        targets: Sequence[Endpoint],
        session_key: bytes,
        *,
        punch_interval: float = 0.25,
    ) -> None:
        if not targets:
            raise ValueError("at least one target endpoint is required")
        self.sock = sock
        self.peer_id = peer_id
        self.session_key = session_key
        self.punch_interval = punch_interval
        self.targets: List[Endpoint] = _dedupe(targets)

        self._remote: Optional[Endpoint] = None
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        self._inbox: "queue.Queue[bytes]" = queue.Queue()
        self._lock = threading.Lock()
        self._seq = 0
        self._last_recv = 0.0
        self._last_ping = 0.0
        self.last_error: Optional[str] = None

    # ---- public API ----

    @property
    def connected(self) -> bool:
        return self._connected.is_set() and not self._closed

    @property
    def remote_endpoint(self) -> Optional[Endpoint]:
        with self._lock:
            return self._remote

    def connect(self, timeout: float = 20.0) -> None:
        if self._thread is not None:
            raise RuntimeError("connect() may only be called once")
        self.sock.settimeout(0.2)
        self._last_recv = time.monotonic()
        self._thread = threading.Thread(
            target=self._recv_loop, name=f"p2punch-{self.peer_id}", daemon=True
        )
        self._thread.start()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._connected.is_set():
                break
            for target in self.targets:
                self._send_packet(TYPE_PUNCH, b"", target)
            self._connected.wait(self.punch_interval)

        if not self._connected.is_set():
            targets = ", ".join(str(t) for t in self.targets)
            self.close(send_bye=False)
            raise HolePunchError(
                f"hole punching to {self.peer_id} timed out after {timeout:.0f}s "
                f"(tried {targets})"
            )

        self._last_recv = time.monotonic()
        self._last_ping = time.monotonic()

    def send(self, data) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        if len(data) > MAX_PAYLOAD:
            raise ValueError(
                f"message is {len(data)} bytes; UDP datagram payload limit is {MAX_PAYLOAD}"
            )
        if not self.connected:
            raise ConnectionError("not connected")
        self._send_packet(TYPE_DATA, data)

    def send_text(self, text: str) -> None:
        self.send(text.encode("utf-8"))

    def recv(self, timeout: Optional[float] = None) -> Optional[bytes]:
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self, *, send_bye: bool = True) -> None:
        if send_bye and self.connected:
            self._send_packet(TYPE_BYE, b"")
        self._closed = True
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    # ---- internals ----

    def _send_packet(self, packet_type: int, payload: bytes, target: Optional[Endpoint] = None) -> None:
        with self._lock:
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            seq = self._seq
            dest = target if target is not None else self._remote
        if dest is None:
            return
        try:
            packet = encode_packet(packet_type, seq, payload, self.session_key)
            self.sock.sendto(packet, dest.sockaddr)
        except (OSError, ProtocolError):
            pass

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                self._tick()
                continue
            except OSError:
                break

            try:
                packet_type, _seq, payload = decode_packet(data, self.session_key)
            except ProtocolError:
                continue

            family = 6 if len(addr) == 4 else 4
            source = Endpoint(addr[0], addr[1], family)
            now = time.monotonic()
            with self._lock:
                self._last_recv = now
                # Learn the real source endpoint. A symmetric NAT gives the
                # peer a different port towards us than the one it advertised.
                if self._remote is None or self._remote.sockaddr != source.sockaddr:
                    self._remote = source

            if packet_type == TYPE_PUNCH:
                self._connected.set()
                self._send_packet(TYPE_ACK, b"", source)
            elif packet_type == TYPE_ACK:
                self._connected.set()
            elif packet_type == TYPE_DATA:
                self._connected.set()
                self._inbox.put(payload)
            elif packet_type == TYPE_PING:
                self._send_packet(TYPE_PONG, b"", source)
            elif packet_type == TYPE_BYE:
                self._closed = True
                self._stop.set()

            self._tick(now)

    def _tick(self, now: Optional[float] = None) -> None:
        if not self._connected.is_set() or self._closed:
            return
        now = now if now is not None else time.monotonic()
        if now - self._last_ping >= KEEPALIVE_SECONDS:
            self._last_ping = now
            self._send_packet(TYPE_PING, b"")
        if now - self._last_recv > DEAD_AFTER_SECONDS:
            self._closed = True
            self.last_error = "peer stopped responding"
            self._stop.set()


def _dedupe(endpoints: Iterable[Endpoint]) -> List[Endpoint]:
    seen = set()
    result = []
    for endpoint in endpoints:
        key = (endpoint.host, endpoint.port, endpoint.family)
        if key not in seen:
            seen.add(key)
            result.append(endpoint)
    return result
