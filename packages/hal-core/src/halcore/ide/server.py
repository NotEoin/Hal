"""The per-session IDE socket: `<runtime>/ide/<session>.sock` plus a lock file naming the workspace.

Security design: the socket lives on the host, never in the box. The directory is 0700 and the
socket 0600; each connection's peer uid must equal ours (`SO_PEERCRED`); messages are one JSON
line, capped at 1 MiB, with a read timeout; unknown types and paths outside the workspace are
refused (`protocol.parse`). The server only forwards validated `Message` objects to a callback.
It never executes, writes or reads anything on behalf of the editor.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path

from halkit import paths

from .protocol import MAX_MESSAGE_BYTES, IdeRefused, Message, encode, parse

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 300.0  # a quiet editor is normal; a stalled half-sent line is not
HELLO_TIMEOUT_S = 5.0  # the first message must come promptly
SEND_TIMEOUT_S = 3.0  # a stuck editor must not stall the agent turn that is publishing to it
MAX_CLIENTS = 8  # one session has a handful of editors at most; more is a runaway or hostile loop
SOCKET_MODE = 0o600
PEERCRED_FORMAT = "3i"  # struct ucred: pid, uid, gid
Handler = Callable[[Message], Awaitable[None]]


def ide_dir() -> Path:
    """The private `ide` directory below the runtime directory (0700, created on demand)."""
    return paths.ensure_private_dir(paths.runtime_dir() / "ide")


def peer_uid(sock: socket.socket) -> int:
    """The uid of the process on the other end of a connected Unix socket."""
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(PEERCRED_FORMAT))
    return struct.unpack(PEERCRED_FORMAT, raw)[1]


def find_session(workspace: Path, directory: Path | None = None) -> dict[str, object] | None:
    """The lock record of a live session for `workspace`, or None (what the extension does too)."""
    root = directory or ide_dir()
    want = os.path.realpath(workspace)
    for lock in sorted(root.glob("*.lock")):
        try:
            info = json.loads(lock.read_text(encoding="ascii"))
        except (OSError, ValueError):
            continue
        if isinstance(info, dict) and info.get("workspace") == want and Path(str(info.get("socket"))).exists():
            return info
    return None


class IdeServer:
    """Accepts editor connections for one `hal code` session and hands valid messages to `handler`."""

    def __init__(self, session: str, workspace: Path, handler: Handler, *, directory: Path | None = None) -> None:
        self.session = session
        self.workspace = Path(os.path.realpath(workspace))
        self.handler = handler
        self.directory = directory
        self.sock_path: Path | None = None
        self.lock_path: Path | None = None
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> Path:
        """Creates the socket and lock file; returns the socket path."""
        if not paths.SESSION_ID_RE.fullmatch(self.session):
            raise ValueError(f"bad session id {self.session!r}")
        root = paths.ensure_private_dir(self.directory) if self.directory else ide_dir()
        self.sock_path = root / f"{self.session}.sock"
        self.lock_path = root / f"{self.session}.lock"
        self.sock_path.unlink(missing_ok=True)
        old_umask = os.umask(0o177)  # the socket is born 0600, with no window of wider access
        try:
            self._server = await asyncio.start_unix_server(self._client, str(self.sock_path), limit=MAX_MESSAGE_BYTES)
        finally:
            os.umask(old_umask)
        os.chmod(self.sock_path, SOCKET_MODE)
        info = {"session": self.session, "workspace": str(self.workspace), "socket": str(self.sock_path), "pid": os.getpid()}
        fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, SOCKET_MODE)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            json.dump(info, fh)
        return self.sock_path

    async def send(self, message: dict[str, object]) -> None:
        """Sends a host message to every connected editor."""
        data = encode(message)
        for writer in list(self._writers):
            try:
                writer.write(data)
                await asyncio.wait_for(writer.drain(), SEND_TIMEOUT_S)
            except (TimeoutError, OSError):
                self._writers.discard(writer)

    @property
    def connected(self) -> bool:
        """True while at least one editor is connected."""
        return bool(self._writers)

    async def close(self) -> None:
        """Stops accepting, drops clients and removes the socket and lock file."""
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        for path in (self.sock_path, self.lock_path):
            if path is not None:
                path.unlink(missing_ok=True)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        try:
            if sock is None or peer_uid(sock) != os.getuid():
                log.warning("ide: refused a connection from another user")
                return
            if len(self._writers) >= MAX_CLIENTS:
                log.warning("ide: refused a connection, %d editors are already connected", MAX_CLIENTS)
                await self._reply_error(writer, "too many editor connections")
                return
            self._writers.add(writer)
            first = True
            while True:
                limit = HELLO_TIMEOUT_S if first else READ_TIMEOUT_S
                try:
                    line = await asyncio.wait_for(reader.readline(), limit)
                except (TimeoutError, ValueError, asyncio.LimitOverrunError):
                    log.warning("ide: closed a client (timeout or oversized line)")
                    return
                if not line:
                    return
                try:
                    msg = parse(line, self.workspace)
                    if first and msg.type != "hello":
                        raise IdeRefused("the first message must be hello")
                except IdeRefused as exc:
                    log.warning("ide: refused a message: %s", exc)
                    await self._reply_error(writer, str(exc))
                    return
                first = False
                if msg.type == "hello":
                    writer.write(encode({"type": "welcome", "session": self.session, "workspace": str(self.workspace)}))
                    continue
                await self.handler(msg)
        except OSError:
            return
        finally:
            self._writers.discard(writer)
            with contextlib.suppress(OSError):
                writer.close()

    @staticmethod
    async def _reply_error(writer: asyncio.StreamWriter, text: str) -> None:
        with contextlib.suppress(OSError):
            writer.write(encode({"type": "error", "message": text}))
            await writer.drain()
