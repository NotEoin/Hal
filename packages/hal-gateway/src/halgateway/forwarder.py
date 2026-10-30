"""The per-session model socket forwarder (REQ-GW-63 to 65, 69; 01-security REQ-SEC-66).

One forwarder serves one box session on a unix socket (mode 0600) and relays to the gateway
with that session's token. It is the host's first filter and the only enforcer of the model
budget, because it sees every request a session makes. Decisions:

- The path allowlist is matched on the path without the query string and anything else is
  answered 403 here, without contacting the gateway (ATK-33). The gateway checks the scope
  again; it stays authoritative.
- Client credentials and `X-Hal-*` headers are stripped, and the session token is injected.
- A request head or body that takes longer than `read_timeout_s` is dropped (slowloris).
- The budget (requests per minute, output cap, tokens per hour) lives in this process and
  resets only when the forwarder closes. Tokens are counted from the reply's `usage`, else
  estimated as the output cap plus the request body length / 4.
"""

from __future__ import annotations

import http.server
import ipaddress
import json
import logging
import os
import socketserver
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from . import relay, reqlog
from .backends.openai import UpstreamAddress

log = logging.getLogger(__name__)

# 01-security REQ-SEC-66: exactly these routes, matched on the path without the query string
ALLOWED = frozenset(
    {
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/responses"),
        ("POST", "/v1/responses/input_tokens"),
        ("POST", "/v1/messages"),
        ("POST", "/v1/messages/count_tokens"),
        ("POST", "/v1/embeddings"),
        ("GET", "/v1/models"),
    }
)
# route -> the output cap key a generation request must carry (REQ-GW-69)
OUTPUT_KEYS = {
    "/v1/chat/completions": ("max_tokens", "max_completion_tokens"),
    "/v1/messages": ("max_tokens",),
    "/v1/responses": ("max_output_tokens",),
}
REQUEST_WINDOW_S = 60.0
TOKEN_WINDOW_S = 3600.0
CHARS_PER_TOKEN = 4  # the estimate when a reply carries no usage (REQ-GW-69)
CONNECT_TIMEOUT_S = 5.0
UPSTREAM_READ_TIMEOUT_S = 7200.0  # a swap can take minutes (05 REQ-GW-47)
SOCKET_MODE = 0o600
REJECTED_CODES = (b"session_unknown", b"session_expired")
REJECTED_BODY_BYTES = 4096


@dataclass(frozen=True)
class ForwarderLimits:
    """Caps for one forwarder; halcore builds it from `[gateway.sessions]`."""

    max_request_bytes: int = 8 * 2**20
    max_concurrent: int = 4
    read_timeout_s: float = 30.0
    max_requests_per_min: int = 30
    max_output_tokens: int = 16384
    max_tokens_per_hour: int = 2_000_000


@dataclass(frozen=True)
class ForwarderEvent:
    """Something the owner may want to react to."""

    kind: Literal["token_rejected", "gateway_down", "rejected", "request"]
    detail: dict[str, object] = field(default_factory=dict)


