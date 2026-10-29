"""Per-connection state for the call pipeline: `HubSession` and the `EventSink` protocol.

04-hub.md, "`HubSession`" and "Audit". One session exists per MCP connection, CLI process,
client process or broker session; it holds what 01-security's policy reads (origin, channel,
profile, taint, grants, rates, approver) and the sink every audit record goes through.
Design decisions:

- Taint starts per 01 REQ-SEC-32: sessions of kind `code`, `box`, `remote` and `hub` start
  `untrusted`, because nothing tells them what the human last saw (REQ-HUB-68, D24). A human
  message resets taint to the starting level, except that `voice` never clears `flagged`
  (audio can come from a page or another person) and `headless` or `routine` text only
  records the message (01 REQ-SEC-93).
- The last human message is kept in memory only, cut to `HUMAN_MESSAGE_CHARS` for the
  reviewer; the audit log gets its length and digest unless `[audit].content` is on.
- A failed `taint_change` record never stops taint from rising: raising it is the safe
  direction, so the record failure is logged and the new level kept.
- No control socket yet: 01's `halhub.approvals.control` lands in phase 2, so `close` only
  drops the session's grants.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, get_args

from halkit import paths

from ._approvals_standin import Approver, GrantStore
from ._audit_standin import AuditUnavailable
from ._policy_standin import Context, Origin, Policy, RateLimiter, ResolvedPath, Taint

log = logging.getLogger(__name__)

SessionKind = Literal["code", "box", "chat", "headless", "routine", "remote", "hub"]
SESSION_KINDS: tuple[str, ...] = get_args(SessionKind)
UNTRUSTED_START = frozenset({"code", "box", "remote", "hub"})  # 01 REQ-SEC-32
RECORD_ONLY_SURFACES = frozenset({"headless", "routine"})  # 01 REQ-SEC-93: no taint reset
HUMAN_MESSAGE_CHARS = 4000  # 01 "Taint, guard and reviewer": what the reviewer reads
SESSION_ID_BYTES = 6  # 12 hex characters (RESOLUTIONS E2)
REMOTE_PREFIX = "remote:"
OWNER_FILE = "owner.json"  # 02-sandbox's session record under runtime_dir()/s/<sid>/


class EventSink(Protocol):
    """Where audit records go; 01's `AuditLog` is the default (RESOLUTIONS B8)."""

    def append(self, event: str, **fields: object) -> None:
        """Writes one record; raises `AuditUnavailable` (an `OSError`) when it cannot."""
        ...


def new_session_id() -> str:
    """A fresh 12-hex session id."""
    return secrets.token_hex(SESSION_ID_BYTES)


def parse_origin(origin: str) -> tuple[Origin, str | None]:
    """`origin` as 01's `Origin` plus the remote client name of `remote:<client>`; raises `ValueError`."""
    if origin.startswith(REMOTE_PREFIX):
        client = origin[len(REMOTE_PREFIX) :]
        if not client:
            raise ValueError("origin remote:<client> needs a client name")
        return Origin.REMOTE, client
    try:
        return Origin(origin), None
    except ValueError:
        choices = ", ".join(o.value for o in Origin)
        raise ValueError(f"unknown origin {origin!r}; expected one of {choices} or remote:<client>") from None


def active_workspaces() -> tuple[Path, ...]:
    """Workspaces of live sessions: `work` of each `runtime_dir()/s/*/owner.json` whose process runs.

    The file format is 02-sandbox's ("Session runtime directory"); a missing runtime directory
    means no sessions. A record whose `pid` and `pidstart` do not match a running process is
    stale and ignored.
    """
    try:
        root = paths.runtime_dir() / "s"
    except paths.RuntimeDirMissing:
        return ()
    found = []
    for owner in sorted(root.glob(f"*/{OWNER_FILE}")):
        try:
            record = json.loads(owner.read_text())
            pid, start, work = int(record["pid"]), str(record["pidstart"]), str(record["work"])
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("ignoring unreadable session record %s", owner)
            continue
        if _process_start(pid) == start:
            found.append(Path(work).resolve())
    return tuple(found)


