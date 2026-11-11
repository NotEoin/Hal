"""The gateway admin client for halcore and the `hal-gateway` command line (design 05).

It speaks `/hal/v1/*` as the owner over the gateway's unix socket, the only place where admin
routes are served. Every call has a timeout; calls that wait for a model (`set_mode`, `load`,
`unload`, `release_gpu`, `backend`) widen it by the wait they ask for, so a slow load is not reported as a
dead gateway. A refusal raises `ClientError` carrying the HTTP status and the server's error
type, so callers can tell a busy router (409) from a missing one (404).
"""

from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .gpu import LeaseInfo, ReleaseResult

DEFAULT_TIMEOUT_S = 10.0  # plain reads answer at once; the router calls extend this
RELEASE_TIMEOUT_S = 180.0  # an unload waits for a running generation (05, REQ-GW-55)
MARGIN_S = 30.0  # added to a requested wait, so the server's own deadline fires first


class ClientError(RuntimeError):
    """The gateway is unreachable or answered an error."""

    def __init__(self, message: str, status: int = 0, error_type: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type


@dataclass(frozen=True)
class SessionGrant:
    """A minted session token; the secret is shown once."""

    id: str
    token: str
    expires: str  # UTC Z


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        """Connects to the unix socket."""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self._path))


class GatewayClient:
    """Calls the admin API over the gateway's unix socket as the owner."""

    def __init__(self, socket_path: Path, token: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.socket_path, self.token, self.timeout_s = Path(socket_path), token, timeout_s

    @classmethod
    def from_config(
        cls, config_path: Path | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> GatewayClient:
        """Builds a client from the Hal config: its socket path and owner token file.

        Raises `ClientError` when the token file cannot be read.
        """
        from .config import load_config  # lazy: the config loader pulls in the whole schema
        from .server import Gateway

        gateway = Gateway(load_config(config_path))
        token_file = Path(str(gateway.config["token_file"])).expanduser()
        try:
            token = token_file.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise ClientError(f"cannot read the owner token at {token_file}: {exc}") from exc
        return cls(gateway.socket_path(), token, timeout_s=timeout_s)

    def admin(
        self,
        method: str,
        path: str,
        body: dict | None = None,  # type: ignore[type-arg]
        *,
        timeout_s: float | None = None,
    ) -> dict:  # type: ignore[type-arg]
        """Sends one request; returns the JSON answer; raises `ClientError`."""
        conn = _UnixConnection(self.socket_path, timeout_s or self.timeout_s)
        try:
            payload = json.dumps(body if body is not None else {}) if method == "POST" else None
            headers = {"Authorization": f"Bearer {self.token}"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except OSError as exc:  # includes socket timeouts
            raise ClientError(f"cannot reach the gateway at {self.socket_path}: {exc}") from exc
        finally:
            conn.close()
        try:
            doc = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ClientError(f"the gateway sent a non-JSON answer ({resp.status})", resp.status) from exc
        if resp.status >= 400:
            err = doc.get("error", {}) if isinstance(doc, dict) else {}
            raise ClientError(
                f"gateway answered {resp.status}: {err.get('message', '')}",
                resp.status,
                str(err.get("type", "")),
            )
        return doc  # type: ignore[no-any-return]

    def stream(self, path: str, body: dict, *, timeout_s: float) -> Iterator[bytes]:  # type: ignore[type-arg]
        """POSTs `body` to `path` and yields the answer line by line (server-sent events).

        Raises `ClientError` when the gateway is unreachable or answers with an error.
        """
        conn = _UnixConnection(self.socket_path, timeout_s)
        try:
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            conn.request("POST", path, body=json.dumps(body), headers=headers)
            resp = conn.getresponse()
            if resp.status >= 400:
                raw = resp.read()
                try:
                    err = json.loads(raw or b"{}").get("error", {})
                except (ValueError, AttributeError):
                    err = {}
                raise ClientError(
                    f"gateway answered {resp.status}: {err.get('message', '')}",
                    resp.status,
                    str(err.get("type", "")),
                )
            yield from resp
        except OSError as exc:  # includes socket timeouts
            raise ClientError(f"cannot reach the gateway at {self.socket_path}: {exc}") from exc
        finally:
            conn.close()

    def health(self) -> bool:
        """True when `/health` answers; never raises."""
        conn = _UnixConnection(self.socket_path, self.timeout_s)
        try:
            conn.request("GET", "/health")
            return conn.getresponse().status == 200
        except OSError:
            return False
        finally:
            conn.close()

    def status(self) -> dict[str, Any]:
        """`GET /hal/v1/status`: backends, GPU, warnings."""
        return self.admin("GET", "/hal/v1/status")

    def aliases(self) -> dict[str, Any]:
        """`GET /hal/v1/aliases`: listed names, roles and the active mode."""
        return self.admin("GET", "/hal/v1/aliases")

    def models(self) -> list[dict[str, Any]]:
        """`GET /v1/models` as the owner."""
        return list(self.admin("GET", "/v1/models").get("data", []))

    def set_mode(self, mode: str, *, wait_s: float = 900.0, force: bool = False) -> dict[str, Any]:
        """Switches the managed router's mode; raises `ClientError` 409 when it is busy."""
        body = {"mode": mode, "wait_s": wait_s, "force": force}
        return self.admin("POST", "/hal/v1/mode", body, timeout_s=wait_s + MARGIN_S)

    def stop_router(self) -> None:
        """Stops the managed router process."""
        self.admin("POST", "/hal/v1/router/stop", timeout_s=RELEASE_TIMEOUT_S)

    def backend(
        self, action: Literal["start", "stop", "restart"], name: str, *, wait_s: float = 900.0
    ) -> dict[str, Any]:
        """Starts, stops or restarts managed server `name`; a start waits up to `wait_s` for it."""
        return self.admin("POST", f"/hal/v1/backend/{action}", {"name": name}, timeout_s=wait_s + MARGIN_S)

    def load(self, model: str, wait_s: float = 900.0) -> None:
        """Loads one router preset."""
        self.admin(
            "POST", "/hal/v1/models/load", {"model": model, "wait_s": wait_s}, timeout_s=wait_s + MARGIN_S
        )

    def unload(self, model: str) -> None:
        """Unloads one router preset."""
        self.admin("POST", "/hal/v1/models/unload", {"model": model}, timeout_s=RELEASE_TIMEOUT_S)

    def release_gpu(
        self, force: bool = False, *, stop_router: bool = False, reclaim: bool = False
    ) -> ReleaseResult:
        """Unloads models and releases the GPU claims; a kept release is a result, not an error."""
        body = {"force": force, "stop_router": stop_router, "reclaim": reclaim}
        doc = self.admin("POST", "/hal/v1/gpu/release", body, timeout_s=RELEASE_TIMEOUT_S)
        return ReleaseResult(
            released=bool(doc.get("released")),
            unloaded=tuple(doc.get("unloaded", ())),
            router_stopped=bool(doc.get("router_stopped")),
            kept=tuple(LeaseInfo(k["session_id"], k["label"], k["since"]) for k in doc.get("kept", ())),
            busy=int(doc.get("busy", 0)),
        )

    def leases(self) -> list[LeaseInfo]:
        """`GET /hal/v1/leases`: sessions that keep the GPU state."""
        rows = self.admin("GET", "/hal/v1/leases").get("leases", [])
        return [LeaseInfo(r["session_id"], r["label"], r["since"]) for r in rows]

    def create_session(
        self,
        label: str,
        *,
        aliases: Sequence[str] = ("*",),
        allow_remote: bool = False,
        max_request_bytes: int | None = None,
        max_concurrent: int | None = None,
        ttl_s: int | None = None,
        cls: Literal["interactive", "service"] = "interactive",
        lease: bool = False,
    ) -> SessionGrant:
        """Mints a session token; omitted limits take the gateway's defaults."""
        body: dict[str, Any] = {
            "label": label, "aliases": list(aliases), "allow_remote": allow_remote,
            "class": cls, "lease": lease,
        }  # fmt: skip
        for key, value in (
            ("max_request_bytes", max_request_bytes),
            ("max_concurrent", max_concurrent),
            ("ttl_s", ttl_s),
        ):
            if value is not None:
                body[key] = value
        doc = self.admin("POST", "/hal/v1/sessions", body)
        return SessionGrant(str(doc["id"]), str(doc["token"]), str(doc["expires"]))

    def revoke_session(self, session_id: str) -> None:
        """Revokes one session; raises `ClientError` 404 for an unknown id."""
        self._delete(f"/hal/v1/sessions/{session_id}")

    def _delete(self, path: str) -> None:
        conn = _UnixConnection(self.socket_path, self.timeout_s)
        try:
            conn.request("DELETE", path, headers={"Authorization": f"Bearer {self.token}"})
            resp = conn.getresponse()
            raw = resp.read()
        except OSError as exc:
            raise ClientError(f"cannot reach the gateway at {self.socket_path}: {exc}") from exc
        finally:
            conn.close()
        if resp.status >= 400:
            try:
                err = json.loads(raw or b"{}").get("error", {})
            except ValueError:
                err = {}
            raise ClientError(
                f"gateway answered {resp.status}: {err.get('message', '')}",
                resp.status,
                str(err.get("type", "")),
            )
