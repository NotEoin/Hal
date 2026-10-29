"""The approval control socket: the `control` surface behind `hal approve` (01-security, B3).

One socket per approval session (REQ-SEC-26): per broker session, per `hal-hub mcp`
connection, and per CLI or client process with an approver, at
`runtime_dir()/control/<sid>.sock` (directory 0700, socket 0600). The directory is outside
`runtime_dir()/s/`, so it is never in a box mount. `hal approvals` lists pending requests
across every socket; `hal approve ID` and `hal deny ID` find the owning socket and answer.

Protocol: one JSON object per line, ASCII-escaped, one request per connection except `watch`,
5 s read timeout. Ops `list`, `decide` and `watch`; errors `key_mismatch`, `unknown_id`,
`already_decided`, `expired`, `harness`, `bad_scope`, plus `bad_request` for a malformed line.
`watch` pushes every pending request, then each new one and an `approval_closed` object when
one closes, until either side hangs up.

Design decisions:

- A peer whose `SO_PEERCRED` uid is not ours is closed before a byte is read. A `decide` from
  a peer whose `/proc/<pid>/environ` has `HAL_HARNESS` set is refused with `harness` and
  audited as `approval_refused`, and so is one whose environment cannot be read (fail closed).
  `/proc` shows a process's starting environment, which is what a harness passes to the tools
  it launches. The credential and environment lookups are injectable (`FakePeer` in tests).
- `source` of an answer is always `control`, set here, never read from the client.
- Nothing pending is written to disk; the socket file is removed on `close`. A client that
  finds a socket nobody listens on removes it as stale.
- The socket starts on the first request, so a process that never needs approval leaves no
  socket behind.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import socket
import struct
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from halkit import paths
from halkit.timeutil import parse_iso_z, utc_now

from ..policy.model import Scope
from . import ApprovalRequest, Decision, request_json

if TYPE_CHECKING:  # annotation only: halhub.session imports this package
    from ..session import EventSink

log = logging.getLogger(__name__)

CONTROL_DIR = "control"  # under runtime_dir(), outside `s/` so it is never in a box mount (B3)
READ_TIMEOUT_S = 5.0  # 01 "Control socket"
MAX_LINE_BYTES = 4 * 2**20  # the broker's message cap (RESOLUTIONS E9); a request with every argument fits
ACCEPT_POLL_S = 0.2  # how often the accept and watch loops check for `close`
BACKLOG = 8
NOTE_CHARS = 200  # a person's note on a decision; longer text is cut
MIN_PREFIX_HEX = 6  # 01 "Approval binding": `hal approve` takes a unique prefix of 6+ hex
REQUEST_PREFIX = "apr_"
HARNESS_ENV = b"HAL_HARNESS="
_UCRED = struct.Struct("3i")  # struct ucred: pid, uid, gid (socket(7))


class ControlError(Exception):
    """A control-socket request failed; `code` is the protocol's error string."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class Peer:
    """The process at the other end of a unix socket."""

    pid: int
    uid: int
    gid: int


def peer_credentials(conn: socket.socket) -> Peer:
    """The connecting process's pid, uid and gid from `SO_PEERCRED` (Linux)."""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    return Peer(*_UCRED.unpack(raw))


def read_environ(pid: int) -> bytes:
    """The starting environment of process `pid` (`/proc/<pid>/environ`); raises `OSError`."""
    return Path(f"/proc/{pid}/environ").read_bytes()


def environ_has_harness(environ: bytes) -> bool:
    """True when a NUL-separated environment sets `HAL_HARNESS` to a non-empty value."""
    return any(e.startswith(HARNESS_ENV) and e != HARNESS_ENV for e in environ.split(b"\0"))


def control_dir() -> Path:
    """`runtime_dir()/control`, created 0700."""
    return paths.ensure_private_dir(paths.runtime_dir() / CONTROL_DIR)


@dataclass
class _Pending:
    request: ApprovalRequest
    done: threading.Event
    answer: Decision | None = None


