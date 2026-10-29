"""Phase-1 stand-in for 01-security's `halhub.approvals`: requests, answers, grants and the router.

Copies the published shapes of 01-security.md, "The approvals module", with the same names
and fields: `ApprovalRequest`, `Decision(verdict, scope)` (RESOLUTIONS B1), the `Approver`
protocol with `shows_all_args`, `GrantStore` and `ApprovalRouter`. Phase 2 replaces this module
with an import change in the hub. The key functions live in `_policy_standin` beside
`canonical_json` (re-exported here) because the stand-in `decide` needs them too.

Design decisions:

- Grants for `once` and `session` live only in memory in the owning process; nothing pending
  is written to disk (RESOLUTIONS B3, REQ-HUB-21).
- The hub, not the router, adds the grant and writes the `approval_request` and `approval`
  records (04-hub "One call", step 7), because the grant records the tool's security digest
  and `ApprovalRequest` does not carry the tool metadata; this also audits answers from an
  approver that is a bare surface. The router writes `approval_mismatch`, the one record only
  it can see.
- `routine` grants are the one persisted kind, appended to `routine-grants.jsonl` (0600 in a
  0700 directory) with their step list, because the routines daemon that uses them runs in
  another process (01 "Grant store"). Every snapshot re-reads the file when its size or
  mtime changed. A file with open permissions or another owner raises `UnsafeGrantFile`, so
  the call that needed it is denied (01 REQ-SEC-15); a line that does not parse is skipped
  with a warning, which can only remove a grant.
- Missing until 01 lands: the `notify` surface, revocation, compaction and
  `hal approvals grants`. The control socket (`hal approve`) is `_control_standin`.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from halkit import files, paths
from halkit.timeutil import iso_z, parse_iso_z, utc_now

from ._policy_standin import Grant, Origin, Scope, Taint, Tier, approval_key, security_digest

log = logging.getLogger(__name__)

__all__ = [
    "ApprovalRequest",
    "ApprovalRouter",
    "Approver",
    "Decision",
    "GrantStore",
    "RoutineGrantView",
    "UnsafeGrantFile",
    "approval_key",
    "escape",
    "new_grant_id",
    "new_request_id",
    "security_digest",
]

ROUTINE_FILE_MAX_BYTES = 64 * 2**20  # far above a lifetime of routine saves; bounds a runaway writer
ID_HEX_BYTES = 8  # 16 hex characters: `apr_` request ids and `g_` grant ids (01 "Approval binding")
# C0 and C1 controls, DEL, bidirectional controls and zero-width characters (REQ-SEC-25)
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def new_request_id() -> str:
    """`apr_` + 16 hex: typed by people in `hal approve ID`."""
    return "apr_" + secrets.token_hex(ID_HEX_BYTES)


def new_grant_id() -> str:
    """`g_` + 16 hex."""
    return "g_" + secrets.token_hex(ID_HEX_BYTES)


def escape(text: str, *, multiline: bool = True) -> str:
    """`text` with control, bidirectional and zero-width characters shown as `\\uXXXX`.

    Stand-in for `halkit.termsafe.escape` (01 "Display rules"): tabs show as `\\t` and, in
    single-line views, newlines as `\\n`.
    """
    text = text.replace("\t", "\\t")
    if not multiline:
        text = text.replace("\n", "\\n")
    return _UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


@dataclass(frozen=True)
class ApprovalRequest:
    """One host-originated approval request, rendered by the host with every argument."""

    id: str
    session: str
    origin: Origin
    channel: str | None
    pack: str
    pack_version: str
    tool: str
    tier: Tier
    key: str
    reason: str
    args: Mapping[str, object]
    display: str
    summary: str
    content: bool
    challenge: str | None
    scopes: tuple[Scope, ...]
    taint: Taint
    guard: str | None
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class Decision:
    """An approver's answer; `key` must echo the request's key (REQ-SEC-23)."""

    request_id: str
    key: str
    verdict: Literal["allow", "deny"]
    scope: Scope | None
    source: str
    note: str = ""


class Approver(Protocol):
    """An approval surface, or a router over several (01 "Approval surfaces and protocol")."""

    name: str
    shows_all_args: bool

    def available(self) -> bool:
        """True when this surface can show a request now."""
        ...

    def decide(self, request: ApprovalRequest, timeout_s: float) -> Decision:
        """Blocks until the human answers or `timeout_s` passes."""
        ...

    def withdraw(self, request_id: str) -> None:
        """Another surface answered or the request expired."""
        ...


class UnsafeGrantFile(Exception):
    """`routine-grants.jsonl` is writable by others or owned by another user (01 REQ-SEC-15)."""


@dataclass(frozen=True)
class RoutineGrantView:
    """A live routine grant with the steps it was approved for (what `hal approvals grants` prints)."""

    grant: Grant
    steps: tuple[Mapping[str, object], ...]


class GrantStore:
    """`once` and `session` grants in memory, `routine` grants in `routine_file`."""

    def __init__(self, routine_file: Path | None = None, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.routine_file = routine_file
        self._clock = clock
        self._grants: dict[str, Grant] = {}
        self._routine: dict[str, RoutineGrantView] = {}
        self._routine_stamp: tuple[int, int] | None = None
        self._lock = threading.Lock()

    def snapshot(self, session: str) -> tuple[Grant, ...]:
        """Live grants usable by `session`: its own `once` and `session` grants and every routine grant."""
        now = self._clock()
        with self._lock:
            self._reload_routine()
            mine = [g for g in self._grants.values() if g.session == session]
            routine = [v.grant for v in self._routine.values()]
            return tuple(g for g in (*mine, *routine) if g.expires > now)

    def add(self, grant: Grant, *, steps: Sequence[Mapping[str, object]] = ()) -> None:
        """Stores `grant`; a routine grant is appended to the file with `steps`. Raises `ValueError`
        for a grant id already present or a routine grant without a file."""
        with self._lock:
            if grant.id in self._grants or grant.id in self._routine:
                raise ValueError(f"grant {grant.id} already exists")
            if grant.scope is not Scope.ROUTINE:
                self._grants[grant.id] = grant
                return
            if self.routine_file is None:
                raise ValueError("this grant store has no routine grant file")
            paths.ensure_private_dir(self.routine_file.parent)
            record = {"op": "grant", **_grant_record(grant), "steps": [dict(s) for s in steps]}
            files.append_jsonl(self.routine_file, record, max_bytes=ROUTINE_FILE_MAX_BYTES)
            self._routine[grant.id] = RoutineGrantView(grant, tuple(steps))

    def consume(self, grant_id: str) -> bool:
        """For a `once` grant: True exactly once, while unexpired; True for live other scopes."""
        now = self._clock()
        with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None:
                view = self._routine.get(grant_id)
                return view is not None and view.grant.expires > now
            if grant.expires <= now:
                return False
            if grant.scope is Scope.ONCE:
                if grant.used:
                    return False
                self._grants[grant_id] = replace(grant, used=True)
            return True

    def routine_grants(self) -> tuple[RoutineGrantView, ...]:
        """Live routine grants with their steps."""
        now = self._clock()
        with self._lock:
            self._reload_routine()
            return tuple(v for v in self._routine.values() if v.grant.expires > now)

    def end_session(self, session: str) -> None:
        """Drops every `once` and `session` grant of `session`."""
        with self._lock:
            for gid in [gid for gid, g in self._grants.items() if g.session == session]:
                del self._grants[gid]

    def _reload_routine(self) -> None:
        """Re-reads the routine grant file when it changed; called with the lock held."""
        if self.routine_file is None:
            return
        try:
            st = self.routine_file.stat()
        except FileNotFoundError:
            self._routine, self._routine_stamp = {}, None
            return
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise UnsafeGrantFile(f"{self.routine_file} must be owned by you with mode 0600")
        stamp = (st.st_size, st.st_mtime_ns)
        if stamp == self._routine_stamp:
            return
        loaded: dict[str, RoutineGrantView] = {}
        with self.routine_file.open(encoding="ascii") as fh:
            for number, line in enumerate(fh, 1):
                try:
                    record = json.loads(line)
                    if record.get("op") == "grant":
                        loaded[record["id"]] = RoutineGrantView(
                            _grant_from(record), tuple(record.get("steps", ()))
                        )
                    elif record.get("op") == "revoke":
                        loaded.pop(record.get("grant", ""), None)
                except (ValueError, KeyError, TypeError) as exc:
                    log.warning("%s:%d: skipped a routine grant line: %s", self.routine_file, number, exc)
        self._routine, self._routine_stamp = loaded, stamp


def _grant_record(grant: Grant) -> dict[str, object]:
    return {
        "id": grant.id,
        "scope": str(grant.scope),
        "key": grant.key,
        "pack": grant.pack,
        "tool": grant.tool,
        "session": grant.session,
        "routine_id": grant.routine_id,
        "steps_sha256": grant.steps_sha256,
        "step_keys": dict(grant.step_keys),
        "meta_sha256": grant.meta_sha256,
        "created": iso_z(grant.created, ms=True),
        "expires": iso_z(grant.expires, ms=True),
        "used": grant.used,
        "source": grant.source,
    }


def _grant_from(record: Mapping[str, object]) -> Grant:
    if record["scope"] != Scope.ROUTINE:
        raise ValueError(f"only routine grants are persisted, got {record['scope']!r}")
    step_keys = record["step_keys"]
    if not isinstance(step_keys, dict):
        raise ValueError("step_keys must be an object")
    return Grant(
        id=str(record["id"]),
        scope=Scope.ROUTINE,
        key=None,
        pack=None,
        tool=None,
        session=None,
        routine_id=str(record["routine_id"]),
        steps_sha256=str(record["steps_sha256"]),
        step_keys={str(k): str(v) for k, v in step_keys.items()},
        meta_sha256=None,
        created=parse_iso_z(str(record["created"])),
        expires=parse_iso_z(str(record["expires"])),
        used=False,
        source=str(record["source"]),
    )


class ApprovalRouter:
    """Offers a request to every available surface that may show it; the first matching answer wins."""

    name = "router"
    shows_all_args = True

    def __init__(
        self,
        surfaces: Sequence[Approver],
        *,
        audit: Callable[..., None] | None = None,
    ) -> None:
        self.surfaces = tuple(surfaces)
        self._audit = audit

    def available(self) -> bool:
        """True when any surface is available."""
        return any(s.available() for s in self.surfaces)

    def surfaces_for(self, request: ApprovalRequest) -> tuple[Approver, ...]:
        """Available surfaces allowed to show `request`; a `content` request needs `shows_all_args`."""
        return tuple(s for s in self.surfaces if s.available() and (s.shows_all_args or not request.content))

    def decide(self, request: ApprovalRequest, timeout_s: float) -> Decision:
        """The first answer whose id and key match; `deny` from `timeout` or `no-approver` otherwise."""
        surfaces = self.surfaces_for(request)
        if not surfaces:
            return Decision(request.id, request.key, "deny", None, "no-approver")
        answers: queue.Queue[tuple[Approver, Decision | BaseException]] = queue.Queue()

        def ask(surface: Approver) -> None:
            try:
                answers.put((surface, surface.decide(request, timeout_s)))
            except Exception as exc:  # a broken surface counts as no answer, never as allow
                answers.put((surface, exc))

        for surface in surfaces:
            threading.Thread(
                target=ask, args=(surface,), name=f"approval-{surface.name}", daemon=True
            ).start()
        deadline = time.monotonic() + timeout_s
        pending = len(surfaces)
        timed_out = False
        winner: Decision | None = None
        while pending and winner is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                surface, answer = answers.get(timeout=remaining)
            except queue.Empty:
                break
            pending -= 1
            # a surface's own timeout is no answer, never a "deny" that beats the other surfaces
            if isinstance(answer, TimeoutError) or (
                isinstance(answer, Decision) and answer.verdict == "deny" and answer.source == "timeout"
            ):
                timed_out = True
                continue
            if isinstance(answer, BaseException):
                log.warning("approval surface %s failed: %s", surface.name, answer)
                continue
            if answer.request_id != request.id or answer.key != request.key:
                self._mismatch(request, answer, surface)
                continue
            winner = replace(answer, source=surface.name)
        for surface in surfaces:
            surface.withdraw(request.id)
        if winner is not None:
            return winner
        source = "timeout" if pending or timed_out else "no-approver"
        return Decision(request.id, request.key, "deny", None, source)

    def _mismatch(self, request: ApprovalRequest, answer: Decision, surface: Approver) -> None:
        log.warning("approval %s: answer from %s did not match the request", request.id, surface.name)
        if self._audit is not None:
            self._audit(
                "approval_mismatch",
                approval=request.id,
                key_expected=request.key,
                key_got=answer.key,
                source=surface.name,
            )
