"""Container labels and the orphan sweep (REQ-BOX-39, 41, 42).

The sweep removes only containers that carry both `hal.sandbox=1` and this user's
`hal.uid`, and whose recorded `hal.pid` is dead or was reused (`hal.pidstart` differs). It
checks the labels again on every row it reads, so a daemon that ignores a filter cannot make
it remove a foreign container. Runtime directories go only when `owner.json` names a dead
process, and an isolated copy with unaccepted changes is never removed (REQ-BOX-42).
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from halkit import paths, proc

from . import __version__, isolated
from .errors import SandboxError
from .runtime import pidstart, read_owner

log = logging.getLogger("halsandbox.labels")

LABEL_SANDBOX = "hal.sandbox"
LIST_TIMEOUT_S = 60.0  # REQ-BOX-41
REMOVE_TIMEOUT_S = 180.0  # the timeout a batch eval harness uses for `docker rm -f`
SWEEP_INTERVAL_S = 3600.0  # REQ-BOX-41: at most hourly at `hal` start
NO_OWNER_GRACE_S = 3600.0  # a directory without a readable owner.json may belong to a start in progress
EMPTY_ISOLATED_DAYS = 7
_LIST_LABELS = (LABEL_SANDBOX, "hal.uid", "hal.pid", "hal.pidstart")
_LIST_FORMAT = "\t".join(["{{.Names}}", *('{{.Label "' + k + '"}}' for k in _LIST_LABELS)])


def labels(
    kind: str,
    sid: str,
    *,
    version: str = __version__,
    uid: int | None = None,
    pid: int | None = None,
    start: int | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The labels every box container carries; `extra` is added after and never overrides them."""
    pid = os.getpid() if pid is None else pid
    base = {
        LABEL_SANDBOX: "1",
        "hal.kind": kind,
        "hal.session": sid,
        "hal.uid": str(os.getuid() if uid is None else uid),
        "hal.pid": str(pid),
        "hal.pidstart": str(pidstart(pid) if start is None else start),
        "hal.version": version,
    }
    return {**base, **{k: v for k, v in (extra or {}).items() if k not in base}}


@dataclass(frozen=True)
class SweepResult:
    """What one sweep did; `error` is set when docker could not be asked."""

    removed: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    dirs_removed: tuple[Path, ...] = ()
    error: str | None = None


def _alive(pid: int, start: int) -> bool:
    """True when `pid` runs with the recorded start time (a reused pid has another one)."""
    try:
        return pidstart(pid) == start
    except (OSError, ValueError):
        return False


def _containers(docker: str) -> list[tuple[str, bool]]:
    """(name, owner_alive) for each container carrying both own labels."""
    done = proc.run(
        [docker, "ps", "-a", "--no-trunc", "--filter", f"label={LABEL_SANDBOX}=1",
         "--filter", f"label=hal.uid={os.getuid()}", "--format", _LIST_FORMAT],
        timeout=LIST_TIMEOUT_S,
    )  # fmt: skip
    rows: list[tuple[str, bool]] = []
    for line in done.stdout.decode(errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) != 5 or parts[1] != "1" or parts[2] != str(os.getuid()):
            continue  # not ours, whatever the daemon's filter did
        try:
            alive = _alive(int(parts[3]), int(parts[4]))
        except ValueError:
            alive = False  # labelled as ours but unreadable liveness: treat as orphaned
        rows.append((parts[0], alive))
    return rows


def _sweep_dirs(dry_run: bool) -> list[Path]:
    root = paths.runtime_dir() / "s"
    removed: list[Path] = []
    if not root.is_dir():
        return removed
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        try:
            owner = read_owner(d)
            dead = not _alive(int(owner["pid"]), int(owner["pidstart"]))
        except (SandboxError, KeyError, ValueError, TypeError, OSError):
            dead = time.time() - d.stat().st_mtime > NO_OWNER_GRACE_S
        if not dead:
            continue
        if not dry_run:
            shutil.rmtree(d, ignore_errors=True)
        removed.append(d)
    return removed


def sweep_isolated(*, dry_run: bool = False) -> list[str]:
    """Removes isolated copies with no changes that are older than a week; keeps pending work."""
    gone: list[str] = []
    cutoff = time.time() - EMPTY_ISOLATED_DAYS * 86400
    for sid in isolated.sessions():
        iso = isolated.load(sid)
        if (iso.root / "session.json").stat().st_mtime > cutoff:
            continue
        try:
            pending = bool(isolated.changes(iso))
        except SandboxError:
            pending = True  # cannot tell: keep it
        if pending:
            continue
        if not dry_run:
            isolated.reject(iso)
        gone.append(sid)
    return gone


def sweep(*, docker: str | None = "docker", dry_run: bool = False) -> SweepResult:
    """Removes dead own boxes and dead runtime dirs; `docker=None` skips containers."""
    removed: list[str] = []
    kept: list[str] = []
    error: str | None = None
    if docker:
        try:
            for name, alive in _containers(docker):
                if alive:
                    kept.append(name)
                    continue
                if not dry_run:
                    proc.run([docker, "rm", "-f", name], timeout=REMOVE_TIMEOUT_S, check=False)
                removed.append(name)
                log.info("sweep removed orphan container %s", name)
        except (proc.ProcTimeout, OSError) as exc:
            error = f"docker: {exc}"
    dirs = _sweep_dirs(dry_run)
    if not dry_run:
        sweep_isolated()
    return SweepResult(tuple(removed), tuple(kept), tuple(dirs), error)


def sweep_if_due(*, docker: str | None = "docker", now: float | None = None) -> SweepResult | None:
    """Runs `sweep` at most once an hour (stamp in `last-sweep`); None when it is not due."""
    stamp = paths.ensure_private_dir(paths.state_dir() / "sandbox") / "last-sweep"
    now = time.time() if now is None else now
    try:
        if now - float(stamp.read_text()) < SWEEP_INTERVAL_S:
            return None
    except (OSError, ValueError):
        # no usable stamp means the sweep is due
        pass
    stamp.write_text(str(now))
    return sweep(docker=docker)
