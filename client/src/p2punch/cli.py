"""Command line interface: `p2punch ip`, `p2punch peers`, `p2punch run`."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from .connection import HolePunchError, PeerConnection
from .protocol import Endpoint, derive_session_key
from .rendezvous import RendezvousClient, RendezvousError, whoami
from .stun import DEFAULT_STUN_SERVERS, detect_local_ip, detect_nat, parse_server

DEFAULT_SERVER = os.environ.get("P2P_SERVER", "http://127.0.0.1:8787")
DEFAULT_ROOM = os.environ.get("P2P_ROOM", "demo")

_print_lock = threading.Lock()


def log(message: str) -> None:
    with _print_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def say(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


# ---- local state -----------------------------------------------------------


def _state_file() -> Path:
    return Path(os.environ.get("P2PUNCH_STATE", "~/.p2punch/state.json")).expanduser()


def load_secret(server: str, room: str, peer_id: str) -> str:
    """Persist a per-(server, room, peer) auth secret with 0600 permissions."""
    path = _state_file()
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    secrets_map = data.get("secrets")
    if not isinstance(secrets_map, dict):
        secrets_map = {}
        data["secrets"] = secrets_map

    key = f"{server.rstrip('/')}|{room}|{peer_id}"
    secret = secrets_map.get(key)
    if not secret:
        secret = secrets.token_hex(32)
        secrets_map[key] = secret
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(prefix=".p2punch-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    else:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return secret


# ---- sockets / identity ----------------------------------------------------


def make_socket(family: int, bind_host: str, port: int) -> socket.socket:
    af = socket.AF_INET6 if family == 6 else socket.AF_INET
    sock = socket.socket(af, socket.SOCK_DGRAM)
    if family == 6 and bind_host in ("0.0.0.0", ""):
        bind_host = "::"
    address = (bind_host, port, 0, 0) if family == 6 else (bind_host, port)
    sock.bind(address)
    sock.settimeout(0.5)
    return sock


def _stun_servers(args) -> List:
    if args.stun_server:
        return [parse_server(value) for value in args.stun_server]
    return list(DEFAULT_STUN_SERVERS)


def prepare_identity(args):
    """Bind the UDP socket and work out the public/LAN endpoints and NAT type."""
    sock = make_socket(args.family, args.bind, args.port)
    port = sock.getsockname()[1]

    if args.no_stun:
        host = args.bind
        if host in ("0.0.0.0", "::", ""):
            host = detect_local_ip(args.family)
        endpoint = Endpoint(host, port, args.family)
        return sock, endpoint, endpoint, "local", None

    profile = detect_nat(sock, _stun_servers(args), timeout=args.stun_timeout)
    sock.settimeout(0.5)
    local = Endpoint(detect_local_ip(args.family), port, args.family)
    public = profile.public_endpoint or local
    if profile.public_endpoint is None:
        log("warning: STUN failed; registering the LAN endpoint (WAN punching may fail)")
    return sock, public, local, profile.nat_type, profile


# ---- peer table ------------------------------------------------------------


class PeerTable:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._peers: Dict[str, dict] = {}

    def update(self, peers: List[dict]):
        with self._lock:
            new = {p["peerId"]: p for p in peers if isinstance(p, dict) and p.get("peerId")}
            added = set(new) - set(self._peers)
            removed = set(self._peers) - set(new)
            self._peers = new
        return added, removed

    def get(self, peer_id: str) -> Optional[dict]:
        with self._lock:
            return self._peers.get(peer_id)

    def all(self) -> List[dict]:
        with self._lock:
            return list(self._peers.values())


def format_endpoint(value) -> str:
    endpoint = Endpoint.from_dict(value) if value else None
    return str(endpoint) if endpoint else "-"


def print_peers(peers: List[dict]) -> None:
    if not peers:
        say("  (no other peers online)")
        return
    say(f"  {'PEER':<20} {'NAT':<10} {'OBSERVED IP':<16} {'PUBLIC UDP':<24} LAN")
    for peer in sorted(peers, key=lambda item: item.get("peerId", "")):
        say(
            f"  {peer.get('peerId', ''):<20} "
            f"{peer.get('natType', '?'):<10} "
            f"{peer.get('observedIp', '?'):<16} "
            f"{format_endpoint(peer.get('publicEndpoint')):<24} "
            f"{format_endpoint(peer.get('localEndpoint'))}"
        )


# ---- commands --------------------------------------------------------------


def cmd_ip(args) -> int:
    sock = make_socket(args.family, args.bind, args.port)
    try:
        try:
            info = whoami(args.server, room_secret=args.room_secret)
            log(
                "observed HTTP IP: {} (colo {}, country {})".format(
                    info.get("ip"), info.get("colo"), info.get("country")
                )
            )
        except RendezvousError as exc:
            log(f"whoami failed: {exc}")

        if args.no_stun:
            log("STUN disabled (--no-stun)")
            return 0

        profile = detect_nat(sock, _stun_servers(args), timeout=args.stun_timeout)
        if profile.public_endpoint is None:
            log(f"STUN failed: {profile.detail}")
            return 1

        log(f"public UDP endpoint: {profile.public_endpoint}")
        log(f"NAT type: {profile.nat_type} ({profile.detail})")
        for mapping in profile.mappings:
            say(
                f"  {mapping.server}:{mapping.server_port} -> {mapping.endpoint}"
                f"  ({mapping.rtt_ms:.0f} ms)"
            )
        if profile.nat_type == "symmetric":
            say("  note: symmetric NAT usually requires a relay (TURN) unless the peer is cone NAT")
        return 0
    finally:
        sock.close()


def cmd_peers(args) -> int:
    secret = load_secret(args.server, args.room, args.id)
    sock, public, local, nat_type, _profile = prepare_identity(args)
    client = RendezvousClient(
        args.server, args.room, args.id, secret, room_secret=args.room_secret
    )
    try:
        response = client.announce(
            punch_key=secrets.token_hex(16),
            public_endpoint=public,
            local_endpoint=local,
            nat_type=nat_type,
        )
        self_view = response.get("self") or {}
        say(
            f"room={args.room} peer={args.id} observed_ip={self_view.get('observedIp')} "
            f"public={public} nat={nat_type}"
        )
        print_peers(response.get("peers") or [])
        return 0
    finally:
        try:
            client.leave()
        except RendezvousError:
            pass
        sock.close()


def cmd_run(args) -> int:
    secret = load_secret(args.server, args.room, args.id)
    punch_key = secrets.token_hex(16)
    sock, public, local, nat_type, _profile = prepare_identity(args)
    client = RendezvousClient(
        args.server, args.room, args.id, secret, room_secret=args.room_secret
    )
    table = PeerTable()
    stop = threading.Event()
    session = ChatSession()
    heartbeat: Optional[threading.Thread] = None

    try:
        response = client.announce(
            punch_key=punch_key,
            public_endpoint=public,
            local_endpoint=local,
            nat_type=nat_type,
        )
        self_view = response.get("self") or {}
        say(f"registered peer={args.id} room={args.room}")
        say(f"  observed IP : {self_view.get('observedIp', '?')}")
        say(f"  public UDP  : {public}  nat={nat_type}")
        say(f"  local UDP   : {local}")
        added, _removed = table.update(response.get("peers") or [])
        for peer_id in sorted(added):
            log(f"peer online: {peer_id}")
        print_peers(table.all())

        interval = max(3, int(response.get("heartbeatSeconds") or 15))
        heartbeat = threading.Thread(
            target=_heartbeat_loop,
            args=(client, table, punch_key, public, local, nat_type, interval, stop),
            name="p2punch-heartbeat",
            daemon=True,
        )
        heartbeat.start()

        if args.peer:
            view = _wait_for_peer(args.peer, client, table, stop, args.wait_peer)
            if not view:
                return 1
            if not _open_connection(session, view, sock, args, punch_key):
                return 1
            for message in args.send:
                session.send(message)
            if _is_interactive(args):
                _interactive_loop(session, table, sock, args, punch_key, stop)
            else:
                _wait_and_exit(args, stop, default=10.0)
        elif args.send:
            log("--send requires --peer")
            return 2
        elif _is_interactive(args):
            _interactive_loop(session, table, sock, args, punch_key, stop)
        else:
            _wait_and_exit(args, stop, default=30.0)
        return 0
    except (RendezvousError, HolePunchError) as exc:
        log(f"error: {exc}")
        return 1
    finally:
        stop.set()
        session.close()
        if heartbeat is not None:
            heartbeat.join(timeout=2.0)
        try:
            client.leave()
        except RendezvousError:
            pass
        sock.close()


# ---- run helpers -----------------------------------------------------------


def _heartbeat_loop(
    client: RendezvousClient,
    table: PeerTable,
    punch_key: str,
    public: Endpoint,
    local: Endpoint,
    nat_type: str,
    interval: int,
    stop: threading.Event,
) -> None:
    while not stop.wait(interval):
        try:
            response = client.announce(
                punch_key=punch_key,
                public_endpoint=public,
                local_endpoint=local,
                nat_type=nat_type,
            )
        except RendezvousError as exc:
            log(f"heartbeat failed: {exc}")
            continue
        added, removed = table.update(response.get("peers") or [])
        for peer_id in sorted(added):
            log(f"peer online: {peer_id}")
        for peer_id in sorted(removed):
            log(f"peer offline: {peer_id}")


def _wait_for_peer(
    peer_id: str,
    client: RendezvousClient,
    table: PeerTable,
    stop: threading.Event,
    timeout: float,
) -> Optional[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not stop.is_set():
        view = table.get(peer_id)
        if view:
            return view
        try:
            response = client.peers()
            table.update(response.get("peers") or [])
        except RendezvousError as exc:
            log(f"peer lookup failed: {exc}")
        view = table.get(peer_id)
        if view:
            return view
        if stop.wait(1.0):
            break
    log(f"peer {peer_id} did not appear within {timeout:.0f}s")
    return None


def _open_connection(session: "ChatSession", view: dict, sock, args, punch_key: str) -> bool:
    peer_id = view.get("peerId", "?")
    punch_key_peer = view.get("punchKey")
    if not punch_key_peer:
        log(f"peer {peer_id} did not publish a punch key")
        return False

    targets: List[Endpoint] = []
    for field in ("publicEndpoint", "localEndpoint"):
        endpoint = Endpoint.from_dict(view.get(field))
        if endpoint and endpoint.host not in ("0.0.0.0", "::"):
            targets.append(endpoint)
    if not targets:
        log(f"peer {peer_id} has no usable UDP endpoint")
        return False

    # Stop the old receive thread first: two PeerConnection objects must never
    # read from the same socket at the same time.
    session.close()

    key = derive_session_key(punch_key, str(punch_key_peer))
    log(
        f"punching {peer_id} (nat={view.get('natType', '?')}) via "
        + ", ".join(str(target) for target in targets)
    )
    connection = PeerConnection(
        sock, peer_id, targets, key, punch_interval=args.punch_interval
    )
    try:
        connection.connect(timeout=args.punch_timeout)
    except HolePunchError as exc:
        log(f"punch failed: {exc}")
        if view.get("natType") == "symmetric":
            log(
                "hint: this peer is behind symmetric NAT. If both sides are symmetric, "
                "direct punching cannot work; use TURN/relay or IPv6."
            )
        else:
            log("hint: check that both sides allow outbound/inbound UDP and that STUN succeeded.")
        return False

    log(f"connected to {peer_id} at {connection.remote_endpoint}")
    session.open(peer_id, connection)
    return True


def _is_interactive(args) -> bool:
    return sys.stdin.isatty() and not args.no_stdin


def _wait_and_exit(args, stop: threading.Event, default: float) -> None:
    seconds = args.wait if args.wait is not None else default
    if seconds > 0:
        log(f"waiting {seconds:.0f}s (Ctrl-C to quit)")
        stop.wait(seconds)


def _interactive_loop(session, table, sock, args, punch_key, stop) -> None:
    say("commands: /peers  /connect <id>  /disconnect  /whoami  /quit")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if stop.is_set():
            break
        if not line.startswith("/"):
            session.send(line)
            continue

        command, _, rest = line.partition(" ")
        command = command.lower()
        if command in ("/quit", "/exit"):
            break
        if command == "/peers":
            print_peers(table.all())
        elif command == "/whoami":
            say(
                f"peer={args.id} room={args.room} "
                f"udp={sock.getsockname()[0]}:{sock.getsockname()[1]} "
                f"server={args.server}"
            )
        elif command == "/disconnect":
            session.close()
            log("disconnected")
        elif command == "/connect":
            peer_id = rest.strip()
            if not peer_id:
                log("usage: /connect <peer-id>")
                continue
            view = table.get(peer_id)
            if not view:
                log(f"peer {peer_id} is not online; use /peers")
                continue
            _open_connection(session, view, sock, args, punch_key)
        else:
            log(f"unknown command: {command}")


class ChatSession:
    """Owns the current PeerConnection and its receive thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connection: Optional[PeerConnection] = None
        self._peer_id: Optional[str] = None
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None

    def open(self, peer_id: str, connection: PeerConnection) -> None:
        self.close()
        stop = threading.Event()
        with self._lock:
            self._connection = connection
            self._peer_id = peer_id
            self._stop = stop
            self._thread = threading.Thread(
                target=self._pump,
                args=(connection, peer_id, stop),
                name=f"p2punch-chat-{peer_id}",
                daemon=True,
            )
            self._thread.start()

    def _pump(self, connection: PeerConnection, peer_id: str, stop: threading.Event) -> None:
        while not stop.is_set():
            data = connection.recv(0.5)
            if data:
                say(f"<{peer_id}> {data.decode('utf-8', 'replace')}")
            elif not connection.connected:
                if connection.last_error:
                    log(f"connection to {peer_id} closed: {connection.last_error}")
                break

    def send(self, text: str) -> bool:
        with self._lock:
            connection = self._connection
        if connection is None or not connection.connected:
            log("not connected")
            return False
        try:
            connection.send_text(text)
            return True
        except (ConnectionError, ValueError) as exc:
            log(f"send failed: {exc}")
            return False

    def close(self) -> None:
        with self._lock:
            connection = self._connection
            thread = self._thread
            stop = self._stop
            self._connection = None
            self._thread = None
            self._stop = None
            self._peer_id = None
        if stop:
            stop.set()
        if connection:
            connection.close()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)


