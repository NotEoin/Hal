"""Model forwarder: serves the OpenAI-compatible API on a unix socket and relays to a local endpoint.

Only `POST /v1/chat/completions` and `GET /v1/models` are forwarded; every other method or path
gets 404 or 405 and never reaches the upstream, so a boxed agent cannot use the model server for
anything else (admin routes of llama-server or Ollama stay unreachable). Headers from the box are
dropped except `Content-Type`; the box never sees or sends upstream credentials. Bodies are capped
at `MAX_BODY_BYTES`. Design: 02-sandbox.md (the full gateway forwarder is 05-gateway's).
"""

from __future__ import annotations

import http.client
import logging
import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 4 * 1024 * 1024  # same cap as the broker socket messages (REQ-BOX-29)
UPSTREAM_TIMEOUT_S = 600.0  # a local model can think for minutes before the first byte
CHUNK_BYTES = 8192
ALLOWED: dict[tuple[str, str], str] = {
    ("POST", "/v1/chat/completions"): "/v1/chat/completions",
    ("GET", "/v1/models"): "/v1/models",
}


class ModelForwarder(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """A unix-socket HTTP server that forwards the allowed routes to `upstream`."""

    daemon_threads = True

    def __init__(self, socket_path: Path, upstream: str, timeout_s: float = UPSTREAM_TIMEOUT_S) -> None:
        parts = urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"upstream must be an http(s) URL: {upstream!r}")
        self.upstream = parts
        self.timeout_s = timeout_s
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

    def connect(self) -> http.client.HTTPConnection:
        """Opens a connection to the upstream."""
        cls = http.client.HTTPSConnection if self.upstream.scheme == "https" else http.client.HTTPConnection
        return cls(self.upstream.hostname, self.upstream.port, timeout=self.timeout_s)


class _Handler(BaseHTTPRequestHandler):
    server: ModelForwarder

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base class signature
        log.debug("forwarder: " + format, *args)

    def _refuse(self, status: int, text: str) -> None:
        body = (text + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str) -> None:
        route = ALLOWED.get((method, self.path))
        if route is None:
            known = {path for _, path in ALLOWED}
            log.warning("forwarder refused %s %s", method, self.path[:200])
            self._refuse(405 if self.path in known else 404, "not forwarded")
            return
        body = b""
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._refuse(411, "Content-Length required")
                return
            if length < 0 or length > MAX_BODY_BYTES:
                self._refuse(413, "request too large")
                return
            body = self.rfile.read(length)
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
