"""Newline-delimited JSON-RPC transports to the boxed agent (REQ-CLI-108).

The box is untrusted, so everything read from it is capped before it is buffered: a line longer
than `ACP_MAX_MESSAGE_BYTES` or JSON nested deeper than `ACP_MAX_JSON_DEPTH` ends the session
with `AgentCrashed`. The agent's stderr goes to a rotating log, never to the terminal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

ACP_MAX_MESSAGE_BYTES = 8 * 2**20  # REQ-CLI-108: far above any agent update, below host memory worries
ACP_MAX_JSON_DEPTH = 64  # REQ-CLI-108: deeper than any ACP schema nests
AGENT_STDERR_MAX_BYTES = 10 * 2**20  # REQ-CLI-108
AGENT_STDERR_BACKUPS = 2  # REQ-CLI-108
CLOSE_TIMEOUT_S = 5.0


class AgentCrashed(Exception):
    """The agent exited, wrote invalid JSON, or broke a transport limit."""


class Transport(Protocol):
    """A duplex channel of JSON-RPC objects."""

    async def send(self, message: Mapping[str, object]) -> None:
        """Writes one message."""
        ...

    async def receive(self) -> dict[str, object] | None:
        """The next message, or None at end of input; raises `AgentCrashed` on a bad one."""
        ...

    async def close(self, timeout: float = CLOSE_TIMEOUT_S) -> None:
        """Ends the channel."""
        ...


def json_depth_ok(line: bytes, limit: int = ACP_MAX_JSON_DEPTH) -> bool:
    """True when brackets outside strings never nest deeper than `limit` (checked before parsing)."""
    depth = 0
    in_string = escaped = False
    for byte in line:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > limit:
                return False
        elif byte in (0x5D, 0x7D):  # ] }
            depth -= 1
    return True


def parse_line(line: bytes) -> dict[str, object]:
    """Decodes one capped line into a JSON object; raises `AgentCrashed` when it is not one."""
    if not json_depth_ok(line):
        raise AgentCrashed(f"agent message nests deeper than {ACP_MAX_JSON_DEPTH} levels")
    try:
        message = json.loads(line)
    except ValueError as exc:
        raise AgentCrashed(f"agent wrote invalid JSON: {exc}") from None
    if not isinstance(message, dict):
        raise AgentCrashed("agent wrote a JSON value that is not an object")
    return message


class StreamTransport:
    """A transport over an asyncio reader and writer; `SubprocessTransport` and tests build on it."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def send(self, message: Mapping[str, object]) -> None:
        """Writes `message` as one ASCII-escaped line."""
        self._writer.write(json.dumps(message).encode("ascii") + b"\n")
        try:
            await self._writer.drain()
        except (ConnectionError, BrokenPipeError) as exc:
            raise AgentCrashed(f"agent closed its input: {exc}") from None

    async def receive(self) -> dict[str, object] | None:
        """The next message, or None at EOF."""
        try:
            line = await self._reader.readline()
        except (asyncio.LimitOverrunError, ValueError):
            raise AgentCrashed(f"agent line longer than {ACP_MAX_MESSAGE_BYTES} bytes") from None
        if not line:
            return None
        return parse_line(line)

    async def close(self, timeout: float = CLOSE_TIMEOUT_S) -> None:
        """Closes the writer."""
        self._writer.close()
        try:
            await asyncio.wait_for(self._writer.wait_closed(), timeout)
        except (TimeoutError, ConnectionError, OSError):
            log.debug("transport did not close cleanly")


class SubprocessTransport(StreamTransport):
    """JSON-RPC on a child's stdin and stdout; its stderr feeds a rotating log."""

    def __init__(self, proc: asyncio.subprocess.Process, stderr_log: Path) -> None:
        assert proc.stdout is not None and proc.stdin is not None
        super().__init__(proc.stdout, proc.stdin)  # type: ignore[arg-type]  # stdin is a StreamWriter
        self.proc = proc
        self._pump = asyncio.ensure_future(_drain_stderr(proc, stderr_log))

    @classmethod
    async def spawn(
        cls,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None,
        stderr_log: Path,
        cwd: str | None = None,
        new_session: bool = False,
    ) -> SubprocessTransport:
        """Starts `argv` with the stdout reader capped at `ACP_MAX_MESSAGE_BYTES`."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=None if env is None else dict(env),
            cwd=cwd,
            start_new_session=new_session,
            limit=ACP_MAX_MESSAGE_BYTES,
        )
        return cls(proc, stderr_log)

    async def close(self, timeout: float = CLOSE_TIMEOUT_S) -> None:
        """Closes stdin, waits `timeout` seconds for exit, then kills the child."""
        await super().close(timeout)
        try:
            await asyncio.wait_for(self.proc.wait(), timeout)
        except TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        await self._pump


async def _drain_stderr(proc: asyncio.subprocess.Process, path: Path) -> None:
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=AGENT_STDERR_MAX_BYTES, backupCount=AGENT_STDERR_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    sink = logging.Logger(f"agent-stderr-{path.name}")
    sink.addHandler(handler)
    sink.propagate = False
    assert proc.stderr is not None
    try:
        while line := await proc.stderr.readline():
            sink.warning(line[:AGENT_STDERR_MAX_BYTES].decode("utf-8", "replace").rstrip("\n"))
    except (asyncio.LimitOverrunError, ValueError):
        log.warning("agent stderr line over the cap; log stopped")
    finally:
        handler.close()


async def stdio_transport() -> StreamTransport:
    """The process's own stdin and stdout as a transport (the editor side of `hal acp`)."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=ACP_MAX_MESSAGE_BYTES)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    wtransport, wprotocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout)
    return StreamTransport(reader, asyncio.StreamWriter(wtransport, wprotocol, reader, loop))