# ---- argument parsing ------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", default=DEFAULT_SERVER, help="rendezvous Worker URL")
    common.add_argument("--room", default=DEFAULT_ROOM, help="room code (acts as a shared secret)")
    common.add_argument(
        "--id",
        default=os.environ.get("P2P_ID") or f"peer-{secrets.token_hex(3)}",
        help="this peer's id",
    )
    common.add_argument(
        "--room-secret",
        default=os.environ.get("P2P_ROOM_SECRET"),
        help="X-Room-Secret value, if the Worker has ROOM_SECRET configured",
    )
    common.add_argument("--family", type=int, choices=(4, 6), default=4, help="IP family")
    common.add_argument("--bind", default="0.0.0.0", help="local UDP bind address")
    common.add_argument("--port", type=int, default=0, help="local UDP port (0 = ephemeral)")
    common.add_argument("--no-stun", action="store_true", help="skip STUN (LAN/loopback testing)")
    common.add_argument(
        "--stun-server",
        action="append",
        metavar="HOST:PORT",
        help="override the STUN server list (repeatable)",
    )
    common.add_argument("--stun-timeout", type=float, default=1.2, help="per-server STUN timeout")

    parser = argparse.ArgumentParser(
        prog="p2punch",
        description="STUN NAT detection + UDP hole punching with a Cloudflare Worker rendezvous",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ip_parser = subparsers.add_parser(
        "ip", parents=[common], help="show observed IP, public UDP endpoint and NAT type"
    )
    ip_parser.set_defaults(func=cmd_ip)

    peers_parser = subparsers.add_parser(
        "peers", parents=[common], help="register briefly and list online peers"
    )
    peers_parser.set_defaults(func=cmd_peers)

    run_parser = subparsers.add_parser(
        "run", parents=[common], help="stay online, punch holes and chat"
    )
    run_parser.add_argument("--peer", help="peer id to connect to automatically")
    run_parser.add_argument(
        "--send",
        action="append",
        default=[],
        metavar="TEXT",
        help="send TEXT once connected (repeatable)",
    )
    run_parser.add_argument(
        "--wait", type=float, default=None, metavar="SECONDS", help="exit after SECONDS"
    )
    run_parser.add_argument("--wait-peer", type=float, default=60.0, help="how long to wait for --peer")
    run_parser.add_argument("--punch-timeout", type=float, default=20.0)
    run_parser.add_argument("--punch-interval", type=float, default=0.25)
    run_parser.add_argument("--no-stdin", action="store_true", help="do not read interactive input")
    run_parser.set_defaults(func=cmd_run)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RendezvousError as exc:
        log(f"server error: {exc}")
        return 1
    except HolePunchError as exc:
        log(f"punch failed: {exc}")
        return 1
    except OSError as exc:
        log(f"socket/network error: {exc}")
        return 1
    except KeyboardInterrupt:
        log("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