class Budget:
    """Sliding windows for requests and tokens (REQ-GW-69)."""

    def __init__(self, limits: ForwarderLimits, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits, self.clock = limits, clock
        self._requests: deque[float] = deque()
        self._tokens: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def admit(self, estimate: int) -> tuple[str, int] | None:
        """None when a request fits; else `(limit name, seconds until it would)`. Records the request."""
        now = self.clock()
        with self._lock:
            while self._requests and now - self._requests[0] >= REQUEST_WINDOW_S:
                self._requests.popleft()
            while self._tokens and now - self._tokens[0][0] >= TOKEN_WINDOW_S:
                self._tokens.popleft()
            if len(self._requests) >= self.limits.max_requests_per_min:
                return "max_requests_per_min", max(1, int(REQUEST_WINDOW_S - (now - self._requests[0]) + 1))
            used = sum(t for _, t in self._tokens)
            if used + estimate > self.limits.max_tokens_per_hour:
                need, wait = used + estimate - self.limits.max_tokens_per_hour, TOKEN_WINDOW_S
                for stamp, tokens in self._tokens:
                    need -= tokens
                    if need <= 0:
                        wait = TOKEN_WINDOW_S - (now - stamp)
                        break
                return "max_tokens_per_hour", max(1, int(wait + 1))
            self._requests.append(now)
            return None

    def record(self, tokens: int) -> None:
        """Adds the tokens a finished request used."""
        with self._lock:
            self._tokens.append((self.clock(), tokens))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    forwarder: SessionForwarder

    def server_bind(self) -> None:
        old = os.umask(0o177)
        try:
            super().server_bind()
        finally:
            os.umask(old)
        os.chmod(self.server_address, SOCKET_MODE)  # type: ignore[arg-type]
        self.server_name, self.server_port = "localhost", 0

    def handle_error(self, request, client_address):  # type: ignore[no-untyped-def]  # socketserver API
        log.debug("forwarder connection failed", exc_info=True)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base class signature
        """Silences the default stderr log."""

    def setup(self) -> None:
        """Applies the read timeout to the connection (slowloris)."""
        self.timeout = self.server.forwarder.limits.read_timeout_s  # type: ignore[attr-defined]
        super().setup()

    def address_string(self) -> str:
        """No peer address on a unix socket."""
        return "box"

    def do_GET(self) -> None:  # noqa: N802 - http.server dispatch name
        """Handles GET."""
        self.server.forwarder.handle(self)  # type: ignore[attr-defined]

    do_POST = do_GET  # noqa: N815 - same dispatch
    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_GET  # noqa: N815 - every method is judged by the allowlist

    def reply(self, status: int, code: str, message: str, extra: list[tuple[str, str]] | None = None) -> None:
        """Sends a small JSON error and closes the connection."""
        body = json.dumps({"error": {"message": message, "type": code, "code": code}}).encode("ascii")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in extra or []:
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            log.debug("the box left before the error was written")


class SessionForwarder:
    """Serves one sandbox session's model socket and relays to the gateway with its token."""

    def __init__(
        self,
        listen_path: Path,
        gateway_socket: Path | None = None,
        token: str = "",
        *,
        gateway_url: str | None = None,
        limits: ForwarderLimits = ForwarderLimits(),  # noqa: B008 - frozen dataclass, safe as a default
        on_event: Callable[[ForwarderEvent], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (gateway_socket is None) == (gateway_url is None):
            raise ValueError("give exactly one of gateway_socket and gateway_url")
        if not token:
            raise ValueError("the forwarder needs a session token")
        self.listen_path, self.limits, self.on_event = Path(listen_path), limits, on_event
        self._token = token
        self.address = self._address(gateway_socket, gateway_url)
        self.budget = Budget(limits, clock)
        self._slots = threading.BoundedSemaphore(limits.max_concurrent)
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @staticmethod
    def _address(socket_path: Path | None, url: str | None) -> UpstreamAddress:
        if socket_path is not None:
            return UpstreamAddress("unix", "localhost", 0, str(socket_path))
        parts = urlsplit(url or "")
        if parts.scheme != "http" or not parts.hostname or not _is_loopback(parts.hostname):
            raise ValueError(f"gateway_url must be a loopback http:// URL, got {url!r}")
        return UpstreamAddress("http", parts.hostname, parts.port or 80)

    def start(self) -> None:
        """Binds the socket (0600) and serves on a daemon thread."""
        self.listen_path.unlink(missing_ok=True)
        self._server = _Server(str(self.listen_path), _Handler)
        self._server.forwarder = self
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(0.05),  # type: ignore[union-attr]
            name="hal-forwarder",
            daemon=True,
        )
        self._thread.start()

    def set_token(self, token: str) -> None:
        """Replaces the session token after a `token_rejected` event."""
        if not token:
            raise ValueError("the forwarder needs a session token")
        self._token = token

    def close(self, timeout_s: float = 5.0) -> None:
        """Stops serving and removes the socket."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout_s)
        self.listen_path.unlink(missing_ok=True)

    def _emit(self, kind: str, **detail: object) -> None:
        if self.on_event is not None:
            try:
                self.on_event(ForwarderEvent(kind, detail))  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 - a broken callback must not break the relay
                log.exception("forwarder event callback failed")

    # --- one request ---------------------------------------------------------------------

    def handle(self, h: _Handler) -> None:
        """Judges, budgets and relays one request."""
        path = h.path.split("?", 1)[0]
        if (h.command, path) not in ALLOWED:
            self._emit("rejected", limit="path", path=path)
            h.reply(403, "forbidden", f"{h.command} {path} is not available to a session")
            return
        if not self._slots.acquire(blocking=False):
            self._emit("rejected", limit="max_concurrent")
            h.reply(
                429,
                "rate_limited",
                f"at most {self.limits.max_concurrent} requests at once",
                [("Retry-After", "1")],
            )
            return
        try:
            self._serve(h, path)
        finally:
            self._slots.release()

    def _read_body(self, h: _Handler) -> bytes | None:
        """Reads the body within `read_timeout_s` in total; None after an error reply."""
        length = h.headers.get("Content-Length", "")
        if "Transfer-Encoding" in h.headers or not length.isdigit():
            h.reply(
                411 if "Transfer-Encoding" not in h.headers else 400,
                "bad_request",
                "a Content-Length is needed",
            )
            return None
        if int(length) > self.limits.max_request_bytes:
            self._emit("rejected", limit="max_request_bytes")
            h.reply(
                413, "payload_too_large", f"request body is larger than {self.limits.max_request_bytes} bytes"
            )
            return None
        deadline, chunks, left = time.monotonic() + self.limits.read_timeout_s, [], int(length)
        try:
            while left:
                if time.monotonic() > deadline:
                    raise TimeoutError
                chunk = h.rfile.read(min(left, relay.CHUNK_BYTES))
                if not chunk:
                    raise OSError("the box closed the connection")
                chunks.append(chunk)
                left -= len(chunk)
        except OSError:  # TimeoutError is an OSError
            h.close_connection = True
            self._emit("rejected", limit="read_timeout_s")
            return None
        return b"".join(chunks)

    def _serve(self, h: _Handler, path: str) -> None:
        body: bytes | None = None
        estimate = 0
        if h.command == "POST":
            body = self._read_body(h)
            if body is None:
                return
            clamped = self._clamp(path, body)
            if clamped is None:
                h.reply(400, "bad_request", "request body must be a JSON object")
                return
            body = clamped
            estimate = self.limits.max_output_tokens + len(body) // CHARS_PER_TOKEN
        denied = self.budget.admit(estimate)
        if denied is not None:
            limit, wait = denied
            self._emit("rejected", limit=limit, retry_after=wait)
            h.reply(429, "budget_exceeded", f"model budget reached: {limit}", [("Retry-After", str(wait))])
            return
        self._relay(h, body, estimate)

    def _clamp(self, path: str, body: bytes) -> bytes | None:
        """Sets the output cap keys to at most `max_output_tokens`; None when the body is not an object."""
        keys = OUTPUT_KEYS.get(path)
        try:
            doc = json.loads(body)
        except ValueError:
            return None
        if not isinstance(doc, dict):
            return None
        if keys is None:
            return body
        cap = self.limits.max_output_tokens
        present = [k for k in keys if k in doc]
        for key in present or keys[:1]:
            value = doc.get(key)
            doc[key] = cap if isinstance(value, bool) or not isinstance(value, int) or value > cap else value
        return json.dumps(doc).encode("utf-8")

    def _relay(self, h: _Handler, body: bytes | None, estimate: int) -> None:
        headers = relay.request_headers(h.headers, {"Authorization": f"Bearer {self._token}"})
        if body is not None:
            headers["Content-Length"] = str(len(body))
        try:
            conn = relay.connect(self.address, CONNECT_TIMEOUT_S, UPSTREAM_READ_TIMEOUT_S)
            resp = relay.open_response(conn, h.command, h.path, body, headers)
        except Exception as exc:  # noqa: BLE001 - any relay failure is a 502 for the box and an event for the owner
            self._emit("gateway_down", error=str(exc))
            h.reply(502, "bad_gateway", "the model gateway is not reachable")
            self.budget.record(estimate)
            return
        try:
            if resp.status == 401:
                head = resp.read(REJECTED_BODY_BYTES)
                if any(code in head for code in REJECTED_CODES):
                    self._emit("token_rejected")
                h.reply(401, "session_unknown", "the model session was rejected")
                self.budget.record(estimate)
                return
            self._stream(h, resp, estimate)
        finally:
            conn.close()

    def _stream(self, h: _Handler, resp, estimate: int) -> None:  # type: ignore[no-untyped-def]
        h.send_response_only(resp.status)
        length_known = False
        for name, value in relay.response_headers(resp.getheaders()):
            if name.lower() in ("date", "server"):
                continue
            length_known = length_known or name.lower() == "content-length"
            h.send_header(name, value)
        if not length_known:
            h.close_connection = True  # a close-delimited body
            h.send_header("Connection", "close")
        h.end_headers()
        tail = b""

        def write(chunk: bytes) -> None:
            nonlocal tail
            tail = (tail + chunk)[-reqlog.USAGE_TAIL_BYTES :]
            h.wfile.write(chunk)

        if not relay.copy_body(resp, write):
            h.close_connection = True
        usage = reqlog.scan_usage(tail)
        used = (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)) if usage else estimate
        self.budget.record(used)
        self._emit("request", status=resp.status, tokens=used)
