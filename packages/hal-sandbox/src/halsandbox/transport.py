"""Line-framed unix socket server for the broker (REQ-BOX-29 to 31, REQ-BOX-65).

Carries MCP stdio framing over a unix socket: one UTF-8 JSON-RPC object per line. The box is
hostile, so the transport enforces what a peer must not be trusted with, and nothing else:

- `SO_PEERCRED` uid (and on runc the container id in `/proc/<pid>/cgroup`) is checked on accept;
- a message over `max_message_bytes` gets one `-32600` error frame, then the connection closes;
- a partial message older than `partial_timeout_s` is a slowloris and closes the connection; an
  idle connection with nothing pending is fine, because a call may wait on a human approval;
- connections past `max_connections` get an error frame and are closed.

It never inspects or changes a complete message (REQ-BOX-31): policy lives in the broker.
Threads, not anyio: the handlers block on child processes, and the adapter to the SDK's
streams (`anyio_streams`) is left to the hub-side wiring.
"""

from __future__ import annotations

import json
import logging
import os
import select
import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import SandboxError

log = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 4 * 2**20  # the request cap of the `mcp` 2.x SDK (REQ-BOX-29)
MAX_CONNECTIONS = 8
PARTIAL_TIMEOUT_S = 30.0  # no legitimate client takes 30 s to write one line
CHUNK_BYTES = 65536
JOIN_TIMEOUT_S = 5.0
ERR_INVALID_REQUEST = -32600  # JSON-RPC "Invalid Request"
ERR_SERVER_BUSY = -32000  # JSON-RPC implementation-defined server error range
_PEERCRED = struct.Struct("3i")  # pid, uid, gid
Cred = tuple[int, int, int]


class TransportError(SandboxError):
    """A connection broke the framing rules; the server answers with an error frame and closes."""

    code = ERR_INVALID_REQUEST


class MessageTooLarge(TransportError):
    """A message went over `max_message_bytes`."""


class PartialTimeout(TransportError):
    """A message was started and not finished in time."""


@dataclass(frozen=True)
class PeerRule:
    """Who may connect: the expected uid, and for runc the container id the peer's cgroup must name."""

    uid: int
    container_id: str | None = None


def error_frame(code: int, message: str) -> bytes:
    """A JSON-RPC error with `id: null`, newline-terminated."""
    body = {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}
    return json.dumps(body).encode() + b"\n"


def _socket_cred(sock: socket.socket) -> Cred:
    return _PEERCRED.unpack(sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size))


def _in_container(pid: int, container_id: str) -> bool:
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return False
    return container_id in text


class LineConnection:
    """One accepted client; reads and writes whole newline-framed messages."""

    def __init__(self, sock: socket.socket, conn_id: int, peer: Cred, *, max_message_bytes: int,
                 partial_timeout_s: float) -> None:  # fmt: skip
        self.sock = sock
        self.conn_id = conn_id
        self.peer = peer
        self.max_message_bytes = max_message_bytes
        self.partial_timeout_s = partial_timeout_s
        self._buf = bytearray()
        self._wlock = threading.Lock()
        self._closed = False

    def read_message(self) -> bytes | None:
        """The next message without its newline, or None at EOF.

        Raises `MessageTooLarge` or `PartialTimeout`. Bytes after an unterminated last message
        at EOF are dropped, never forwarded.
        """
        deadline: float | None = None
        while True:
            end = self._buf.find(b"\n")
            if end >= 0:
                if end > self.max_message_bytes:
                    raise MessageTooLarge(f"message exceeds {self.max_message_bytes} bytes")
                msg = bytes(self._buf[:end])
                del self._buf[: end + 1]
                return msg
            if len(self._buf) > self.max_message_bytes:
                raise MessageTooLarge(f"message exceeds {self.max_message_bytes} bytes")
            if self._buf and deadline is None:
                deadline = time.monotonic() + self.partial_timeout_s
            wait = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                ready = select.select([self.sock], [], [], wait)[0]
                if not ready:
                    raise PartialTimeout(f"partial message not finished in {self.partial_timeout_s:g}s")
                data = self.sock.recv(CHUNK_BYTES)
            except (OSError, ValueError):  # closed under us
                return None
            if not data:
                return None
            self._buf += data

    def write_message(self, data: bytes) -> None:
        """Sends one message and its newline; raises `MessageTooLarge` or `ValueError` for bad input."""
        if len(data) > self.max_message_bytes:
            raise MessageTooLarge(f"message exceeds {self.max_message_bytes} bytes")
        if b"\n" in data:
            raise ValueError("a framed message must not contain a newline")
        with self._wlock:
            self.sock.sendall(data + b"\n")

    def write_raw(self, data: bytes) -> None:
        """Sends already framed bytes (an error frame); serialised with `write_message`."""
        with self._wlock:
            self.sock.sendall(data)

    def close(self) -> None:
        """Closes the socket; safe to call twice and from another thread."""
        if self._closed:
            return
        self._closed = True
        for step in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                step()
            except OSError:
                pass


