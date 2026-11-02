"""Model forwarder: serves the OpenAI-compatible API on a unix socket and relays to a local endpoint.

Only `POST /v1/chat/completions` and `GET /v1/models` are forwarded; every other method or path
gets 404 or 405 and never reaches the upstream, so a boxed agent cannot use the model server for
anything else (admin routes of llama-server or Ollama stay unreachable). Headers from the box are
dropped except `Content-Type`; the box never sees or sends upstream credentials. Bodies are capped
at `MAX_BODY_BYTES`. The box is hostile, so a connection must come from the forwarder's own uid
(`SO_PEERCRED`), a client that stalls mid-request is dropped after `CLIENT_TIMEOUT_S`, and at most
`MAX_CONNECTIONS` requests run at once. A request body must arrive within `BODY_DEADLINE_S` in
total (a trickle of bytes cannot hold a slot), and a session gets at most `MAX_REQUESTS_PER_WINDOW`
requests per `WINDOW_S` (429 after that), so a looping or hostile agent cannot saturate the model.
Design: 02-sandbox.md (the full gateway forwarder is 05-gateway's).
"""

from __future__ import annotations

import http.client
import logging
import os
import socket
import socketserver
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 4 * 1024 * 1024  # same cap as the broker socket messages (REQ-BOX-29)
UPSTREAM_TIMEOUT_S = 600.0  # a local model can think for minutes before the first byte
CHUNK_BYTES = 8192
CLIENT_TIMEOUT_S = 30.0  # a client that sends or reads nothing for this long is cut off
MAX_CONNECTIONS = 8  # concurrent requests per session
BODY_DEADLINE_S = 60.0  # total time to receive one request body, however slowly it trickles
WINDOW_S = 60.0
MAX_REQUESTS_PER_WINDOW = 240  # a coding agent makes a few calls a minute; this only stops floods
READ_CHUNK_BYTES = 65536
_PEERCRED = struct.Struct("3i")  # pid, uid, gid
ALLOWED: dict[tuple[str, str], str] = {
    ("POST", "/v1/chat/completions"): "/v1/chat/completions",
    ("GET", "/v1/models"): "/v1/models",
}


class ModelForwarder(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """A unix-socket HTTP server that forwards the allowed routes to `upstream`."""

    daemon_threads = True

    def __init__(
        self,
        socket_path: Path,
        upstream: str,
        timeout_s: float = UPSTREAM_TIMEOUT_S,
        *,
        allowed_uid: int | None = None,
        max_connections: int = MAX_CONNECTIONS,
        max_body_bytes: int = MAX_BODY_BYTES,
        max_requests: int = MAX_REQUESTS_PER_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        parts = urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"upstream must be an http(s) URL: {upstream!r}")
        self.upstream = parts
        self.timeout_s = timeout_s
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self.max_body_bytes = max_body_bytes
        self.max_requests = max_requests
        self._clock = clock
        self._stamps: deque[float] = deque()
        self._stamps_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(max_connections)
        prefix = parts.path.rstrip("/")
        self.prefix = prefix[:-3] if prefix.endswith("/v1") else prefix
        self.socket_path = Path(socket_path)
        old_umask = os.umask(0o177)  # the socket file is created 0600
        try:
            super().__init__(str(self.socket_path), _Handler)
        finally:
            os.umask(old_umask)

    def start(self) -> threading.Thread:
        """Serves in a background thread and returns it."""
        self._thread = threading.Thread(target=self.serve_forever, name="model-forwarder", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        """Stops serving and removes the socket file."""
        if getattr(self, "_thread", None) is not None:  # shutdown() blocks unless serving
            self.shutdown()
        self.server_close()
        self.socket_path.unlink(missing_ok=True)

    def verify_request(self, request: socket.socket, client_address: object) -> bool:
        """Accepts only a peer with the allowed uid, and only while a connection slot is free."""
        _, uid, _ = _PEERCRED.unpack(
            request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size)
        )
        if uid != self.allowed_uid:
            log.warning("forwarder: dropped a connection from uid %d", uid)
            return False
        if not self.slots.acquire(blocking=False):
            log.warning("forwarder: too many connections, dropping one")
            return False
        return True

    def process_request_thread(self, request: socket.socket, client_address: object) -> None:
        """Handles one connection, then frees its slot."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def within_rate(self) -> bool:
        """True and counts the request when the session is under its request budget for the window."""
        now = self._clock()
        with self._stamps_lock:
            while self._stamps and now - self._stamps[0] > WINDOW_S:
                self._stamps.popleft()
            if len(self._stamps) >= self.max_requests:
                return False
            self._stamps.append(now)
            return True

    def connect(self) -> http.client.HTTPConnection:
        """Opens a connection to the upstream."""
        cls = http.client.HTTPSConnection if self.upstream.scheme == "https" else http.client.HTTPConnection
        return cls(self.upstream.hostname, self.upstream.port, timeout=self.timeout_s)


class _Handler(BaseHTTPRequestHandler):
    server: ModelForwarder
    timeout = CLIENT_TIMEOUT_S  # StreamRequestHandler applies this to the client socket

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base class signature
        log.debug("forwarder: " + format, *args)

    def _refuse(self, status: int, text: str) -> None:
        body = (text + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, length: int) -> bytes | None:
        """The body, or None when it did not arrive in full within `BODY_DEADLINE_S`."""
        deadline = time.monotonic() + BODY_DEADLINE_S
        chunks: list[bytes] = []
        left = length
        while left > 0:
            if time.monotonic() > deadline:
                return None
            chunk = self.rfile.read1(min(left, READ_CHUNK_BYTES))
            if not chunk:
                return None
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)

    def _handle(self, method: str) -> None:
        route = ALLOWED.get((method, self.path))
        if route is None:
            known = {path for _, path in ALLOWED}
            log.warning("forwarder refused %s %s", method, self.path[:200])
            self._refuse(405 if self.path in known else 404, "not forwarded")
            return
        if not self.server.within_rate():
            log.warning("forwarder: request budget exhausted")
            self._refuse(429, "too many requests")
            return
        body = b""
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._refuse(411, "Content-Length required")
                return
            if length < 0 or length > self.server.max_body_bytes:
                self._refuse(413, "request too large")
                return
            body = self._read_body(length)
            if body is None:
                self._refuse(408, "request body too slow")
                return
        headers = {"Accept": self.headers.get("Accept", "*/*")}
        if body:
            headers["Content-Type"] = self.headers.get("Content-Type", "application/json")
        conn = self.server.connect()
        try:
            conn.request(method, self.server.prefix + route, body=body or None, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type", "application/json"))
            self.send_header("Connection", "close")  # body ends at close; works for SSE too
            self.end_headers()
            while chunk := resp.read1(CHUNK_BYTES):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException) as exc:
            log.warning("forwarder upstream error: %s", exc)
            if not self.wfile.closed:
                try:
                    self._refuse(502, "upstream unavailable")
                except OSError:
                    # the client left before the refusal was sent
                    pass
        finally:
            conn.close()
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        """Forwards GET /v1/models."""
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        """Forwards POST /v1/chat/completions."""
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802 - http.server naming
        """Refuses PUT."""
        self._handle("PUT")

    do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_PUT
