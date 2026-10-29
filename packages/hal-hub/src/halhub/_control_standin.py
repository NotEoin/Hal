"""Phase-1 stand-in for 01-security's `halhub.approvals.control`: the approval control socket.

01-security.md, "Approval surfaces and protocol", control socket (RESOLUTIONS B3,
REQ-SEC-26). A process with an approver owns one socket, `runtime_dir()/control/<sid>.sock`
(directory 0700, socket 0600), and is the `control` approval surface: a pending request is
listed there, and `hal approve ID` / `hal-hub approve ID` answer it. Phase 2 replaces this
module with an import change.

Design decisions:

- Same protocol as 01: one JSON object per line, ASCII-escaped, one request per connection, a
  5 s read timeout; ops `list` and `decide`, errors `key_mismatch`, `unknown_id`,
  `already_decided`, `expired`, `harness`, `bad_scope` (plus `bad_request` for a malformed
  line).
- A peer whose `SO_PEERCRED` uid is not ours is closed before a byte is read. A `decide`
  from a peer whose `/proc/<pid>/environ` has `HAL_HARNESS` set is refused with `harness`,
  and so is one whose environment cannot be read (fail closed). `/proc` shows a process's
  starting environment, which is what a harness passes to the tools it launches.
- `source` of an answer is always `control`, set here, never read from the client.
- Nothing pending is written to disk; the socket file is removed on `close`. A client that
  finds a socket nobody listens on removes it as stale.
- The socket starts on the first request, so a process that never needs approval leaves no
  socket behind.
- Left out until 01 lands: the `watch` op (06's `hal approvals watch`), and 01's exact audit
  event for a refused `decide`; this stand-in writes `approval_refused` with the reason.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import struct
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from halkit import paths
from halkit.timeutil import parse_iso_z, utc_now

from ._approvals_standin import ApprovalRequest, Decision
from ._policy_standin import Scope

log = logging.getLogger(__name__)

__all__ = [
    "ControlError",
    "ControlServer",
    "Peer",
    "answer",
    "control_dir",
    "list_pending",
    "request_json",
]

CONTROL_DIR = "control"  # under runtime_dir(), outside `s/` so it is never in a box mount (B3)
READ_TIMEOUT_S = 5.0  # 01 "Control socket"
MAX_LINE_BYTES = 4 * 2**20  # the broker's message cap (RESOLUTIONS E9); a request with every argument fits
ACCEPT_POLL_S = 0.2  # how often the accept loop checks for `close`
BACKLOG = 8
NOTE_CHARS = 200  # a person's note on a decision; longer text is cut
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


def peer_in_harness(pid: int) -> bool:
    """True when process `pid` started with `HAL_HARNESS` set, or its environment cannot be read."""
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return True  # fail closed: an unreadable peer may be a harness
    return any(entry.startswith(HARNESS_ENV) and entry != HARNESS_ENV for entry in environ.split(b"\0"))


def control_dir() -> Path:
    """`runtime_dir()/control`, created 0700."""
    return paths.ensure_private_dir(paths.runtime_dir() / CONTROL_DIR)


def request_json(request: ApprovalRequest) -> dict[str, object]:
    """The request as 01's `approval_request` JSON object."""
    return {
        "type": "approval_request",
        "v": 1,
        "id": request.id,
        "session": request.session,
        "origin": str(request.origin),
        "channel": request.channel,
        "pack": request.pack,
        "pack_version": request.pack_version,
        "tool": request.tool,
        "tier": str(request.tier),
        "key": request.key,
        "reason": request.reason,
        "args": dict(request.args),
        "display": request.display,
        "summary": request.summary,
        "content": request.content,
        "challenge": request.challenge,
        "scopes": [str(s) for s in request.scopes],
        "taint": request.taint.name.lower(),
        "guard": request.guard,
        "created_at": request.created_at,
        "expires_at": request.expires_at,
    }


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
        audit: Callable[..., None] | None = None,
        on_pending: Callable[[ApprovalRequest, Path], None] | None = None,
        peer: Callable[[socket.socket], Peer] | None = None,
        in_harness: Callable[[int], bool] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.session_id = session_id
        self.path: Path | None = None
        self._audit = audit
        self._on_pending = on_pending
        # looked up at use, so a test can replace the module's checks
        self._peer = peer or (lambda conn: peer_credentials(conn))
        self._in_harness = in_harness or (lambda pid: peer_in_harness(pid))
        self._clock = clock
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._listener: socket.socket | None = None

    # -- the Approver protocol --

    def available(self) -> bool:
        """True until `close`; the socket itself starts with the first request."""
        return not self._closed.is_set()

    def decide(self, request: ApprovalRequest, timeout_s: float) -> Decision:
        """Lists `request` on the socket and waits for a `decide`; no answer in time is a `deny`."""
        path = self.start()
        entry = _Pending(request, threading.Event())
        with self._lock:
            self._pending[request.id] = entry
        if self._on_pending is not None:
            self._on_pending(request, path)
        try:
            entry.done.wait(timeout_s)
        finally:
            with self._lock:
                self._pending.pop(request.id, None)
        if entry.answer is None:
            return Decision(request.id, request.key, "deny", None, "timeout")
        return entry.answer

    def withdraw(self, request_id: str) -> None:
        """Another surface answered or the request expired: stop listing it."""
        with self._lock:
            entry = self._pending.pop(request_id, None)
        if entry is not None:
            entry.done.set()

    # -- lifecycle --

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
        """Stops serving, denies whatever is pending and removes the socket file."""
        self._closed.set()
        with self._lock:
            listener, self._listener = self._listener, None
            pending = list(self._pending.values())
            self._pending.clear()
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

    # -- serving --

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
                reply = self._reply(line, peer)
                conn.sendall(json.dumps(reply).encode("ascii") + b"\n")
            except (OSError, TimeoutError) as exc:
                log.warning("control socket: connection failed: %s", exc)

    def _reply(self, line: bytes, peer: Peer) -> dict[str, object]:
        if len(line) > MAX_LINE_BYTES:
            return {"ok": False, "error": "bad_request"}
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"ok": False, "error": "bad_request"}
        if not isinstance(message, dict):
            return {"ok": False, "error": "bad_request"}
        if message.get("op") == "list":
            with self._lock:
                pending = [request_json(e.request) for e in self._pending.values() if e.answer is None]
            return {"ok": True, "pending": pending}
        if message.get("op") == "decide":
            return self._decide(message, peer)
        return {"ok": False, "error": "bad_request"}

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
            self._audit(event, **fields)
        except OSError as exc:  # the refusal stands whether or not its record was written
            log.error("control socket: %s record failed: %s", event, exc)


# -- client side: `hal approvals`, `hal approve ID`, `hal deny ID` --


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
    if not isinstance(session, str):
        raise ControlError("unknown_id", "the request names no session")
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
    try:
        reply = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ControlError("bad_request", f"{path} sent a malformed reply") from None
    if not isinstance(reply, dict):
        raise ControlError("bad_request", f"{path} sent a malformed reply")
    return reply
