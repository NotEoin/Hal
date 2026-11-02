"""The audit log: one JSONL record per security event, for every front end (01-security, B8).

`AuditLog` is 04-hub's `EventSink`. It writes `state_dir()/audit/<UTC date>.jsonl` (directory
0700, files 0600, rotated at `audit.max_file_mb`, 99 rotations kept) with the common fields
`v`, `ts` (UTC, milliseconds, `Z`), `pid` and a per-process `seq`; the caller supplies `event`,
the session fields and the event fields. `tail` and `verify` back `hal audit tail` and
`hal audit show` (REQ-SEC-44); `prune` deletes day files past `audit.retention_days`
(REQ-SEC-42) and runs at broker start.

Design decisions:

- Every record passes through `halkit.redact` with the host's known secret values before it
  is written (REQ-SEC-43). Redaction runs before truncation, so a secret cut in half by the
  4096-character limit is still found whole.
- Strings are cut at 4096 characters with a `[+N chars]` marker, and a record over 64 KiB
  keeps its envelope and replaces `args` with a `{"chars", "sha256"}` summary (REQ-SEC-48).
  If that is still too big, every non-envelope field is folded into one summary.
- `seq` is taken before the write, so a record that fails leaves a gap that `verify` shows
  (REQ-SEC-40); the counter is per process, shared by every `AuditLog`.
- Any failure to write raises `AuditUnavailable` (an `OSError`): the hub refuses a call whose
  pre-execution record cannot be written, and no setting changes that.
- `content = true` parameters and result text are summarised by the hub before they reach
  this module (REQ-SEC-41), because only the hub knows the tool's parameter flags.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import re
import threading
from collections import Counter
from collections.abc import Callable, Collection, Iterator, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path

from halkit import files, paths, redact
from halkit.timeutil import iso_z, utc_now

from .policy.model import AuditSettings

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
KEEP_ROTATED = 99  # 01 "Audit log": append_jsonl(..., keep=99)
MAX_STRING_CHARS = 4096  # REQ-SEC-48
MAX_RECORD_BYTES = 64 * 2**10  # REQ-SEC-48
SESSION_FIELDS = ("session", "origin", "remote_addr", "channel")
ENVELOPE = ("v", "ts", "pid", "seq", "event", *SESSION_FIELDS, "profile", "call", "tool")
EVENTS = frozenset(
    {
        "decision",
        "self_approval_attempt",
        "approval_request",
        "approval",
        "approval_mismatch",
        "approval_refused",
        "review",
        "classification",
        "guard_scan",
        "result",
        "grant_revoked",
        "taint_change",
        "policy_loaded",
        "policy_error",
        "broker_start",
        "broker_stop",
        "session_start",
        "session_end",
        "listener_auth_failed",
        "model_verified",
        "model_rejected",
        "pack_installed",
        "pack_updated",
    }
)
_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl(?:\.(\d+))?$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

_seq = itertools.count(1)
_seq_lock = threading.Lock()


class AuditUnavailable(OSError):
    """An audit record could not be written."""


def default_directory() -> Path:
    """`state_dir()/audit`: the one audit log every front end writes (RESOLUTIONS B8)."""
    return paths.state_dir() / "audit"


def summary(text: str) -> dict[str, object]:
    """`{"chars": N, "sha256": "..."}` for text that is not logged in full (REQ-SEC-41)."""
    return {"chars": len(text), "sha256": hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()}


def _next_seq() -> int:
    with _seq_lock:
        return next(_seq)


def _cut(value: object) -> object:
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            return f"{value[:MAX_STRING_CHARS]}[+{len(value) - MAX_STRING_CHARS} chars]"
        return value
    if isinstance(value, Mapping):
        return {k: _cut(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_cut(v) for v in value]
    return value


def _encoded(record: Mapping[str, object]) -> str:
    return json.dumps(record, ensure_ascii=True, separators=(",", ":"))


def _bounded(record: dict[str, object]) -> dict[str, object]:
    """`record` within `MAX_RECORD_BYTES` (REQ-SEC-48)."""
    text = _encoded(record)
    if len(text) <= MAX_RECORD_BYTES:
        return record
    if "args" in record:
        record = {**record, "args": summary(_encoded({"args": record["args"]}))}
        text = _encoded(record)
        if len(text) <= MAX_RECORD_BYTES:
            return record
    kept = {k: record[k] for k in ENVELOPE if k in record}
    rest = {k: v for k, v in record.items() if k not in kept}
    return {**kept, "oversize": summary(_encoded(rest))}


class AuditLog:
    """Appends audit records; implements 04-hub's `EventSink`."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        settings: AuditSettings | None = None,
        secrets: Callable[[], Collection[str]] = tuple,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.directory = Path(directory) if directory is not None else default_directory()
        self.settings = settings or AuditSettings()
        self._secrets = secrets
        self._clock = clock

    def append(self, event: str, **fields: object) -> None:
        """Writes one record; raises `AuditUnavailable`."""
        self.record({"event": event, **fields})

    def record(self, event: Mapping[str, object]) -> None:
        """The mapping form `append` calls; `event["event"]` names the event."""
        now = self._clock()
        record: dict[str, object] = {
            "v": SCHEMA_VERSION,
            "ts": iso_z(now, ms=True),
            "pid": os.getpid(),
            "seq": _next_seq(),
        }
        record.update({name: None for name in SESSION_FIELDS})
        record.update(event)
        path = self.directory / f"{now.date().isoformat()}.jsonl"
        try:
            secrets = tuple(self._secrets())
            clean = redact.redact(record, secrets=secrets)
            assert isinstance(clean, dict)  # redact keeps the shape of a mapping
            # the envelope is ours, so redaction may not blank it (a session id is not a secret)
            clean.update({k: record[k] for k in ("v", "ts", "pid", "seq")})
            final = _bounded(_cut(clean))  # type: ignore[arg-type]  # _cut keeps a dict a dict
            paths.ensure_private_dir(self.directory)
            files.append_jsonl(path, final, max_bytes=self.settings.max_file_mb * 2**20, keep=KEEP_ROTATED)
        except (OSError, paths.UnsafePathError, TypeError, ValueError) as exc:
            raise AuditUnavailable(f"cannot write the audit log {path}: {exc}") from exc

    def prune(self) -> int:
        """Deletes day files (and their rotations) older than `audit.retention_days`; returns how many."""
        cutoff = self._clock().date() - timedelta(days=self.settings.retention_days)
        removed = 0
        for path, day, _ in _day_files(self.directory):
            if day < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except FileNotFoundError:
                    # another process removed it first
                    pass
                # append_jsonl's lock file for that day; nothing writes an old day again
                (self.directory / f"{day.isoformat()}.jsonl.lock").unlink(missing_ok=True)
        if removed:
            log.info("pruned %d audit files older than %s", removed, cutoff.isoformat())
        return removed


