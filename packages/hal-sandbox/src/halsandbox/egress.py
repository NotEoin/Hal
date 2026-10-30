"""HTTP CONNECT proxy on a unix socket for the dependency-prep box (REQ-BOX-72).

The prep box has no network. Its package managers reach a registry through `hal-relay` and this
proxy, which runs on the host. Constraints, each with its reason:

- Only `CONNECT <registry>:443`: plain HTTP to a registry would let a proxy-side attacker swap
  files, and any other host is a data-exfiltration route for install scripts.
- The host must equal a listed registry name exactly. A suffix match would allow
  `evil.pypi.org.example`-style tricks and subdomain takeovers.
- The host is resolved here, every address must be global (`halkit.netguard`), and the tunnel
  connects to the checked address, so a DNS answer cannot change between check and connect.
- TLS stays end to end. The proxy never terminates it and holds no CA, so integrity comes from
  TLS plus the hashes in the lockfiles.
- Every connection is bounded: a head timeout, an idle timeout and a tunnel cap.
"""

from __future__ import annotations

import logging
import os
import select
import socket
import threading
import time
from collections.abc import Callable, Collection
from pathlib import Path

from halkit import netguard

log = logging.getLogger("halsandbox.egress")

DEFAULT_REGISTRIES = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "index.crates.io",
    "static.crates.io",
    "crates.io",
)
CONNECT_PORT = 443  # TLS only
HEAD_TIMEOUT_S = 10.0  # a client that cannot send a CONNECT line in 10 s is not a package manager
IDLE_TIMEOUT_S = 120.0  # large wheels stream for minutes but never stall that long
CONNECT_TIMEOUT_S = 15.0
MAX_HEAD_BYTES = 8192
CHUNK_BYTES = 65536
DEFAULT_MAX_TUNNELS = 16
Resolver = Callable[[str], list[str]]
Connector = Callable[[str, int], socket.socket]


def _connect(address: str, port: int) -> socket.socket:
    return socket.create_connection((address, port), timeout=CONNECT_TIMEOUT_S)


class ConnectProxy:
    """HTTP CONNECT proxy on a unix socket that tunnels only to allowlisted registries."""

    def __init__(
        self,
        path: Path,
        *,
        registries: Collection[str],
        on_event: Callable[[dict[str, object]], None],
        max_tunnels: int = DEFAULT_MAX_TUNNELS,
        resolve: Resolver | None = None,
        connect: Connector = _connect,
        head_timeout_s: float = HEAD_TIMEOUT_S,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
    ) -> None:
        self.path = Path(path)
        self.registries = frozenset(r.strip().rstrip(".").lower() for r in registries)
        self.denied: list[str] = []
        self._on_event = on_event
        self._resolve = resolve
        self._connect = connect
        self._head_timeout = head_timeout_s
        self._idle_timeout = idle_timeout_s
        self._slots = threading.BoundedSemaphore(max_tunnels)
        self._server: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._closing = threading.Event()

    def start(self) -> None:
        """Binds the socket (0600) and serves in background threads."""
        self.path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)  # the socket is created 0600, with no window where it is wider
        try:
            server.bind(str(self.path))
        finally:
            os.umask(old)
        server.listen(16)
        server.settimeout(0.2)
        self._server = server
        thread = threading.Thread(target=self._accept_loop, name="egress-accept", daemon=True)
        thread.start()
        self._threads.append(thread)

    def close(self, timeout_s: float = 5.0) -> None:
        """Stops accepting, ends the tunnels and removes the socket file."""
        self._closing.set()
        if self._server is not None:
            self._server.close()
        deadline = time.monotonic() + timeout_s
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        self.path.unlink(missing_ok=True)

    def _event(self, name: str, **fields: object) -> None:
        try:
            self._on_event({"event": name, **fields})
        except Exception:  # noqa: BLE001 - a broken audit sink must not open or close a tunnel
            log.exception("egress event sink failed")

    def _deny(self, client: socket.socket, status: str, host: str, reason: str) -> None:
        self.denied.append(host)
        self._event("egress.deny", host=host, reason=reason)
        log.warning("egress denied %s: %s", host or "(none)", reason)
        try:
            client.sendall(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        except OSError:
            pass

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._closing.is_set():
            try:
                client, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self._handle, args=(client,), name="egress-conn", daemon=True)
            thread.start()
            self._threads = [t for t in self._threads if t.is_alive()] + [thread]

    def _read_head(self, client: socket.socket) -> tuple[bytes, bytes] | None:
        """The request head and any bytes after it; None when it is late, too long or cut off."""
        deadline = time.monotonic() + self._head_timeout
        buf = b""
        while b"\r\n\r\n" not in buf:
            left = deadline - time.monotonic()
            if left <= 0 or len(buf) > MAX_HEAD_BYTES:
                return None
            client.settimeout(left)
            try:
                chunk = client.recv(CHUNK_BYTES)
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head, rest

    def _handle(self, client: socket.socket) -> None:
        try:
            got = self._read_head(client)
            if got is None:
                self._deny(client, "408 Request Timeout", "", "no complete request head")
                return
            head, rest = got
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            parts = line.split(" ")
            if len(parts) != 3 or parts[0] != "CONNECT" or parts[2] not in ("HTTP/1.1", "HTTP/1.0"):
                self._deny(client, "403 Forbidden", line[:100], "only CONNECT host:443 is allowed")
                return
            host, _, port = parts[1].rpartition(":")
            host = host.lower().rstrip(".")
            if port != str(CONNECT_PORT) or host not in self.registries:
                self._deny(client, "403 Forbidden", parts[1][:100], "not a listed registry on port 443")
                return
            try:
                address = netguard.resolve_checked(host, resolve=self._resolve)
            except (netguard.BlockedURL, OSError) as exc:
                self._deny(client, "403 Forbidden", host, f"address refused: {exc}")
                return
            if not self._slots.acquire(blocking=False):
                self._deny(client, "503 Service Unavailable", host, "too many tunnels")
                return
            try:
                self._tunnel(client, host, address, rest)
            finally:
                self._slots.release()
        finally:
            client.close()

    def _tunnel(self, client: socket.socket, host: str, address: str, rest: bytes) -> None:
        try:
            upstream = self._connect(address, CONNECT_PORT)
        except OSError as exc:
            self._deny(client, "502 Bad Gateway", host, f"connect failed: {exc}")
            return
        self._event("egress.allow", host=host, address=address)
        try:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            self._splice(client, upstream)
        except OSError as exc:
            log.debug("egress tunnel to %s ended: %s", host, exc)
        finally:
            upstream.close()

    def _splice(self, a: socket.socket, b: socket.socket) -> None:
        a.settimeout(None)
        b.settimeout(None)
        pair = {a: b, b: a}
        while not self._closing.is_set():
            ready, _, _ = select.select(list(pair), [], [], self._idle_timeout)
            if not ready:
                return  # idle for too long
            for src in ready:
                data = src.recv(CHUNK_BYTES)
                if not data:
                    return
                pair[src].sendall(data)
