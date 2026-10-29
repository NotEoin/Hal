"""Per-connection state for the call pipeline: `HubSession` and the `EventSink` protocol.

04-hub.md, "`HubSession`" and "Audit". One session exists per MCP connection, CLI process,
client process or broker session; it holds what 01-security's policy reads (origin, channel,
profile, taint, grants, rates, approver) and the sink every audit record goes through.
Design decisions:

- Taint follows 01's `halhub.policy.taint.TaintTracker` (REQ-SEC-32, REQ-HUB-68): the
  session holds one tracker and writes the `taint_change` record for each change it returns.
  `resumed=True` marks a session reopened after a broker restart, which starts `untrusted`.
- The last human message is kept in memory only, cut to `HUMAN_MESSAGE_CHARS` for the
  reviewer; the audit log gets its length and digest unless `[audit].content` is on.
- A failed `taint_change` record never stops taint from rising: raising it is the safe
  direction, so the record failure is logged and the new level kept.
- `close` drops the session's `once` and `session` grants, its rate counts and its recorded
  search URLs; the control socket belongs to the front end that made it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, get_args

from halkit import paths

from .approvals import Approver, GrantStore
from .audit import AuditUnavailable
from .policy.model import Context, Origin, Policy, ResolvedPath, Taint, ToolMeta
from .policy.provenance import SearchUrls
from .policy.rate import RateLimiter
from .policy.taint import TaintChange, TaintTracker

log = logging.getLogger(__name__)

SessionKind = Literal["code", "box", "chat", "headless", "routine", "remote", "hub"]
SESSION_KINDS: tuple[str, ...] = get_args(SessionKind)
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


class SessionSink:
    """An `EventSink` for objects built before their session (the control socket, the router).

    Records go through `session.emit` once `session` is set. Before that there is no session
    to name, and `append` raises `AuditUnavailable`: the audit log is always fail-closed (01, F4).
    """

    def __init__(self) -> None:
        self.session: HubSession | None = None

    def append(self, event: str, **fields: object) -> None:
        """Writes through the bound session; raises `AuditUnavailable` like `HubSession.emit`."""
        if self.session is None:
            raise AuditUnavailable(f"no session yet for the {event} audit record")
        self.session.emit(event, **fields)


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
    search_urls: SearchUrls = field(default_factory=SearchUrls)
    home: Path = field(default_factory=Path.home)  # where `~/` in path arguments and roots points
    remote_addr: str | None = None
    last_human_message: str = ""
    resumed: bool = False  # reopened after a broker restart: starts `untrusted` (01 F5)
    guard_label: str | None = None  # the last guard label, shown in approval requests
    _taint: TaintTracker = field(init=False, repr=False)

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
        self._taint = TaintTracker(self.kind, resumed=self.resumed)

    @property
    def start_taint(self) -> Taint:
        """The level a human message resets this session to."""
        return self._taint.reset_level

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
        meta: ToolMeta | None = None,
    ) -> Context:
        """01's `Context` for one call at `now`, with the paths `prepare` resolved.

        `approver=False` (nested calls) reports no approver whatever the session holds; `meta`
        adds the tool's own rate count to the snapshot.
        """
        return Context(
            now=now,
            session_kind=self.kind,
            taint=self.current_taint(),
            grants=self.grants.snapshot(self.id),
            rate=self.rate.snapshot(self.id, self.policy, meta),
            approver_available=approver and self.approver is not None and self.approver.available(),
            paths=resolved,
            workspaces=self.workspaces(),
            home=self.home,
            routine_steps_sha256=routine_steps_sha256,
            search_urls=self.search_urls.urls(self.id),
        )

    def current_taint(self) -> Taint:
        """The taint level now."""
        return self._taint.level

    def raise_taint(self, level: Taint, *, cause: str) -> None:
        """Raises taint to at least `level` and audits the change; never lowers it."""
        change = self._taint.raise_to(level, cause=cause)
        if change is not None:
            self._audit_taint(change)

    def human_message(self, text: str, *, surface: str) -> None:
        """Keeps `text` for the reviewer and resets taint per 01 F5 and REQ-SEC-32.

        Raises `ValueError` for a surface `TaintTracker` does not know.
        """
        change = self._taint.human_message(surface)
        self.last_human_message = text[:HUMAN_MESSAGE_CHARS]
        if change is not None:
            self._audit_taint(change, text=text)

    def close(self) -> None:
        """Ends the session: its grants, rate counts and search URLs are dropped."""
        self.grants.end_session(self.id)
        self.rate.forget(self.id)
        self.search_urls.forget(self.id)

    def _audit_taint(self, change: TaintChange, *, text: str = "") -> None:
        fields: dict[str, object] = {
            "from": change.before.label,
            "to": change.after.label,
            "cause": change.cause,
            "surface": change.surface,
        }
        if change.cause == "human_message":
            content = self.policy.audit.content
            digest = hashlib.sha256(text.encode()).hexdigest()
            fields["message"] = text if content else {"chars": len(text), "sha256": digest}
        try:
            self.emit("taint_change", **fields)
        except AuditUnavailable as exc:
            level = change.after.label
            log.error("session %s: taint is now %s but the audit record failed: %s", self.id, level, exc)


def default_workspaces() -> Callable[[], tuple[Path, ...]]:
    """The workspace source for host front ends: live session records, or none outside Linux."""
    return active_workspaces if os.name == "posix" else _no_workspaces
