"""Phase-1 stand-in for 01-security's `halhub.audit`: `AuditLog` as the hub's `EventSink`.

Writes 01-security.md, "Audit log": one JSON line per record in
`state_dir()/audit/<UTC date>.jsonl` (directory 0700, files 0600, rotated at
`audit.max_file_mb`), with the common fields `v`, `ts` (UTC, milliseconds, `Z`), `pid` and a
per-process `seq`. The caller supplies `event`, the session fields and the event fields.
Phase 2 replaces this module with an import change in the hub.

Design decisions:

- `seq` is taken before the write, so a record that fails leaves a gap that `verify` can
  show (REQ-SEC-40); the counter is per process, shared by every `AuditLog`, as 01 defines.
- Any failure to write raises `AuditUnavailable` (an `OSError`): the hub refuses a call whose
  pre-execution record cannot be written, and there is no setting that changes it.
- Missing until 01 lands: `halkit.redact`'s key and value rules (only the exact known secret
  values are replaced here), retention pruning, and `verify`.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
from collections.abc import Callable, Collection, Iterator, Mapping
from datetime import datetime
from pathlib import Path

from halkit import files, paths
from halkit.timeutil import iso_z, utc_now

from ._policy_standin import AuditSettings

SCHEMA_VERSION = 1
KEEP_ROTATED = 99  # 01 "Audit log": append_jsonl(..., keep=99)
REDACTED = "[redacted]"

_seq = itertools.count(1)
_seq_lock = threading.Lock()


class AuditUnavailable(OSError):
    """An audit record could not be written."""


def default_directory() -> Path:
    """`state_dir()/audit`: the one audit log every front end writes (RESOLUTIONS B8)."""
    return paths.state_dir() / "audit"


def _next_seq() -> int:
    with _seq_lock:
        return next(_seq)


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
        record = {"v": SCHEMA_VERSION, "ts": iso_z(now, ms=True), "pid": os.getpid(), "seq": _next_seq()}
        record.update(event)
        needles = sorted((s for s in self._secrets() if s), key=len, reverse=True)
        if needles:
            record = _scrub(record, needles)
        path = self.directory / f"{now.date().isoformat()}.jsonl"
        try:
            paths.ensure_private_dir(self.directory)
            files.append_jsonl(path, record, max_bytes=self.settings.max_file_mb * 2**20, keep=KEEP_ROTATED)
        except (OSError, paths.UnsafePathError, TypeError, ValueError) as exc:
            raise AuditUnavailable(f"cannot write the audit log {path}: {exc}") from exc


def _scrub(value: object, needles: list[str]) -> object:
    if isinstance(value, str):
        for needle in needles:
            value = value.replace(needle, REDACTED)
        return value
    if isinstance(value, Mapping):
        return {k: _scrub(v, needles) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(v, needles) for v in value]
    return value


def tail(
    directory: Path | None = None, *, n: int = 20, session: str | None = None
) -> Iterator[dict[str, object]]:
    """The last `n` records across the day files, oldest first, optionally for one session."""
    directory = Path(directory) if directory is not None else default_directory()
    records: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.jsonl")):
        with path.open(encoding="ascii") as fh:
            for line in fh:
                record = json.loads(line)
                if session is None or record.get("session") == session:
                    records.append(record)
    yield from records[-n:] if n else records