class LineServer:
    """Accepts clients on a unix socket and runs `handler` per connection in a thread."""

    def __init__(self, path: Path, handler: Callable[[LineConnection], None], *, peer: PeerRule,
                 max_message_bytes: int = MAX_MESSAGE_BYTES, max_connections: int = MAX_CONNECTIONS,
                 partial_timeout_s: float = PARTIAL_TIMEOUT_S,
                 peer_cred: Callable[[socket.socket], Cred] = _socket_cred) -> None:  # fmt: skip
        self.path = Path(path)
        self.handler = handler
        self.peer = peer
        self.max_message_bytes = max_message_bytes
        self.max_connections = max_connections
        self.partial_timeout_s = partial_timeout_s
        self._peer_cred = peer_cred
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._live: dict[int, LineConnection] = {}
        self._next_id = 1
        self._stopping = False

    def start(self) -> None:
        """Binds with mode 0600, listens and starts the accept thread."""
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)  # the socket file is created 0600
        try:
            listener.bind(str(self.path))
        finally:
            os.umask(old_umask)
        listener.listen(self.max_connections + 1)
        self._listener = listener
        self._thread = threading.Thread(target=self._accept_loop, name="line-server", daemon=True)
        self._thread.start()

    def close(self, timeout_s: float = JOIN_TIMEOUT_S) -> None:
        """Stops accepting, closes every connection and removes the socket file."""
        self._stopping = True
        if self._listener is not None:
            try:
                self._listener.shutdown(socket.SHUT_RDWR)  # wakes the accept thread
            except OSError:
                pass
            self._listener.close()
        with self._lock:
            live = list(self._live.values())
        for conn in live:
            conn.close()
        if self._thread is not None:
            self._thread.join(timeout_s)
        self.path.unlink(missing_ok=True)

    def connections(self) -> int:
        """The number of live connections."""
        with self._lock:
            return len(self._live)

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stopping:
            try:
                sock, _ = self._listener.accept()
            except OSError:
                return
            try:
                self._admit(sock)
            except Exception:  # noqa: BLE001 - one bad client must not stop the accept loop; logged
                log.exception("transport: admitting a connection failed")
                sock.close()

    def _peer_allowed(self, cred: Cred) -> bool:
        pid, uid, _gid = cred
        if uid != self.peer.uid:
            log.warning("transport: dropped a connection from uid %d (pid %d)", uid, pid)
            return False
        if self.peer.container_id and not _in_container(pid, self.peer.container_id):
            log.warning("transport: dropped pid %d, not in container %s", pid, self.peer.container_id[:12])
            return False
        return True

    def _admit(self, sock: socket.socket) -> None:
        cred = self._peer_cred(sock)
        if not self._peer_allowed(cred):
            sock.close()
            return
        with self._lock:
            full = len(self._live) >= self.max_connections
            conn_id = self._next_id
            self._next_id += 1
            conn = LineConnection(sock, conn_id, cred, max_message_bytes=self.max_message_bytes,
                                  partial_timeout_s=self.partial_timeout_s)  # fmt: skip
            if not full:
                self._live[conn_id] = conn
        if full:
            log.warning("transport: connection cap %d reached, refusing one", self.max_connections)
            self._refuse(conn, ERR_SERVER_BUSY, "too many connections")
            return
        threading.Thread(target=self._run, args=(conn,), name=f"line-conn-{conn_id}", daemon=True).start()

    def _refuse(self, conn: LineConnection, code: int, message: str) -> None:
        try:
            conn.write_raw(error_frame(code, message))
        except OSError:
            pass
        conn.close()

    def _run(self, conn: LineConnection) -> None:
        try:
            self.handler(conn)
        except TransportError as exc:
            log.warning("transport: connection %d closed: %s", conn.conn_id, exc)
            self._refuse(conn, exc.code, str(exc))
        except OSError as exc:
            log.debug("transport: connection %d ended: %s", conn.conn_id, exc)
        except Exception:  # noqa: BLE001 - a handler bug closes its own connection only; logged
            log.exception("transport: handler for connection %d failed", conn.conn_id)
        finally:
            conn.close()
            with self._lock:
                self._live.pop(conn.conn_id, None)
