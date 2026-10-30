"""The `Gateway` object: owner-only unix listener, optional loopback TCP listener, lifecycle.

Decisions:

- The runtime directory must be private and the socket is bound under `umask 0o177`, then
  `chmod 0600` (REQ-GW-01); a path that cannot be made safe stops the start with a message that
  names the setting to change (REQ-GW-03).
- A socket file left behind is replaced only after a connect is refused; a live one stops the
  start (REQ-GW-04), so two gateways never share a runtime directory.
- Connections are capped by a semaphore taken in `process_request`; beyond the cap the client
  gets 503 with `Retry-After: 1` and the thread is not started (REQ-GW-72).
"""

from __future__ import annotations

import errno
import http.server
import logging
import os
import socket
import socketserver
import threading
from collections.abc import Mapping
from pathlib import Path

from halkit import paths

from . import auth
from .config import GatewayConfig
from .errors import Unavailable, error_body
from .handler import Handler

log = logging.getLogger(__name__)

SOCKET_NAME = "gateway.sock"
SOCKET_MODE = 0o600
BIND_UMASK = 0o177  # the socket is created 0600 even before the chmod
LISTEN_BACKLOG = 64
RETRY_AFTER_S = 1
PROBE_TIMEOUT_S = 1.0


class StartError(Exception):
    """The gateway cannot start; the message says why and which setting to change."""


class _CappedMixin:
    """Refuses a connection beyond the cap with a 503 instead of starting a thread."""

    gateway: Gateway

    def process_request(self, request, client_address):  # type: ignore[no-untyped-def]  # socketserver API
        """Starts the handler thread, or answers 503 when `max_connections` are open."""
        gateway = self.gateway
        if not gateway.slots.acquire(blocking=False):
            body = error_body(Unavailable("too many open connections"), "openai")
            head = (
                f"HTTP/1.1 503 Service Unavailable\r\nRetry-After: {RETRY_AFTER_S}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            )
            try:
                request.settimeout(PROBE_TIMEOUT_S)
                request.sendall(head.encode("ascii") + body)
            except OSError:
                log.debug("client left before the 503 was written")
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)  # type: ignore[misc]

    def process_request_thread(self, request, client_address):  # type: ignore[no-untyped-def]  # socketserver API
        """Runs the handler, then frees the connection slot."""
        try:
            super().process_request_thread(request, client_address)  # type: ignore[misc]
        finally:
            self.gateway.slots.release()


class UnixHTTPServer(_CappedMixin, socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Serves HTTP on an owner-only unix socket."""

    daemon_threads = True
    request_queue_size = LISTEN_BACKLOG

    def server_bind(self) -> None:
        """Binds under a restrictive umask, then sets mode 0600."""
        old = os.umask(BIND_UMASK)
        try:
            socketserver.UnixStreamServer.server_bind(self)
        finally:
            os.umask(old)
        os.chmod(self.server_address, SOCKET_MODE)
        self.server_name = "localhost"  # http.server would call getfqdn on a path
        self.server_port = 0


class LoopbackHTTPServer(_CappedMixin, http.server.ThreadingHTTPServer):
    """Serves HTTP on 127.0.0.1 or ::1 only."""

    daemon_threads = True
    request_queue_size = LISTEN_BACKLOG

    def __init__(self, address: tuple[str, int], handler: type, *, ipv6: bool) -> None:
        if ipv6:
            self.address_family = socket.AF_INET6
        super().__init__(address, handler)


class Gateway:
    """Owns the listeners and the state the handler reads."""

    def __init__(
        self,
        cfg: GatewayConfig,
        *,
        peer_uid=auth.peer_uid,
        own_uid: int | None = None,  # type: ignore[no-untyped-def]  # injectable for tests
    ) -> None:
        self.cfg = cfg
        self.config: Mapping[str, object] = cfg.gateway
        self.peer_uid = peer_uid
        self.own_uid = os.getuid() if own_uid is None else own_uid
        self.slots = threading.BoundedSemaphore(int(self.config["max_connections"]))  # type: ignore[call-overload]
        self.owner_token = ""
        self.unix: UnixHTTPServer | None = None
        self.tcp: LoopbackHTTPServer | None = None
        self._threads: list[threading.Thread] = []

    def listener_kind(self, server: object) -> str:
        """Returns `unix` or `tcp` for one of this gateway's servers."""
        return "unix" if server is self.unix else "tcp"

    def socket_path(self) -> Path:
        """The unix socket path from config or the runtime directory (REQ-GW-03)."""
        configured = str(self.config["unix_path"])
        try:
            path = Path(configured).expanduser() if configured else paths.runtime_dir() / SOCKET_NAME
        except paths.RuntimeDirMissing as exc:
            raise StartError(f"{exc}; set HAL_RUNTIME_DIR or [gateway].unix_path") from exc
        try:
            return paths.check_socket_path(path)
        except paths.SocketPathTooLong as exc:
            raise StartError(f"{exc}; set [gateway].unix_path to a shorter path") from exc

    def start(self) -> None:
        """Binds the listeners and serves them on background threads; raises `StartError`."""
        path = self.socket_path()
        try:
            paths.ensure_private_dir(path.parent)
        except paths.UnsafePathError as exc:
            raise StartError(f"runtime directory is not private: {exc}") from exc
        token_path = Path(str(self.config["token_file"])).expanduser()
        self.owner_token = auth.load_or_create_owner_token(token_path)
        if self.config["tcp"] and not auth.token_file_is_private(token_path):
            raise StartError(f"token file {token_path} is readable by others; run chmod 600 on it")
        self._clear_stale_socket(path)
        self.unix = UnixHTTPServer(str(path), Handler)
        self.unix.gateway = self
        servers: list[http.server.HTTPServer | socketserver.BaseServer] = [self.unix]
        if self.config["tcp"]:
            host = str(self.config["tcp_host"])
            self.tcp = LoopbackHTTPServer((host, int(self.config["tcp_port"])), Handler, ipv6=":" in host)  # type: ignore[call-overload]
            self.tcp.gateway = self
            servers.append(self.tcp)
        for server in servers:
            thread = threading.Thread(target=server.serve_forever, name="hal-gateway", daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("gateway listening on %s", path)

    @staticmethod
    def _clear_stale_socket(path: Path) -> None:
        """Removes a socket nobody answers on; raises `StartError` when a gateway answers."""
        if not path.exists():
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(PROBE_TIMEOUT_S)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            path.unlink()
            log.info("replaced a stale socket %s", path)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise StartError(f"cannot probe {path}: {exc}") from exc
        else:
            raise StartError(f"another gateway is running on {path}")
        finally:
            probe.close()

    def stop(self) -> None:
        """Stops the listeners and removes the socket file."""
        for server in (self.unix, self.tcp):
            if server is not None:
                server.shutdown()
                server.server_close()
        if self.unix is not None:
            Path(str(self.unix.server_address)).unlink(missing_ok=True)
        for thread in self._threads:
            thread.join(timeout=PROBE_TIMEOUT_S * 5)
