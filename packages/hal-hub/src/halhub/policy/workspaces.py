"""The workspaces of every live Hal session, which host tools may never touch (REQ-SEC-09).

A box can write anything inside its workspace, including symlinks, so a host tool that read or
wrote there could be redirected between the policy check and the tool's own `open`. Excluding
every active workspace removes that race without `openat2` (01-security Decision SD11).

Sessions are found from `runtime_dir()/s/*/owner.json` (02-sandbox, "Session runtime
directory"). A session counts as active when its `pid` is alive with the same `pidstart`
(field 22 of `/proc/<pid>/stat`), so a reused pid does not keep a dead session's workspace
excluded, and workspaces of other `hal` processes count too.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from halkit import paths as halpaths

log = logging.getLogger(__name__)

PROC = Path("/proc")
STARTTIME_FIELD = 22  # proc(5): field 22 of /proc/<pid>/stat is the start time in clock ticks


def process_start(pid: int, *, proc: Path = PROC) -> int | None:
    """The start time of process `pid` from `/proc/<pid>/stat`, or None when it is gone."""
    try:
        text = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    # the command name (field 2) may hold spaces and parentheses; fields resume after the last ")"
    rest = text.rsplit(")", 1)[-1].split()
    try:
        return int(rest[STARTTIME_FIELD - 3])
    except (IndexError, ValueError):
        return None


def active_workspaces(runtime: Path | None = None, *, proc: Path = PROC) -> tuple[Path, ...]:
    """Resolved workspace roots of every live session under `runtime` (default `runtime_dir()`)."""
    if runtime is None:
        try:
            runtime = halpaths.runtime_dir()
        except halpaths.RuntimeDirMissing:
            return ()  # no runtime dir means no session could have started
    found: list[Path] = []
    for owner in sorted((runtime / "s").glob("*/owner.json")):
        try:
            data = json.loads(owner.read_text())
            pid, pidstart, work = int(data["pid"]), int(data["pidstart"]), str(data["work"])
        except (OSError, ValueError, KeyError, TypeError) as err:
            log.warning("ignoring unreadable session owner file %s: %s", owner, err)
            continue
        if process_start(pid, proc=proc) != pidstart:
            continue
        found.append(Path(os.path.realpath(work)))
    return tuple(dict.fromkeys(found))
