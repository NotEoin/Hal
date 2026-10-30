"""Pid files for the long-running `hal` processes, so `hal kill` can find and stop them.

One file per process in `runtime_dir()/pids/`, named `<name>-<pid>.pid`, holding `pid starttime`.
The start time (the same `/proc` field `halsandbox` records for its sessions) stops `hal kill`
signalling an unrelated process that reused the pid after a crash left the file behind.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import signal
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from halkit import paths
from halsandbox.runtime import pidstart

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
GRACE_S = 5.0  # how long a process gets after SIGTERM before SIGKILL
POLL_S = 0.1


@dataclass(frozen=True)
class PidRecord:
    """A live process that registered itself."""

    name: str
    pid: int
    path: Path


def pid_dir() -> Path:
    """The private directory that holds the pid files."""
    return paths.ensure_private_dir(paths.runtime_dir() / "pids")


@contextlib.contextmanager
def registered(name: str) -> Iterator[Path]:
    """Writes a pid file for this process while the block runs and removes it afterwards."""
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"bad process name {name!r}: lowercase letters, digits and '-'")
    pid = os.getpid()
    path = pid_dir() / f"{name}-{pid}.pid"
    path.write_text(f"{pid} {pidstart(pid)}\n")
    path.chmod(0o600)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _read(path: Path) -> PidRecord | None:
    """The record for one file, or None when it is malformed or the process is gone or replaced."""
    try:
        pid_text, start_text = path.read_text().split()
        pid, start = int(pid_text), int(start_text)
        name = path.name.removesuffix(".pid").rsplit("-", 1)[0]
    except (OSError, ValueError):
        return None
    if pid <= 1 or start == 0 or pidstart(pid) != start or _is_zombie(pid):
        return None
    return PidRecord(name, pid, path)


def _is_zombie(pid: int) -> bool:
    """True for a process that exited but was not yet reaped by its parent (it holds no resources)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat.rpartition(")")[2].split()[0] == "Z"


def live() -> list[PidRecord]:
    """Registered processes that still run; files of dead or replaced ones are removed."""
    found: list[PidRecord] = []
    for path in sorted(pid_dir().glob("*.pid")):
        record = _read(path)
        if record is None:
            log.info("removing stale pid file %s", path.name)
            path.unlink(missing_ok=True)
        else:
            found.append(record)
    return found


def terminate(records: list[PidRecord], grace: float = GRACE_S) -> list[PidRecord]:
    """Sends SIGTERM, then SIGKILL after `grace` seconds; returns the records that survived."""
    for rec in records:
        _signal(rec, signal.SIGTERM)
    deadline = time.monotonic() + grace
    pending = records
    while pending and time.monotonic() < deadline:
        pending = [r for r in pending if _read(r.path) is not None]
        if pending:
            time.sleep(POLL_S)
    for rec in pending:
        _signal(rec, signal.SIGKILL)
    time.sleep(POLL_S)
    return [r for r in pending if _read(r.path) is not None]


def _signal(rec: PidRecord, sig: signal.Signals) -> None:
    try:
        os.kill(rec.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        log.warning("not allowed to signal %s (pid %d)", rec.name, rec.pid)
