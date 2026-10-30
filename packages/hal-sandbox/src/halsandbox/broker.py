"""Hub broker: serves the hal-hub MCP server on a unix socket, one `hal-hub mcp` process per connection.

Each connection gets its own child, so policy, approvals and audit run in the existing 01-security
code (`halhub`) on the host, never in the box. Children run with `--approvals out-of-band`: a call
that needs approval waits, and the person answers on the host terminal with `hal-hub approve ID`
(the child's warning on stderr names the id). The box cannot answer. The socket is 0600 inside a
0700 directory. An idle or stuck child is killed when the connection closes.

The box is hostile, so the broker treats the connection as untrusted input:
The framing, peer-credential check (uid, and container cgroup on runc), 4 MiB cap, partial-message
timeout and connection cap all live in `halsandbox.transport.LineServer`; this module only
splices complete messages to a child and back. A child reply over the cap closes the connection.

The lockdown level's paranoia floor reaches the children in `HAL_PARANOIA_FLOOR`, set by the host
after the inherited environment so nothing upstream can lower it.

Human messages reach the children on a private pipe (`HAL_HUMAN_FD`), never on the socket the box
can write to; see `halhub.humanfeed`. `Broker.on_human_message` is the host's entry point. A new
child gets the last message replayed, so a connection opened after the prompt still knows it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from .transport import (
    MAX_CONNECTIONS,
    MAX_MESSAGE_BYTES,
    PARTIAL_TIMEOUT_S,
    LineConnection,
    LineServer,
    MessageTooLarge,
    PeerRule,
)

log = logging.getLogger(__name__)

MAX_FRAME_BYTES = MAX_MESSAGE_BYTES  # one cap for both directions, owned by the transport
EXIT_WAIT_S = 5.0
PARANOIA_FLOOR_ENV = "HAL_PARANOIA_FLOOR"  # same name as `halhub.classifier.FLOOR_ENV`
HUMAN_FD_ENV = "HAL_HUMAN_FD"  # same name as `halhub.humanfeed.ENV_FD`; the sandbox does not import halhub
SURFACES = frozenset({"tui", "acp", "telegram", "voice", "console", "headless", "routine"})  # REQ-SEC-93
DEFAULT_COMMAND = (sys.executable, "-m", "halhub", "mcp", "--approvals", "out-of-band")


def upstream_command(upstreams_file: Path, base: Sequence[str] = DEFAULT_COMMAND) -> list[str]:
    """`base` (a `python -m halhub mcp ...` command) rewritten to run under `halsandbox.hubchild`."""
    cmd = list(base)
    if cmd[1:3] != ["-m", "halhub"]:
        raise ValueError(f"cannot add upstreams to {cmd!r}: expected `<python> -m halhub ...`")
    return [cmd[0], "-m", "halsandbox.hubchild", str(upstreams_file), "--", *cmd[3:]]


class Broker:
    """A unix-socket server that splices each connection to a fresh MCP server process."""

    def __init__(
        self,
        socket_path: Path,
        command: Sequence[str] = DEFAULT_COMMAND,
        *,
        allowed_uid: int | None = None,
        container_id: str | None = None,
        paranoia_floor: str = "off",
        max_frame_bytes: int = MAX_FRAME_BYTES,
        partial_timeout_s: float = PARTIAL_TIMEOUT_S,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        self.command = list(command)
        self.paranoia_floor = paranoia_floor
        self._feeds: set[int] = set()  # write ends of the children's human-message pipes
        self._feed_lock = threading.Lock()
        self._last_line: bytes | None = None
        self.socket_path = Path(socket_path)
        self._server = LineServer(
            self.socket_path,
            self._serve,
            peer=PeerRule(os.getuid() if allowed_uid is None else allowed_uid, container_id),
            max_message_bytes=max_frame_bytes,
            max_connections=max_connections,
            partial_timeout_s=partial_timeout_s,
        )

    def start(self) -> None:
        """Binds the socket and serves in background threads."""
        self._server.start()

    def on_human_message(self, text: str, surface: str) -> None:
        """Tells every hub child what the person said, before the agent sees it (REQ-SEC-93).

        Raises `ValueError` for an unknown `surface`. A child that cannot take the line (pipe full
        or closed) is skipped and logged: its session stays at its current taint, which is the
        safe side.
        """
        if surface not in SURFACES:
            raise ValueError(f"unknown surface {surface!r}; use one of {sorted(SURFACES)}")
        line = json.dumps({"text": text, "surface": surface}).encode() + b"\n"
        with self._feed_lock:
            self._last_line = line
            for fd in tuple(self._feeds):
                self._write_feed(fd, line)

    def _write_feed(self, fd: int, line: bytes) -> None:
        try:
            os.write(fd, line)
        except OSError as exc:  # BlockingIOError (full) or EPIPE (child gone)
            log.warning("human message not delivered to a hub child: %s", exc)

    def stop(self) -> None:
        """Stops serving, closes every connection and removes the socket file."""
        self._server.close()

    def _serve(self, conn: LineConnection) -> None:
        feed_r, feed_w = os.pipe()
        os.set_blocking(feed_w, False)  # a stuck child must not stall the host's prompt
        env = {**os.environ, HUMAN_FD_ENV: str(feed_r), PARANOIA_FLOOR_ENV: self.paranoia_floor}
        try:
            proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                env=env,
                pass_fds=(feed_r,),
            )
        except BaseException:
            os.close(feed_w)
            raise
        finally:
            os.close(feed_r)
        with self._feed_lock:
            self._feeds.add(feed_w)
            if self._last_line is not None:
                self._write_feed(feed_w, self._last_line)
        assert proc.stdin is not None and proc.stdout is not None
        out = threading.Thread(target=self._pump_out, args=(conn, proc), daemon=True)
        out.start()
        try:
            while (msg := conn.read_message()) is not None:
                proc.stdin.write(msg + b"\n")
                proc.stdin.flush()
        except OSError as exc:
            log.debug("broker connection ended: %s", exc)
        finally:
            with self._feed_lock:
                self._feeds.discard(feed_w)
            os.close(feed_w)
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

    def _pump_out(self, conn: LineConnection, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        try:
            # one extra byte so a reply over the cap is seen as over, not silently cut
            while line := proc.stdout.readline(conn.max_message_bytes + 2):
                conn.write_message(line.rstrip(b"\n"))
        except MessageTooLarge as exc:
            log.warning("broker: dropping a connection, child reply too large: %s", exc)
        except (OSError, ValueError) as exc:
            log.debug("broker reply ended: %s", exc)
        finally:
            conn.close()
