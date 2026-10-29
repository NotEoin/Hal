"""The guard relay: owns `runtime_dir()/guard.sock` and forwards scans to one sandboxed worker.

01-security "Taint, guard and reviewer". The user unit `hal-guard.service` (06-core-cli renders
it) runs `python -m halhub.guard.serve`. The worker command line comes from
`state_dir()/guard/worker.json`, which `hal guard install` writes: a docker run of the guard
image with the REQ-SEC-60 flags, or a bubblewrap command over a venv.

Design decisions:

- The socket is created here on the host, mode 0600 in the 0700 runtime directory, and a peer
  with another uid is closed before a byte is read. No box or runtime has to create a socket
  in a bind mount, and `halhub` never imports `halsandbox` (A4).
- One worker, one request at a time: the model uses every thread it is given, so parallel
  requests would only compete. Requests queue on a lock.
- The worker is started when the relay starts, because importing torch takes seconds and the
  first scan would otherwise time out. A worker that misses its deadline, dies or writes
  garbage is killed with its process group and started again; the request gets an `error`
  reply, which the client treats as a flag (REQ-SEC-35).
- Replies are passed through only when they are one JSON object answering the request's id.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from halkit import log as halkit_log
from halkit import paths
from halkit.config import ConfigError

from ..approvals.control import Peer, peer_credentials
from ..policy import load_policy
from .client import PROTOCOL_VERSION, GuardUnavailable, socket_path

log = logging.getLogger(__name__)

WORKER_FILE = Path("guard") / "worker.json"  # under state_dir(), written by `hal guard install`
MAX_REQUEST_BYTES = 1 << 20  # the client caps texts at max_scan_chars; JSON escaping can grow it
MAX_REPLY_BYTES = 64 * 1024
BACKLOG = 16
ACCEPT_POLL_S = 0.5  # how often the accept loop looks at the stop flag
READ_TIMEOUT_S = 10.0  # a client must send its request line within this
KILL_WAIT_S = 5.0  # how long a killed worker gets to be reaped
EXIT_OK = 0
EXIT_UNAVAILABLE = 2


def load_worker(path: Path | None = None) -> tuple[str, ...]:
    """The worker argv from `worker.json`; raises `GuardUnavailable` naming the problem."""
    path = path if path is not None else paths.state_dir() / WORKER_FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GuardUnavailable(f"{path} does not exist; run `hal guard install`") from exc
    except (OSError, ValueError) as exc:
        raise GuardUnavailable(f"cannot read {path}: {exc}") from exc
    argv = record.get("argv") if isinstance(record, dict) else None
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise GuardUnavailable(f"{path}: `argv` must be a non-empty list of strings")
    return tuple(argv)


class Relay:
    """Serves the guard socket and relays each request line to the worker started from `argv`."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        path: Path | None = None,
        peer: Callable[[socket.socket], Peer] = peer_credentials,
    ) -> None:
        self.argv = tuple(argv)
        self.timeout_s = timeout_s
        self.path = path
        self._peer = peer
        self._worker: subprocess.Popen[bytes] | None = None
        self._buffer = b""
        self._work_lock = threading.Lock()
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> Path:
        """Starts the worker and binds the socket; raises `GuardUnavailable` if it is in use."""
        path = paths.check_socket_path(self.path if self.path is not None else socket_path())
        if path.exists():
            if _answers(path):
                raise GuardUnavailable(f"another guard relay is serving {path}")
            path.unlink()  # stale: left by a relay that did not shut down
        with self._work_lock:
            self._spawn()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chmod(path, 0o600)  # the 0700 runtime directory keeps others out while this runs
        listener.listen(BACKLOG)
        listener.settimeout(ACCEPT_POLL_S)
        self._listener, self.path = listener, path
        self._thread = threading.Thread(target=self._serve, name="hal-guard-relay", daemon=True)
        self._thread.start()
        log.info("guard relay at %s, worker %s", path, self.argv[0])
        return path

    def close(self) -> None:
        """Stops serving, removes the socket and stops the worker."""
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
        if self._thread is not None:
            self._thread.join(ACCEPT_POLL_S * 4)
        if self.path is not None:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
        with self._work_lock:
            self._kill()

    def __enter__(self) -> Relay:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return  # closed under us
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                peer = self._peer(conn)
            except OSError as exc:
                log.warning("guard relay: cannot read peer credentials: %s", exc)
                return
            if peer.uid != os.getuid():
                log.warning("guard relay: refused a peer with uid %d", peer.uid)
                return
            conn.settimeout(READ_TIMEOUT_S)
            try:
                line = conn.makefile("rb").readline(MAX_REQUEST_BYTES + 1)
                reply = self.relay(line)
                conn.sendall(json.dumps(reply, separators=(",", ":")).encode("ascii") + b"\n")
            except (OSError, TimeoutError) as exc:
                log.warning("guard relay: connection failed: %s", exc)

    def relay(self, line: bytes) -> dict[str, object]:
        """The worker's reply to one request line, or an `error` reply; never raises."""
        request_id: object = None
        try:
            if len(line) > MAX_REQUEST_BYTES:
                raise ValueError(f"request is over {MAX_REQUEST_BYTES} bytes")
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("v") != PROTOCOL_VERSION:
                raise ValueError(f"expected a protocol {PROTOCOL_VERSION} object")
            request_id = request.get("id")
            if not isinstance(request_id, str):
                raise ValueError("id must be a string")
        except ValueError as exc:
            return {"v": PROTOCOL_VERSION, "id": request_id, "error": f"bad request: {exc}"}
        with self._work_lock:
            try:
                reply = self._ask(json.dumps(request, separators=(",", ":")).encode("ascii") + b"\n")
            except (OSError, TimeoutError, ValueError) as exc:
                log.error("guard worker failed (%s); restarting it", exc)
                self._kill()
                self._spawn_quietly()
                return {"v": PROTOCOL_VERSION, "id": request_id, "error": f"worker failed: {exc}"}
        if reply.get("id") != request_id:
            return {"v": PROTOCOL_VERSION, "id": request_id, "error": "worker answered another request"}
        return reply

    def _ask(self, payload: bytes) -> dict[str, object]:
        """Sends one line to the worker and reads its reply line before the deadline."""
        worker = self._worker
        if worker is None or worker.poll() is not None:
            self._kill()
            self._spawn()
            worker = self._worker
        assert worker is not None and worker.stdin is not None and worker.stdout is not None
        worker.stdin.write(payload)
        worker.stdin.flush()
        deadline = time.monotonic() + self.timeout_s
        fd = worker.stdout.fileno()
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no reply within {self.timeout_s} s")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                raise ConnectionError("the worker exited")
            self._buffer += chunk
            if len(self._buffer) > MAX_REPLY_BYTES:
                raise ValueError(f"worker reply is over {MAX_REPLY_BYTES} bytes")
        line, self._buffer = self._buffer.split(b"\n", 1)
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise ValueError("worker reply is not a JSON object")
        return reply

    def _spawn(self) -> None:
        self._buffer = b""
        # argv comes from worker.json, which only `hal guard install` and the user write
        self._worker = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            start_new_session=True,  # its own process group, so a kill reaches docker run's children too
        )

    def _spawn_quietly(self) -> None:
        try:
            self._spawn()
        except OSError as exc:
            log.error("guard worker could not be restarted: %s", exc)
            self._worker = None

    def _kill(self) -> None:
        worker, self._worker = self._worker, None
        if worker is None:
            return
        if worker.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(worker.pid, signal.SIGKILL)
        try:
            worker.wait(KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            log.error("guard worker %d did not exit after SIGKILL", worker.pid)
        for stream in (worker.stdin, worker.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


def _answers(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        try:
            probe.connect(str(path))
        except OSError:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    """`python -m halhub.guard.serve`: serve until SIGTERM or SIGINT; errors go to the log (stderr)."""
    halkit_log.setup("halhub")
    if argv:
        log.error("usage: python -m halhub.guard.serve (it takes no arguments)")
        return EXIT_UNAVAILABLE
    try:
        settings = load_policy().guard
        relay = Relay(load_worker(), timeout_s=settings.timeout_s)
        relay.start()
    except (ConfigError, GuardUnavailable, OSError) as exc:
        log.error("hal-guard: %s", exc)
        return EXIT_UNAVAILABLE
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        while not stop.wait(ACCEPT_POLL_S):
            pass
    finally:
        relay.close()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
