"""Approval grants: building them with their bounds, and the store that holds them (01-security).

A grant is a standing approval for one exact call (`once`, `session`) or for the asking steps
of one routine (`routine`). `once_grant`, `session_grant` and `routine_grant` build grants
with the expiry `[approval]` sets and the tool's security digest (REQ-SEC-21); `GrantStore`
refuses a grant that breaks those bounds, so a caller cannot widen one by hand.

Design decisions:

- `once` and `session` grants live only in memory in the process that owns the session;
  nothing pending is written to disk (RESOLUTIONS B3). `end_session` drops them (REQ-SEC-28).
- `routine` grants are the one persisted kind, in `state_dir()/routine-grants.jsonl` (0600, in
  a 0700 directory), because the routines daemon that uses them runs in another process
  (Decision SD17). The file is append-only: `grant` records carry the step list, so
  `hal approvals grants` can show it without the routines pack, and `revoke` records name one
  grant or a routine id. Every read replays the file when its size or mtime changed.
- The file is compacted (rewritten with only live grants, under the same lock `append_jsonl`
  takes) when more than half its lines are dead, so revocations never pile up.
- A file with open permissions or another owner raises `UnsafeGrantFile` (REQ-SEC-15). A
  line that does not parse is skipped with a warning, which can only remove a grant.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from halkit import files, paths
from halkit.timeutil import iso_z, parse_iso_z, utc_now

from ..policy.model import ApprovalSettings, Grant, Scope, ToolMeta
from .keys import security_digest

if TYPE_CHECKING:  # annotation only: halhub.session imports this package
    from ..session import EventSink

log = logging.getLogger(__name__)

ROUTINE_FILE_NAME = "routine-grants.jsonl"
ROUTINE_FILE_MAX_BYTES = 64 * 2**20  # far above a lifetime of routine saves; bounds a runaway writer
ID_HEX_BYTES = 8  # 16 hex characters: `g_` grant ids (01 "Grant")


class UnsafeGrantFile(Exception):
    """`routine-grants.jsonl` is writable by others or owned by another user (REQ-SEC-15)."""


class GrantError(ValueError):
    """A grant that is malformed or outlives the bounds of `[approval]` (REQ-SEC-21)."""


def new_grant_id() -> str:
    """`g_` + 16 hex."""
    return "g_" + secrets.token_hex(ID_HEX_BYTES)


def default_routine_file() -> Path:
    """`state_dir()/routine-grants.jsonl`."""
    return paths.state_dir() / ROUTINE_FILE_NAME


def _call_grant(
    scope: Scope, ttl: timedelta, *, key: str, meta: ToolMeta, session: str, now: datetime, source: str
) -> Grant:
    return Grant(
        id=new_grant_id(),
        scope=scope,
        key=key,
        pack=meta.pack,
        tool=meta.name,
        session=session,
        routine_id=None,
        steps_sha256=None,
        step_keys={},
        meta_sha256=security_digest(meta),
        created=now,
        expires=now + ttl,
        used=False,
        source=source,
    )


def once_grant(
    *, key: str, meta: ToolMeta, session: str, settings: ApprovalSettings, now: datetime, source: str
) -> Grant:
    """A `once` grant for `key`, expiring `approval.once_ttl_s` after `now`."""
    ttl = timedelta(seconds=settings.once_ttl_s)
    return _call_grant(Scope.ONCE, ttl, key=key, meta=meta, session=session, now=now, source=source)


def session_grant(
    *, key: str, meta: ToolMeta, session: str, settings: ApprovalSettings, now: datetime, source: str
) -> Grant:
    """A `session` grant for `key` in `session`, expiring `approval.session_max_h` after `now`."""
    ttl = timedelta(hours=settings.session_max_h)
    return _call_grant(Scope.SESSION, ttl, key=key, meta=meta, session=session, now=now, source=source)


def routine_grant(
    *,
    routine_id: str,
    steps_sha256: str,
    step_keys: Mapping[str, str],
    settings: ApprovalSettings,
    now: datetime,
    source: str,
) -> Grant:
    """A `routine` grant for the asking steps (key -> tool security digest) of one step list."""
    return Grant(
        id=new_grant_id(),
        scope=Scope.ROUTINE,
        key=None,
        pack=None,
        tool=None,
        session=None,
        routine_id=routine_id,
        steps_sha256=steps_sha256,
        step_keys=dict(step_keys),
        meta_sha256=None,
        created=now,
        expires=now + timedelta(days=settings.routine_max_days),
        used=False,
        source=source,
    )


def check_grant(grant: Grant, settings: ApprovalSettings) -> None:
    """Raises `GrantError` when `grant` misses a field its scope needs or outlives its bound."""
    life = grant.expires - grant.created
    if life <= timedelta(0):
        raise GrantError(f"grant {grant.id} expires before it was created")
    if grant.scope is Scope.ROUTINE:
        if not (grant.routine_id and grant.steps_sha256 and grant.step_keys):
            raise GrantError(f"routine grant {grant.id} needs a routine id, a step hash and step keys")
        if grant.key or grant.session:
            raise GrantError(f"routine grant {grant.id} may not carry a call key or a session")
        bound = timedelta(days=settings.routine_max_days)
    else:
        if not (grant.key and grant.pack and grant.tool and grant.session and grant.meta_sha256):
            raise GrantError(f"{grant.scope} grant {grant.id} needs a key, pack, tool, session and digest")
        bound = (
            timedelta(seconds=settings.once_ttl_s)
            if grant.scope is Scope.ONCE
            else timedelta(hours=settings.session_max_h)
        )
    if life > bound:
        raise GrantError(f"{grant.scope} grant {grant.id} lasts longer than [approval] allows")


@dataclass(frozen=True)
class RoutineGrantView:
    """A live routine grant with the steps it was approved for (what `hal approvals grants` prints)."""

    grant: Grant
    steps: tuple[Mapping[str, object], ...]  # {"tool", "tier", "args"} per step, canonical args


class GrantStore:
    """`once` and `session` grants in memory, `routine` grants in `routine_file`; thread-safe."""

    def __init__(
        self,
        routine_file: Path | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
        settings: ApprovalSettings | None = None,
        audit: EventSink | None = None,
    ) -> None:
        self.routine_file = routine_file
        self.settings = settings or ApprovalSettings()
        self._clock = clock
        self._audit = audit
        self._grants: dict[str, Grant] = {}
        self._routine: dict[str, RoutineGrantView] = {}
        self._routine_stamp: tuple[int, int] | None = None
        self._lines = 0
        self._lock = threading.Lock()

    def check_file(self) -> None:
        """Raises `UnsafeGrantFile` now if the routine file is unsafe; brokers call it at start."""
        with self._lock:
            self._reload_routine()

    def snapshot(self, session: str) -> tuple[Grant, ...]:
        """Live grants usable by `session`: its own `once` and `session` grants and every routine grant."""
        now = self._clock()
        with self._lock:
            self._reload_routine()
            mine = [g for g in self._grants.values() if g.session == session]
            routine = [v.grant for v in self._routine.values()]
            return tuple(g for g in (*mine, *routine) if g.expires > now)

    def add(self, grant: Grant, *, steps: Sequence[Mapping[str, object]] = ()) -> None:
        """Stores `grant`; a routine grant is appended to the file with `steps`.

        Raises `GrantError` for a malformed or over-long grant, an id already present, or a
        routine grant without a file.
        """
        check_grant(grant, self.settings)
        with self._lock:
            self._reload_routine()
            if grant.id in self._grants or grant.id in self._routine:
                raise GrantError(f"grant {grant.id} already exists")
            if grant.scope is not Scope.ROUTINE:
                self._grants[grant.id] = grant
                return
            if self.routine_file is None:
                raise GrantError("this grant store has no routine grant file")
            paths.ensure_private_dir(self.routine_file.parent)
            record = {"op": "grant", **_grant_record(grant), "steps": [dict(s) for s in steps]}
            files.append_jsonl(self.routine_file, record, max_bytes=ROUTINE_FILE_MAX_BYTES)
            self._reload_routine()

    def consume(self, grant_id: str) -> bool:
        """For a `once` grant: True exactly once, while unexpired; True for other live grants."""
        now = self._clock()
        with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None:
                self._reload_routine()
                view = self._routine.get(grant_id)
                return view is not None and view.grant.expires > now
            if grant.expires <= now:
                return False
            if grant.scope is Scope.ONCE:
                if grant.used:
                    return False
                self._grants[grant_id] = replace(grant, used=True)
            return True

    def revoke(self, grant_id: str, *, by: str) -> bool:
        """Revokes one grant (REQ-SEC-99); True when it existed. Audited as `grant_revoked`."""
        with self._lock:
            if self._grants.pop(grant_id, None) is not None:
                found = True
            else:
                self._reload_routine()
                found = grant_id in self._routine
                if found:
                    self._append_revoke({"grant": grant_id}, by)
        if found:
            self._audit_revoke(by, grant=grant_id)
        return found

    def revoke_routine(self, routine_id: str, *, by: str) -> int:
        """Revokes every grant of `routine_id` (an edit or delete of the routine); returns how many."""
        with self._lock:
            self._reload_routine()
            count = sum(1 for v in self._routine.values() if v.grant.routine_id == routine_id)
            if count:
                self._append_revoke({"routine_id": routine_id}, by)
        if count:
            self._audit_revoke(by, routine_id=routine_id, count=count)
        return count

    def routine_grants(self) -> tuple[RoutineGrantView, ...]:
        """Live routine grants with their steps, oldest first."""
        now = self._clock()
        with self._lock:
            self._reload_routine()
            return tuple(v for v in self._routine.values() if v.grant.expires > now)

    def end_session(self, session: str) -> None:
        """Drops every `once` and `session` grant of `session` (REQ-SEC-28)."""
        with self._lock:
            for gid in [gid for gid, g in self._grants.items() if g.session == session]:
                del self._grants[gid]

    # ---- the routine file; every method below runs with self._lock held ----------------------

    def _append_revoke(self, target: Mapping[str, str], by: str) -> None:
        assert self.routine_file is not None  # a routine grant exists only with a file
        record = {"op": "revoke", **target, "by": by, "ts": iso_z(self._clock(), ms=True)}
        files.append_jsonl(self.routine_file, record, max_bytes=ROUTINE_FILE_MAX_BYTES)
        self._reload_routine()
        live = sum(1 for v in self._routine.values() if v.grant.expires > self._clock())
        if self._lines > 2 * live:
            self._compact()

    def _compact(self) -> None:
        assert self.routine_file is not None
        with files.locked(self.routine_file):
            self._routine_stamp = None
            self._reload_routine()
            now = self._clock()
            lines = [
                json.dumps(
                    {"op": "grant", **_grant_record(v.grant), "steps": [dict(s) for s in v.steps]},
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
                + "\n"
                for v in self._routine.values()
                if v.grant.expires > now
            ]
            files.write_atomic(self.routine_file, "".join(lines), mode=0o600)
        log.info("compacted %s to %d live routine grants", self.routine_file, len(lines))
        self._routine_stamp = None
        self._reload_routine()

    def _audit_revoke(self, by: str, **fields: object) -> None:
        if self._audit is not None:
            self._audit.append("grant_revoked", by=by, **fields)

    def _reload_routine(self) -> None:
        if self.routine_file is None:
            return
        try:
            st = self.routine_file.stat()
        except FileNotFoundError:
            self._routine, self._routine_stamp, self._lines = {}, None, 0
            return
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise UnsafeGrantFile(f"{self.routine_file} must be owned by you with mode 0600")
        stamp = (st.st_size, st.st_mtime_ns)
        if stamp == self._routine_stamp:
            return
        loaded: dict[str, RoutineGrantView] = {}
        lines = 0
        with self.routine_file.open(encoding="ascii") as fh:
            for number, line in enumerate(fh, 1):
                lines += 1
                try:
                    record = json.loads(line)
                    if record.get("op") == "grant":
                        loaded[record["id"]] = RoutineGrantView(
                            _grant_from(record), tuple(record.get("steps", ()))
                        )
                    elif record.get("op") == "revoke" and "grant" in record:
                        loaded.pop(str(record["grant"]), None)
                    elif record.get("op") == "revoke" and "routine_id" in record:
                        for gid in [
                            g for g, v in loaded.items() if v.grant.routine_id == record["routine_id"]
                        ]:
                            del loaded[gid]
                    else:
                        raise ValueError(f"unknown record {record.get('op')!r}")
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    log.warning("%s:%d: skipped a routine grant line: %s", self.routine_file, number, exc)
        self._routine, self._routine_stamp, self._lines = loaded, stamp, lines


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
    if not isinstance(step_keys, dict) or not step_keys:
        raise ValueError("step_keys must be a non-empty object")
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
