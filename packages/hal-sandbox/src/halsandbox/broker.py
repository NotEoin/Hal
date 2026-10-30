"""Hub broker: serves the hal-hub MCP server on a unix socket, one `hal-hub mcp` process per connection.

Each connection gets its own child, so policy, approvals and audit run in the existing 01-security
code (`halhub`) on the host, never in the box. Children run with `--approvals out-of-band`: a call
that needs approval waits, and the person answers on the host terminal with `hal-hub approve ID`
(the child's warning on stderr names the id). The box cannot answer. The socket is 0600 inside a
0700 directory. An idle or stuck child is killed when the connection closes.

The box is hostile, so the broker treats the connection as untrusted input:
- the peer must have the broker's own uid (`SO_PEERCRED`), else it is dropped before a child starts;
- messages are newline-framed JSON-RPC and only complete lines reach the child, so a half-sent
  frame cannot confuse it; a frame over `MAX_FRAME_BYTES` closes the connection;
- a partial frame that does not finish within `PARTIAL_TIMEOUT_S` closes the connection (an idle
  connection with nothing pending is fine: a call may wait on a human approval);
- at most `MAX_CONNECTIONS` children run at once.
"""

from __future__ import annotations

import logging
import os
import select
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

log = logging.getLogger(__name__)

CHUNK_BYTES = 65536
MAX_FRAME_BYTES = 4 * 1024 * 1024  # largest single MCP message accepted from the box
PARTIAL_TIMEOUT_S = 30.0  # a frame that starts must finish within this time
MAX_CONNECTIONS = 8  # concurrent hub children per session
_PEERCRED = struct.Struct("3i")  # pid, uid, gid
EXIT_WAIT_S = 5.0
DEFAULT_COMMAND = (sys.executable, "-m", "halhub", "mcp", "--approvals", "out-of-band")


class Broker(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """A unix-socket server that splices each connection to a fresh MCP server process."""

    daemon_threads = True

    def __init__(
        self,
        socket_path: Path,
        command: Sequence[str] = DEFAULT_COMMAND,
        *,
        allowed_uid: int | None = None,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        partial_timeout_s: float = PARTIAL_TIMEOUT_S,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        self.command = list(command)
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self.max_frame_bytes = max_frame_bytes
        self.partial_timeout_s = partial_timeout_s
        self.slots = threading.BoundedSemaphore(max_connections)
        self.socket_path = Path(socket_path)
        old_umask = os.umask(0o177)  # the socket file is created 0600
        try:
            super().__init__(str(self.socket_path), _Handler)
        finally:
            os.umask(old_umask)

    def start(self) -> threading.Thread:
        """Serves in a background thread and returns it."""
        self._thread = threading.Thread(target=self.serve_forever, name="hub-broker", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        """Stops serving and removes the socket file."""
        if getattr(self, "_thread", None) is not None:  # shutdown() blocks unless serving
            self.shutdown()
        self.server_close()
        self.socket_path.unlink(missing_ok=True)


class _Handler(socketserver.BaseRequestHandler):
    server: Broker

    def handle(self) -> None:
        if not self._peer_allowed():
            return
        if not self.server.slots.acquire(blocking=False):
            log.warning("broker: too many connections, dropping one")
            return
        try:
            self._serve()
        finally:
            self.server.slots.release()

    def _peer_allowed(self) -> bool:
        raw = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size)
        _, uid, _ = _PEERCRED.unpack(raw)
        if uid != self.server.allowed_uid:
            log.warning("broker: dropped a connection from uid %d", uid)
            return False
        return True

    def _frames(self) -> Iterator[bytes]:
        """Yields complete newline-terminated frames; stops on EOF, an oversize frame or a stalled one."""
        buf = bytearray()
        deadline: float | None = None
        while True:
            wait = None if deadline is None else max(0.0, deadline - time.monotonic())
            if not select.select([self.request], [], [], wait)[0]:
                log.warning(
                    "broker: partial frame not finished in %.0fs, closing", self.server.partial_timeout_s
                )
                return
            data = self.request.recv(CHUNK_BYTES)
            if not data:
                return
            buf += data
            while (end := buf.find(b"\n")) >= 0:
                if end + 1 > self.server.max_frame_bytes:
                    log.warning("broker: frame over %d bytes, closing", self.server.max_frame_bytes)
                    return
                yield bytes(buf[: end + 1])
                del buf[: end + 1]
            if len(buf) > self.server.max_frame_bytes:
                log.warning("broker: frame over %d bytes, closing", self.server.max_frame_bytes)
                return
            deadline = time.monotonic() + self.server.partial_timeout_s if buf else None

    def _serve(self) -> None:
        proc = subprocess.Popen(
            self.server.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None
        )
        assert proc.stdin is not None and proc.stdout is not None
        out = threading.Thread(target=self._pump_out, args=(proc,), daemon=True)
        out.start()
        try:
            for frame in self._frames():
                proc.stdin.write(frame)
                proc.stdin.flush()
        except OSError as exc:
            log.debug("broker connection ended: %s", exc)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=EXIT_WAIT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            out.join(timeout=EXIT_WAIT_S)

    def _pump_out(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        try:
            while data := proc.stdout.read1(CHUNK_BYTES):
                self.request.sendall(data)
        except OSError as exc:
            log.debug("broker reply ended: %s", exc)
        finally:
            try:
                self.request.shutdown(2)
            except OSError:
                pass
