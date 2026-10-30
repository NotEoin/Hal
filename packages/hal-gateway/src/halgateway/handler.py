"""The HTTP handler: limits, authentication, route match, dispatch to the relay.

Decisions:

- HTTP/1.1 keep-alive; the handler never reads a body before the peer check and the token
  check have passed, so a foreign peer costs no memory.
- A peer with another uid gets a minimal 403 written in `setup()`, then the socket closes
  (`verify_request` could only drop it silently, and REQ-GW-02 wants the 403).
- Every error is a `GatewayError` rendered in the route's format by `errors.error_body`.
- The body of a model route is parsed as JSON, its `model` rewritten to the upstream name and
  every other field kept (REQ-GW-11). Routes without a built handler answer 404 saying so.
"""

from __future__ import annotations

import http.server
import json
import logging

from . import auth, relay, routes
from .errors import (
    BadRequest,
    Forbidden,
    GatewayError,
    NotFound,
    PayloadTooLarge,
    Unauthorized,
    error_body,
)
from .routes import Api, Route

log = logging.getLogger(__name__)

HEALTH_BODY = json.dumps({"status": "ok"}).encode("ascii")
RELAYED = frozenset(
    {Api.CHAT, Api.RESPONSES, Api.RESPONSES_COUNT, Api.MESSAGES, Api.COUNT_TOKENS, Api.EMBEDDINGS}
)
STATUS_LENGTH_REQUIRED = 411


class LengthRequired(GatewayError):
    """A POST without `Content-Length`."""

    status = STATUS_LENGTH_REQUIRED
    code = "length_required"


class Handler(http.server.BaseHTTPRequestHandler):
    """Serves one connection; `self.server.gateway` supplies the gateway state."""

    protocol_version = "HTTP/1.1"
    server_version = "hal-gateway"

    def address_string(self) -> str:
        """Client address for logs; the unix socket has none."""
        return "unix" if not self.client_address else str(self.client_address[0])

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base class signature
        """Routes http.server's access lines to the module logger at debug level."""
        log.debug("%s %s", self.address_string(), format % args)

    def setup(self) -> None:
        """Sets the client timeout and records the unix peer's uid (REQ-GW-02)."""
        super().setup()
        gateway = self.server.gateway  # type: ignore[attr-defined]  # set by the server classes
        self.connection.settimeout(gateway.config["client_timeout_s"])
        self.peer: int | None = None
        if gateway.listener_kind(self.server) == "unix":
            self.peer = gateway.peer_uid(self.connection)
        self.foreign_peer = self.peer is not None and self.peer != gateway.own_uid

    def do_GET(self) -> None:  # noqa: N802 - http.server dispatch name
        """Handles a GET request."""
        self._handle()

    def do_POST(self) -> None:  # noqa: N802 - http.server dispatch name
        """Handles a POST request."""
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802 - http.server dispatch name
        """Handles a DELETE request."""
        self._handle()

    def _handle(self) -> None:
        gateway = self.server.gateway  # type: ignore[attr-defined]
        route = routes.match(self.command, self.path)
        fmt = route.fmt if route else "openai"
        try:
            if self.foreign_peer:
                self.close_connection = True
                raise Forbidden("this socket serves its owner only")
            kind = gateway.listener_kind(self.server)
            if kind == "tcp":
                auth.check_host_and_origin(
                    self.headers,
                    self.server.server_address[1],
                    gateway.config["allow_hosts"],
                    gateway.config["allow_origins"],
                )
            if route is not None and route.api is Api.HEALTH:
                self._send(200, HEALTH_BODY)
                return
            principal = auth.principal_for(kind, self.peer, self.headers, gateway.owner_token)
            if principal.kind == "anonymous":
                raise Unauthorized("missing API key")
            if route is None:
                raise NotFound(f"no route for {self.command} {self.path.split('?', 1)[0]}")
            self._dispatch(gateway, route)
        except GatewayError as exc:
            self._send(exc.status, error_body(exc, fmt))

    def _dispatch(self, gateway, route: Route) -> None:  # type: ignore[no-untyped-def]
        if route.api not in RELAYED:
            raise NotFound(f"{route.api.value} routes are not available yet")
        body = self._read_json(int(gateway.config["max_request_bytes"]))
        model = body.get("model") or gateway.config["default_model"]
        if not isinstance(model, str) or not model:
            raise BadRequest("the request has no model and [gateway].default_model is empty")
        backend, upstream_model = gateway.resolve(model)
        if not backend.supports(route.api):
            raise NotFound(
                f"backend {backend.name} does not serve {route.api.value}; it serves: "
                + ", ".join(backend.api_names())
            )
        body["model"] = upstream_model
        self._relay(gateway, backend, json.dumps(body).encode("utf-8"))

    def _read_json(self, limit: int) -> dict:  # type: ignore[type-arg]
        if "Transfer-Encoding" in self.headers:
            raise BadRequest("chunked request bodies are not supported")
        length = self.headers.get("Content-Length")
        if length is None:
            raise LengthRequired("POST needs a Content-Length")
        if not length.isdigit():
            raise BadRequest("Content-Length is not a number")
        if int(length) > limit:
            self.close_connection = True  # the body is never read, so the stream is out of sync
            raise PayloadTooLarge(f"request body is larger than {limit} bytes")
        raw = self.rfile.read(int(length))
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise BadRequest(f"request body is not JSON: {exc}") from exc
        if not isinstance(doc, dict):
            raise BadRequest("request body must be a JSON object")
        return doc

    def _relay(self, gateway, backend, body: bytes) -> None:  # type: ignore[no-untyped-def]
        cfg = gateway.config
        conn = relay.connect(
            backend.address(), cfg["upstream_connect_timeout_s"], cfg["upstream_read_timeout_s"]
        )
        headers = relay.request_headers(self.headers, backend.auth_headers())
        headers["Content-Length"] = str(len(body))
        try:
            resp = relay.open_response(conn, self.command, self.path, body, headers)
            self.send_response_only(resp.status)
            length_known = False
            for name, value in relay.response_headers(resp.getheaders()):
                if name.lower() in ("date", "server"):
                    continue
                length_known = length_known or name.lower() == "content-length"
                self.send_header(name, value)
            if not length_known:
                self.close_connection = True  # a close-delimited body
                self.send_header("Connection", "close")
            self.end_headers()
            if not relay.copy_body(resp, self._write):
                self.close_connection = True
        finally:
            conn.close()

    def _write(self, chunk: bytes) -> None:
        self.wfile.write(chunk)
        self.wfile.flush()

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
