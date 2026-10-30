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
import time
from collections.abc import Mapping
from pathlib import Path

from halkit import paths

from . import auth, relay
from .aliases import Resolver
from .backends import LlamaRouter, make_backend
from .config import GatewayConfig
from .errors import GatewayError, ModelUnverified, Unavailable, error_body
from .handler import Handler
from .models.manifest import ManifestError, ModelEntry, Verifier, load_manifest
from .reqlog import RequestLog

log = logging.getLogger(__name__)

SOCKET_NAME = "gateway.sock"
VERIFIED_NAME = "verified.json"
DIGEST_TTL_S = 30.0  # REQ-GW-77: how long a digest read from /api/tags is trusted
SOCKET_MODE = 0o600
BIND_UMASK = 0o177  # the socket is created 0600 even before the chmod
LISTEN_BACKLOG = 64
RETRY_AFTER_S = 1
PROBE_TIMEOUT_S = 1.0
POLL_INTERVAL_S = 0.05  # how often serve_forever checks for shutdown; keeps stop() quick


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

    def handle_error(self, request, client_address):  # type: ignore[no-untyped-def]  # socketserver API
        """Logs a failed connection through `logging` instead of printing a traceback."""
        log.warning("connection from %s failed", client_address or "unix", exc_info=True)

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
        state_dir: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.config: Mapping[str, object] = cfg.gateway
        self.peer_uid = peer_uid
        self.own_uid = os.getuid() if own_uid is None else own_uid
        self.slots = threading.BoundedSemaphore(int(self.config["max_connections"]))  # type: ignore[call-overload]
        self.owner_token = ""
        self.state_dir = Path(state_dir) if state_dir is not None else paths.state_dir() / "gateway"
        self.reqlog = RequestLog(self.state_dir, cfg.log)
        self.manifest: dict[str, ModelEntry] = {}
        self.verifier = Verifier(self.manifest, self.state_dir / VERIFIED_NAME)
        self._digests: dict[str, tuple[float, dict[str, str]]] = {}  # backend -> (time, name -> digest)
        self.backends = {n: make_backend(n, e, cfg.ollama_url) for n, e in cfg.backends.items()}
        self.resolver = Resolver(cfg, self.backends, relay.fetch_model_ids, profile_of=self.profile_of)
        for backend in self.backends.values():
            if isinstance(backend, LlamaRouter):
                backend.configure(self.state_dir, self.models_dir(), self.verifier, self._ollama_backends)
        self.unix: UnixHTTPServer | None = None
        self.tcp: LoopbackHTTPServer | None = None
        self._threads: list[threading.Thread] = []

    def router_entry(self) -> Mapping[str, object]:
        """The first `llama-router` backend entry, or an empty mapping."""
        return next((e for e in self.cfg.backends.values() if e.get("type") == "llama-router"), {})

    def router(self) -> LlamaRouter | None:
        """The first managed `llama-router` backend, or None."""
        return next((b for b in self.backends.values() if isinstance(b, LlamaRouter) and b.managed), None)

    def _ollama_backends(self) -> list:  # type: ignore[type-arg]
        """The Ollama backends the router empties before a load (`unload_ollama`)."""
        router = self.router()
        names = router.entry["unload_ollama"] if router else []
        return [self.backends[n] for n in names if n in self.backends and self.backends[n].type == "ollama"]  # type: ignore[attr-defined]

    def models_dir(self) -> Path:
        """The router's model directory (`@MODELS@`); the current directory when unset."""
        configured = str(self.router_entry().get("models_dir") or "")
        return Path(configured).expanduser() if configured else Path()

    def load_models(self) -> None:
        """Reads `models.toml`; a missing file is an empty manifest, an invalid one raises `StartError`."""
        configured = str(self.config["models_file"])
        path = Path(configured).expanduser() if configured else paths.config_dir() / "models.toml"
        models_dir = self.models_dir()
        if not path.exists():
            log.warning("no model manifest at %s; Ollama requests answer 503 model_unverified", path)
            return
        try:
            entries = load_manifest(path, models_dir)
        except ManifestError as exc:
            raise StartError(f"invalid model manifest: {exc}") from exc
        self.manifest.clear()
        self.manifest.update(entries)

    def profile_of(self, model: str) -> str | None:
        """The manifest `profile` of an upstream model name (for `meta.hal_profile`)."""
        entry = self.manifest.get(model)
        return entry.profile if entry else None

    def check_model(self, backend, model: str) -> None:  # type: ignore[no-untyped-def]
        """Refuses an Ollama model that is unpinned or whose tag moved (REQ-GW-77); other backends pass."""
        if backend.type != "ollama":
            return
        name = model if model in self.manifest or ":" in model else f"{model}:latest"
        reported = self._ollama_digests(backend).get(name)
        if reported is None:
            raise ModelUnverified(f"Ollama does not report a digest for {model!r}; check `hal models`")
        try:
            self.verifier.check_ollama(name, reported)
        except ManifestError as exc:
            raise ModelUnverified(f"{exc}; run `hal models` to review and pin it") from exc

    def _ollama_digests(self, backend) -> dict[str, str]:  # type: ignore[no-untyped-def]
        hit = self._digests.get(backend.name)
        if hit and time.monotonic() - hit[0] < DIGEST_TTL_S:
            return hit[1]
        try:
            tags = backend.tags()
        except GatewayError as exc:
            raise Unavailable(f"cannot read the Ollama model digests: {exc.message}") from exc
        digests = {}
        for tag in tags:
            digest = str(tag.get("digest", ""))
            digests[str(tag.get("name", ""))] = digest if digest.startswith("sha256:") else f"sha256:{digest}"
        self._digests[backend.name] = (time.monotonic(), digests)
        return digests

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
        self.load_models()
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
            thread = threading.Thread(
                target=lambda srv=server: srv.serve_forever(POLL_INTERVAL_S), name="hal-gateway", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        log.info("gateway listening on %s", path)
        router = self.router()
        if router is not None:
            router.on_mode = self._set_mode_aliases
            if router.adopt() != "adopted":
                router.peek()
            if router.entry["autostart"] == "with-gateway":
                threading.Thread(
                    target=self._autostart, args=(router,), name="hal-router-start", daemon=True
                ).start()

    def _set_mode_aliases(self, hal) -> None:  # type: ignore[no-untyped-def]
        """Points `hal/main` and `hal/agent` at the models the active mode names."""
        router = self.router()
        aliases = {}
        if hal is not None and router is not None:
            aliases = {"hal/main": (router.name, hal.main), "hal/agent": (router.name, hal.agent)}
        self.resolver.mode_aliases.clear()
        self.resolver.mode_aliases.update(aliases)

    @staticmethod
    def _autostart(router: LlamaRouter) -> None:
        try:
            router.ensure()
        except GatewayError as exc:
            log.error("router autostart failed: %s", exc.message)

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
