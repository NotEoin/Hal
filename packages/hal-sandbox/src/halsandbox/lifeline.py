"""The lifeline socket: when the host side closes it, the box ends itself (02-sandbox D4).

A SIGKILLed host `hal` leaves its `docker run` client dead and the box running when the child
ignores stdin [verified: `orphan.py`]. The box holds this connection open, and the kernel closes
the host end when the host process dies, so the box sees EOF within one read. Protocol, one JSON
object per line: the host sends `{"hal":"lifeline","v":1,"session":"<sid>"}` on accept and may
send `{"hal":"stop","grace_s":N}`; the box sends nothing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import threading
from pathlib import Path

from halkit import paths

from .errors import SandboxError

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
DEFAULT_GRACE_S = 5.0  # REQ-BOX-34: SIGTERM, then SIGKILL after this long
SEND_TIMEOUT_S = 2.0  # a box that stops reading must not block the host's teardown
MAX_CONNECTIONS = 4  # the entry script connects once; more than a few means something is wrong


class Lifeline:
    """Holds the box's lifeline socket; closing it makes the box terminate itself."""

    def __init__(self, path: Path, session_id: str) -> None:
        self.path = paths.check_socket_path(Path(path))
        self.session_id = session_id
        self._server: socket.socket | None = None
        self._conns: list[socket.socket] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False

    def start(self) -> None:
        """Binds the socket (0600) and starts accepting; raises `SandboxError` if already started."""
        if self._server is not None:
            raise SandboxError("lifeline already started")
        self.path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)  # the socket file is created with mode 0600
        try:
            server.bind(str(self.path))
        finally:
            os.umask(old)
        server.listen(MAX_CONNECTIONS)
        self._server = server
        self._thread = threading.Thread(target=self._accept_loop, name="lifeline", daemon=True)
        self._thread.start()

    def connections(self) -> int:
        """How many box connections are held open."""
        with self._lock:
            return len(self._conns)

    def _accept_loop(self) -> None:
        assert self._server is not None
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return  # the listener was closed
            hello = {"hal": "lifeline", "v": PROTOCOL_VERSION, "session": self.session_id}
            conn.settimeout(SEND_TIMEOUT_S)
            with self._lock:
                if self._closed or len(self._conns) >= MAX_CONNECTIONS:
                    conn.close()
                    continue
                self._conns.append(conn)
            self._send(conn, hello)

    def _send(self, conn: socket.socket, message: dict[str, object]) -> None:
        try:
            conn.sendall(json.dumps(message).encode() + b"\n")
        except OSError as exc:
            log.warning("lifeline send failed: %s", exc)

    def request_stop(self, grace_s: float = DEFAULT_GRACE_S) -> None:
        """Asks every connected box to stop its child, SIGKILL after `grace_s`."""
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            self._send(conn, {"hal": "stop", "grace_s": grace_s})

    def close(self) -> None:
        """Closes every connection and the listener and removes the socket file; safe to repeat."""
        with self._lock:
            self._closed = True
            conns, self._conns = self._conns, []
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.close()
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.shutdown(socket.SHUT_RDWR)  # close alone does not wake a blocked accept
            with contextlib.suppress(OSError):
                self._server.close()
            self._server = None
        self.path.unlink(missing_ok=True)
        if self._thread is not None:
            self._thread.join(timeout=SEND_TIMEOUT_S)
