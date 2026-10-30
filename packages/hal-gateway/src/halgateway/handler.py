"""The HTTP handler: limits, authentication, dispatch. Only `GET /health` exists so far.

Decisions:

- HTTP/1.1 keep-alive; the handler never reads a body before the peer check and the token
  check have passed, so a foreign peer costs no memory.
- A peer with another uid gets a minimal 403 written in `setup()`, then the socket closes
  (`verify_request` could only drop it silently, and REQ-GW-02 wants the 403).
- Every error is a `GatewayError` rendered in the route's format by `errors.error_body`.
"""

from __future__ import annotations

import http.server
import json
import logging

from . import auth
from .errors import Forbidden, GatewayError, NotFound, Unauthorized, error_body

log = logging.getLogger(__name__)

HEALTH_BODY = json.dumps({"status": "ok"}).encode("ascii")


class Handler(http.server.BaseHTTPRequestHandler):
    """Serves one connection; `self.server` supplies the gateway state (`gateway`)."""

    protocol_version = "HTTP/1.1"
    server_version = "hal-gateway"

    def address_string(self) -> str:
        """Client address for logs; the unix socket has none."""
        return "unix" if not self.client_address else str(self.client_address[0])

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base class signature
        """Routes http.server's access lines to the module logger at debug level."""
        log.debug("%s %s", self.address_string(), format % args)

    def setup(self) -> None:
        """Sets the client timeout and refuses a unix peer with another uid (REQ-GW-02)."""
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

    def _handle(self) -> None:
        gateway = self.server.gateway  # type: ignore[attr-defined]
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
            path = self.path.split("?", 1)[0]
            if self.command == "GET" and path == "/health":
                self._send(200, HEALTH_BODY)
                return
            principal = auth.principal_for(kind, self.peer, self.headers, gateway.owner_token)
            if principal.kind == "anonymous":
                raise Unauthorized("missing API key")
            raise NotFound(f"no route for {self.command} {path}")
        except GatewayError as exc:
            self._send(exc.status, error_body(exc, "openai"))

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
