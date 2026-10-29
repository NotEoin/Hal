"""Hub broker: serves the hal-hub MCP server on a unix socket, one `hal-hub mcp` process per connection.

Each connection gets its own child, so policy, approvals and audit run in the existing 01-security
code (`halhub`) on the host, never in the box. Children run with `--approvals out-of-band`: a call
that needs approval waits, and the person answers on the host terminal with `hal-hub approve ID`
(the child's warning on stderr names the id). The box cannot answer. The socket is 0600 inside a
0700 directory. An idle or stuck child is killed when the connection closes.
"""

from __future__ import annotations

import logging
import os
import socketserver
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

log = logging.getLogger(__name__)

CHUNK_BYTES = 65536
EXIT_WAIT_S = 5.0
DEFAULT_COMMAND = (sys.executable, "-m", "halhub", "mcp", "--approvals", "out-of-band")


class Broker(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """A unix-socket server that splices each connection to a fresh MCP server process."""

    daemon_threads = True

    def __init__(self, socket_path: Path, command: Sequence[str] = DEFAULT_COMMAND) -> None:
        self.command = list(command)
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
        proc = subprocess.Popen(
            self.server.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None
        )
        assert proc.stdin is not None and proc.stdout is not None
        out = threading.Thread(target=self._pump_out, args=(proc,), daemon=True)
        out.start()
        try:
            while data := self.request.recv(CHUNK_BYTES):
                proc.stdin.write(data)
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