class ControlServer:
    """The `control` approval surface: one socket per approval session, started on first use."""

    name = "control"
    shows_all_args = True

    def __init__(
        self,
        session_id: str,
        *,
        audit: EventSink | None = None,
        on_pending: Callable[[ApprovalRequest, Path], None] | None = None,
        peer: Callable[[socket.socket], Peer] = peer_credentials,
        environ: Callable[[int], bytes] = read_environ,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.session_id = session_id
        self.path: Path | None = None
        self._audit = audit
        self._on_pending = on_pending
        self._peer = peer
        self._environ = environ
        self._clock = clock
        self._pending: dict[str, _Pending] = {}
        self._watchers: list[queue.Queue[dict[str, object] | None]] = []
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._listener: socket.socket | None = None

    # ---- the Approver protocol ----------------------------------------------------------------

    def available(self) -> bool:
        """True until `close`; the socket itself starts with the first request."""
        return not self._closed.is_set()

    def decide(self, request: ApprovalRequest, timeout_s: float) -> Decision:
        """Lists `request` on the socket and waits for a `decide`; no answer in time is a `deny`."""
        path = self.start()
        entry = _Pending(request, threading.Event())
        with self._lock:
            self._pending[request.id] = entry
            self._push(request_json(request))
        if self._on_pending is not None:
            self._on_pending(request, path)
        try:
            entry.done.wait(timeout_s)
        finally:
            with self._lock:
                if self._pending.pop(request.id, None) is not None:
                    self._push({"type": "approval_closed", "id": request.id})
        if entry.answer is None:
            return Decision(request.id, request.key, "deny", None, "timeout")
        return entry.answer

    def withdraw(self, request_id: str) -> None:
        """Another surface answered or the request expired: stop listing it."""
        with self._lock:
            entry = self._pending.pop(request_id, None)
            if entry is not None:
                self._push({"type": "approval_closed", "id": request_id})
        if entry is not None:
            entry.done.set()

    # ---- lifecycle ----------------------------------------------------------------------------

    def start(self) -> Path:
        """Binds the socket if it is not bound yet and returns its path."""
        with self._lock:
            if self._closed.is_set():
                raise ControlError("closed", "the control socket is closed")
            if self.path is not None:
                return self.path
            path = paths.check_socket_path(control_dir() / f"{self.session_id}.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            os.chmod(path, 0o600)  # the 0700 directory already keeps others out while this runs
            listener.listen(BACKLOG)
            listener.settimeout(ACCEPT_POLL_S)
            self._listener, self.path = listener, path
        threading.Thread(target=self._serve, name=f"hal-control-{self.session_id}", daemon=True).start()
        log.info("approval control socket at %s", path)
        return path

    def close(self) -> None:
        """Stops serving, denies whatever is pending, ends watchers and removes the socket file."""
        self._closed.set()
        with self._lock:
            listener, self._listener = self._listener, None
            pending = list(self._pending.values())
            self._pending.clear()
            for watcher in self._watchers:
                watcher.put(None)
            self._watchers.clear()
        for entry in pending:
            entry.done.set()
        if listener is not None:
            listener.close()
        if self.path is not None:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()

    def __enter__(self) -> ControlServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- serving ------------------------------------------------------------------------------

    def _push(self, event: dict[str, object]) -> None:
        """Queues `event` for every watcher; called with the lock held."""
        for watcher in self._watchers:
            watcher.put(event)

    def _serve(self) -> None:
        while not self._closed.is_set():
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
                log.warning("control socket: cannot read peer credentials: %s", exc)
                return
            if peer.uid != os.getuid():
                log.warning("control socket: refused a peer with uid %d", peer.uid)
                return
            conn.settimeout(READ_TIMEOUT_S)
            try:
                line = conn.makefile("rb").readline(MAX_LINE_BYTES + 1)
                message = _parse(line)
                if message is not None and message.get("op") == "watch":
                    self._watch(conn)
                    return
                reply = self._reply(message, peer)
                conn.sendall(json.dumps(reply).encode("ascii") + b"\n")
            except (OSError, TimeoutError) as exc:
                log.warning("control socket: connection failed: %s", exc)

    def _watch(self, conn: socket.socket) -> None:
        events: queue.Queue[dict[str, object] | None] = queue.Queue()
        with self._lock:
            for entry in self._pending.values():
                if entry.answer is None:
                    events.put(request_json(entry.request))
            self._watchers.append(events)
        try:
            while not self._closed.is_set():
                try:
                    event = events.get(timeout=ACCEPT_POLL_S)
                except queue.Empty:
                    continue
                if event is None:
                    return
                conn.sendall(json.dumps(event).encode("ascii") + b"\n")
        except OSError as exc:  # the watcher hung up
            log.debug("control socket: watcher left: %s", exc)
        finally:
            with self._lock:
                if events in self._watchers:
                    self._watchers.remove(events)

    def _reply(self, message: Mapping[str, object] | None, peer: Peer) -> dict[str, object]:
        if message is None:
            return {"ok": False, "error": "bad_request"}
        if message.get("op") == "list":
            with self._lock:
                pending = [request_json(e.request) for e in self._pending.values() if e.answer is None]
            return {"ok": True, "pending": pending}
        if message.get("op") == "decide":
            return self._decide(message, peer)
        return {"ok": False, "error": "bad_request"}

    def _in_harness(self, pid: int) -> bool:
        try:
            return environ_has_harness(self._environ(pid))
        except OSError:
            return True  # fail closed: an unreadable peer may be a harness

    def _decide(self, message: Mapping[str, object], peer: Peer) -> dict[str, object]:
        request_id = message.get("request_id")
        if self._in_harness(peer.pid):
            self._record("approval_refused", approval=request_id, reason="harness", pid=peer.pid)
            return {"ok": False, "error": "harness"}
        verdict, scope, note = message.get("verdict"), message.get("scope"), message.get("note", "")
        if verdict not in ("allow", "deny") or not isinstance(note, str) or not isinstance(request_id, str):
            return {"ok": False, "error": "bad_request"}
        with self._lock:
            entry = self._pending.get(request_id)
            if entry is None:
                return {"ok": False, "error": "unknown_id"}
            request = entry.request
            if entry.answer is not None:
                return {"ok": False, "error": "already_decided"}
            if message.get("key") != request.key:
                error = "key_mismatch"
            elif parse_iso_z(request.expires_at) <= self._clock():
                error = "expired"
            elif verdict == "allow" and scope not in [str(s) for s in request.scopes]:
                error = "bad_scope"
            else:
                error = ""
                chosen = Scope(scope) if verdict == "allow" else None
                entry.answer = Decision(
                    request.id, request.key, verdict, chosen, self.name, note[:NOTE_CHARS]
                )
                entry.done.set()
        if error == "key_mismatch":
            self._record(
                "approval_mismatch",
                approval=request.id,
                key_expected=request.key,
                key_got=message.get("key"),
                source=self.name,
            )
        if error:
            return {"ok": False, "error": error}
        return {"ok": True}

    def _record(self, event: str, **fields: object) -> None:
        if self._audit is None:
            return
        try:
            self._audit.append(event, **fields)
        except OSError as exc:  # the refusal stands whether or not its record was written
            log.error("control socket: %s record failed: %s", event, exc)


def _parse(line: bytes) -> dict[str, object] | None:
    if len(line) > MAX_LINE_BYTES:
        return None
    try:
        message = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return message if isinstance(message, dict) else None


# ---- client side: `hal approvals`, `hal approve ID`, `hal deny ID` ----------------------------


def list_pending(*, timeout: float = READ_TIMEOUT_S) -> list[dict[str, object]]:
    """Every pending request on every control socket of this user, oldest first.

    A socket nobody listens on is stale and removed; one that fails otherwise is skipped with a
    warning.
    """
    found: list[dict[str, object]] = []
    for path in _sockets():
        try:
            reply = _exchange(path, {"op": "list"}, timeout)
        except ConnectionRefusedError:
            log.info("removing stale control socket %s", path)
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            continue
        except (OSError, ControlError) as exc:
            log.warning("control socket %s did not answer: %s", path, exc)
            continue
        pending = reply.get("pending")
        if isinstance(pending, list):
            found += [r for r in pending if isinstance(r, dict)]
    return sorted(found, key=lambda r: str(r.get("created_at", "")))


def find_request(ident: str, pending: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
    """The one pending request whose id is `ident` or starts with it (6+ hex after `apr_`).

    Raises `ControlError("unknown_id")` when none or several match, or the prefix is too short.
    """
    ident = ident.strip().lower()
    if not ident.startswith(REQUEST_PREFIX):
        ident = REQUEST_PREFIX + ident
    if len(ident) - len(REQUEST_PREFIX) < MIN_PREFIX_HEX:
        raise ControlError("unknown_id", f"give at least {MIN_PREFIX_HEX} hex characters of the id")
    matches = [r for r in pending if str(r.get("id", "")).startswith(ident)]
    if len(matches) != 1:
        what = "no pending request" if not matches else f"{len(matches)} pending requests"
        raise ControlError("unknown_id", f"{what} match {ident}")
    return matches[0]


def answer(
    request: Mapping[str, object],
    verdict: str,
    *,
    scope: str | None = None,
    note: str = "",
    timeout: float = READ_TIMEOUT_S,
) -> None:
    """Sends a decision for `request` (as `list_pending` returned it); raises `ControlError`."""
    session = request.get("session")
    if not isinstance(session, str) or not paths.SESSION_ID_RE.fullmatch(session):
        raise ControlError("unknown_id", "the request names no valid session")
    path = paths.runtime_dir() / CONTROL_DIR / f"{session}.sock"
    message = {
        "op": "decide",
        "request_id": request.get("id"),
        "key": request.get("key"),
        "verdict": verdict,
        "scope": scope,
        "note": note,
    }
    try:
        reply = _exchange(path, message, timeout)
    except OSError as exc:
        raise ControlError("unknown_id", f"the session that asked is gone: {exc}") from None
    if reply.get("ok") is not True:
        raise ControlError(str(reply.get("error", "bad_request")))


def watch(path: Path, *, timeout: float = READ_TIMEOUT_S) -> Iterator[dict[str, object]]:
    """Events from one control socket's `watch` op until it closes.

    `timeout` bounds the connect and each wait for the next event; a quiet socket raises
    `TimeoutError`, which the caller may catch to watch again.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(path))
        conn.sendall(b'{"op": "watch"}\n')
        reader = conn.makefile("rb")
        while line := reader.readline(MAX_LINE_BYTES + 1):
            event = _parse(line)
            if event is None:
                raise ControlError("bad_request", f"{path} sent a malformed event")
            yield event


def _sockets() -> list[Path]:
    try:
        directory = paths.runtime_dir() / CONTROL_DIR
    except paths.RuntimeDirMissing:
        return []
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.sock"))


def _exchange(path: Path, message: Mapping[str, object], timeout: float) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(path))
        conn.sendall(json.dumps(message).encode("ascii") + b"\n")
        line = conn.makefile("rb").readline(MAX_LINE_BYTES + 1)
    reply = _parse(line)
    if reply is None:
        raise ControlError("bad_request", f"{path} sent a malformed reply")
    return reply