def _day_files(directory: Path) -> list[tuple[Path, date, int]]:
    """Day files oldest first: by date, and within a date the highest rotation first."""
    found = []
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return []
    for path in entries:
        match = _DAY_FILE.match(path.name)
        if match:
            try:
                day = date.fromisoformat(match.group(1))
            except ValueError:
                # a file whose name has no date is not an audit log
                continue
            found.append((path, day, int(match.group(2) or 0)))
    return sorted(found, key=lambda f: (f[1], -f[2]))


def _lines(path: Path) -> Iterator[tuple[int, str]]:
    with path.open(encoding="ascii", errors="replace") as fh:
        yield from enumerate(fh, 1)


def tail(
    directory: Path | None = None, *, n: int = 20, session: str | None = None
) -> Iterator[dict[str, object]]:
    """The last `n` records (all when `n` is 0), oldest first, optionally for one session.

    A line that does not parse is skipped with a warning; `verify` reports it.
    """
    directory = Path(directory) if directory is not None else default_directory()
    records: list[dict[str, object]] = []
    for path, _, _ in _day_files(directory):
        for number, line in _lines(path):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                log.warning("%s:%d: not a JSON record", path, number)
                continue
            if isinstance(record, dict) and (session is None or record.get("session") == session):
                records.append(record)
    yield from records[-n:] if n else records


def _schema_problem(record: object) -> str | None:
    if not isinstance(record, dict):
        return "not a JSON object"
    if record.get("v") != SCHEMA_VERSION:
        return f"schema version {record.get('v')!r}, expected {SCHEMA_VERSION}"
    if not isinstance(record.get("ts"), str) or not _TS.match(record["ts"]):
        return f"timestamp {record.get('ts')!r} is not UTC with milliseconds and Z"
    for name in ("pid", "seq"):
        if not isinstance(record.get(name), int) or isinstance(record.get(name), bool):
            return f"{name} is not an integer"
    if record.get("event") not in EVENTS:
        return f"unknown event {record.get('event')!r}"
    return None


def verify(directory: Path | None = None, *, date: str | None = None) -> list[str]:
    """Problems in the audit files (all, or one UTC date `YYYY-MM-DD`); empty when clean.

    Checks each line against the v1 schema, `seq` gaps per `pid` within a file, and that every
    `approval` with verdict `allow` follows an `approval_request` with the same key.
    """
    directory = Path(directory) if directory is not None else default_directory()
    problems: list[str] = []
    for path, day, _ in _day_files(directory):
        if date is not None and day.isoformat() != date:
            continue
        last_seq: dict[int, int] = {}
        requested: Counter[str] = Counter()
        for number, line in _lines(path):
            where = f"{path.name}:{number}"
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"{where}: not a JSON record")
                continue
            if (problem := _schema_problem(record)) is not None:
                problems.append(f"{where}: {problem}")
                continue
            pid, seq = record["pid"], record["seq"]
            if pid in last_seq and seq != last_seq[pid] + 1:
                problems.append(f"{where}: seq gap for pid {pid}: {last_seq[pid]} then {seq}")
            last_seq[pid] = seq
            if record["event"] == "approval_request":
                requested[str(record.get("key"))] += 1
            elif record["event"] == "approval" and record.get("verdict") == "allow":
                key = str(record.get("key"))
                if requested[key] <= 0:
                    problems.append(f"{where}: allow for key {key} with no approval_request before it")
                else:
                    requested[key] -= 1
    return problems
