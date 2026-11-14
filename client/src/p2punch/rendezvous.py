"""HTTP client for the p2punch Cloudflare Worker."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .protocol import Endpoint

USER_AGENT = "p2punch/0.2 (+https://github.com/GFYGO/free-p2p)"


class RendezvousError(RuntimeError):
    """Raised when the rendezvous server rejects a request or is unreachable."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class RendezvousClient:
    def __init__(
        self,
        server: str,
        room: str,
        peer_id: str,
        secret: str,
        *,
        room_secret: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        self.server = server.rstrip("/")
        self.room = room
        self.peer_id = peer_id
        self.secret = secret
        self.room_secret = room_secret
        self.timeout = timeout

    def _headers(self, *, auth: bool) -> dict:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if auth:
            headers["X-Peer-Id"] = self.peer_id
            headers["X-Peer-Secret"] = self.secret
        if self.room_secret:
            headers["X-Room-Secret"] = self.room_secret
        return headers

    def _call(self, method: str, path: str, body: Optional[dict] = None, *, auth: bool = False) -> dict:
        data = None
        headers = self._headers(auth=auth)
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.server + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            message = raw.decode("utf-8", "replace")
            try:
                payload = json.loads(message)
                if isinstance(payload, dict) and payload.get("error"):
                    message = str(payload["error"])
            except ValueError:
                pass
            raise RendezvousError(
                f"{method} {path} -> HTTP {exc.code}: {message}", exc.code
            ) from None
        except urllib.error.URLError as exc:
            raise RendezvousError(
                f"cannot reach rendezvous server {self.server}: {exc.reason}"
            ) from None
        except (TimeoutError, socket.timeout):
            raise RendezvousError(f"rendezvous request timed out: {method} {path}") from None

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise RendezvousError(f"invalid JSON from rendezvous server: {method} {path}") from None

    def announce(
        self,
        *,
        punch_key: str,
        public_endpoint: Optional[Endpoint],
        local_endpoint: Optional[Endpoint],
        nat_type: str,
        metadata: Optional[dict] = None,
    ) -> dict:
        body = {
            "room": self.room,
            "peerId": self.peer_id,
            "secret": self.secret,
            "punchKey": punch_key,
            "natType": nat_type,
            "publicEndpoint": public_endpoint.to_dict() if public_endpoint else None,
            "localEndpoint": local_endpoint.to_dict() if local_endpoint else None,
            "metadata": metadata,
        }
        return self._call("POST", "/api/announce", body)

    def peers(self) -> dict:
        query = urllib.parse.urlencode({"room": self.room})
        return self._call("GET", f"/api/peers?{query}", auth=True)

    def leave(self) -> dict:
        return self._call(
            "POST",
            "/api/leave",
            {"room": self.room, "peerId": self.peer_id, "secret": self.secret},
        )


def whoami(server: str, *, room_secret: Optional[str] = None, timeout: float = 10.0) -> dict:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if room_secret:
        headers["X-Room-Secret"] = room_secret
    request = urllib.request.Request(server.rstrip("/") + "/api/whoami", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise RendezvousError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}", exc.code) from None
    except urllib.error.URLError as exc:
        raise RendezvousError(f"cannot reach rendezvous server: {exc.reason}") from None