def _process_start(pid: int) -> str | None:
    """Field 22 of `/proc/<pid>/stat` (start time in clock ticks), or None when the process is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # the command name in field 2 may hold spaces and parentheses; fields after the last ")" are fixed
    return stat.rsplit(")", 1)[1].split()[19]


def _no_workspaces() -> tuple[Path, ...]:
    return ()


@dataclass(eq=False)
class HubSession:
    """Per-connection state that 01-security's policy reads."""

    id: str
    kind: SessionKind
    origin: str
    channel: str | None
    profile: str
    policy: Policy
    approver: Approver | None
    grants: GrantStore
    rate: RateLimiter
    audit: EventSink
    workspaces: Callable[[], tuple[Path, ...]] = _no_workspaces
    remote_addr: str | None = None
    last_human_message: str = ""
    taint: Taint | None = None  # None: the starting level for `kind`
    guard_label: str | None = None  # the last guard label, shown in approval requests
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not paths.SESSION_ID_RE.fullmatch(self.id):
            raise ValueError(f"session id {self.id!r} is not 12 lowercase hex characters")
        if self.kind not in SESSION_KINDS:
            raise ValueError(
                f"unknown session kind {self.kind!r}; expected one of {', '.join(SESSION_KINDS)}"
            )
        origin, _ = parse_origin(self.origin)
        if origin is Origin.REMOTE and self.channel != "remote":
            raise ValueError("origin remote:<client> needs the remote channel")
        if self.taint is None:
            self.taint = self.start_taint

    @property
    def start_taint(self) -> Taint:
        """The level this session starts at and a human message resets it to."""
        return Taint.UNTRUSTED if self.kind in UNTRUSTED_START else Taint.CLEAN

    def fields(self) -> dict[str, object]:
        """The common audit fields this session supplies (01 "Audit log")."""
        return {
            "session": self.id,
            "origin": self.origin,
            "remote_addr": self.remote_addr,
            "channel": self.channel,
            "profile": self.profile,
        }

    def emit(self, event: str, **fields: object) -> None:
        """Appends one audit record with the session's fields; raises `AuditUnavailable`."""
        self.audit.append(event, **self.fields(), **fields)

    def context(
        self,
        now: datetime,
        resolved: Mapping[str, ResolvedPath],
        *,
        routine_steps_sha256: str | None = None,
        approver: bool = True,
    ) -> Context:
        """01's `Context` for one call at `now`, with the paths `prepare` resolved.

        `approver=False` (nested calls) reports no approver whatever the session holds.
        """
        return Context(
            now=now,
            session_kind=self.kind,
            taint=self.current_taint(),
            grants=self.grants.snapshot(self.id),
            rate=self.rate.snapshot(),
            approver_available=approver and self.approver is not None and self.approver.available(),
            paths=resolved,
            workspaces=self.workspaces(),
            home=Path.home(),
            routine_steps_sha256=routine_steps_sha256,
        )

    def current_taint(self) -> Taint:
        """The taint level now."""
        with self._lock:
            assert self.taint is not None  # set in __post_init__
            return self.taint

    def raise_taint(self, level: Taint, *, cause: str) -> None:
        """Raises taint to at least `level` and audits the change; never lowers it."""
        with self._lock:
            before = self.taint
            assert before is not None
            if level <= before:
                return
            self.taint = level
        self._audit_taint(before, level, cause=cause)

    def human_message(self, text: str, *, surface: str) -> None:
        """Keeps `text` for the reviewer and resets taint per 01 F5 and REQ-SEC-32."""
        self.last_human_message = text[:HUMAN_MESSAGE_CHARS]
        with self._lock:
            before = self.taint
            assert before is not None
            reset = surface not in RECORD_ONLY_SURFACES and not (
                surface == "voice" and before is Taint.FLAGGED
            )
            after = self.start_taint if reset else before
            self.taint = after
        if after != before:
            self._audit_taint(before, after, cause="human_message", surface=surface, text=text)

    def close(self) -> None:
        """Ends the session: its `once` and `session` grants are dropped."""
        self.grants.end_session(self.id)

    def _audit_taint(
        self, before: Taint, after: Taint, *, cause: str, surface: str | None = None, text: str = ""
    ) -> None:
        fields: dict[str, object] = {
            "from": before.label,
            "to": after.label,
            "cause": cause,
            "surface": surface,
        }
        if cause == "human_message":
            content = self.policy.audit.content
            digest = hashlib.sha256(text.encode()).hexdigest()
            fields["message"] = text if content else {"chars": len(text), "sha256": digest}
        try:
            self.emit("taint_change", **fields)
        except AuditUnavailable as exc:
            log.error(
                "session %s: taint is now %s but the audit record failed: %s", self.id, after.label, exc
            )


def default_workspaces() -> Callable[[], tuple[Path, ...]]:
    """The workspace source for host front ends: live session records, or none outside Linux."""
    return active_workspaces if os.name == "posix" else _no_workspaces
