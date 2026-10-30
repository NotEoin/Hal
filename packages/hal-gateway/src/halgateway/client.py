"""A small client for the gateway admin routes over the owner's unix socket.

Only the call the CLI needs is here so far; the full client for halcore comes with its own
step. Every call has a timeout (a release may wait for a model to unload).
"""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path

DEFAULT_TIMEOUT_S = 180.0  # an unload waits for a running generation (05, REQ-GW-55)


class ClientError(RuntimeError):
    """The gateway is unreachable or answered an error."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


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
    """Calls `/hal/v1/*` as the owner on the unix socket."""

    def __init__(self, socket_path: Path, token: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.socket_path, self.token, self.timeout_s = socket_path, token, timeout_s

    def admin(self, method: str, path: str, body: dict | None = None) -> dict:  # type: ignore[type-arg]
        """Sends one admin request; returns the JSON answer; raises `ClientError`."""
        conn = _UnixConnection(self.socket_path, self.timeout_s)
        try:
            payload = json.dumps(body if body is not None else {}) if method == "POST" else None
            conn.request(method, path, body=payload, headers={"Authorization": f"Bearer {self.token}"})
            resp = conn.getresponse()
            raw = resp.read()
        except OSError as exc:
            raise ClientError(f"cannot reach the gateway at {self.socket_path}: {exc}") from exc
        finally:
            conn.close()
        try:
            doc = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ClientError(f"the gateway sent a non-JSON answer ({resp.status})", resp.status) from exc
        if resp.status >= 400:
            message = doc.get("error", {}).get("message", "") if isinstance(doc, dict) else ""
            raise ClientError(f"gateway answered {resp.status}: {message}", resp.status)
        return doc  # type: ignore[no-any-return]
