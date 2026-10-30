"""Tests for the container labels and the orphan sweep, using a fake docker script."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from halsandbox import labels as lb
from halsandbox.runtime import RuntimeDir, pidstart

DEAD_PID = 2**22 + 12345  # above the default pid_max, so never alive


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Private state and runtime dirs."""
    state, run = tmp_path / "state", tmp_path / "r"
    run.mkdir(mode=0o700)
    monkeypatch.setenv("HAL_STATE_DIR", str(state))
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(run))
    return tmp_path


def fake_docker(tmp_path: Path, rows: list[str]) -> tuple[str, Path]:
    """A script that prints `rows` for `ps` and logs every other call to `calls`."""
    calls = tmp_path / "calls"
    script = tmp_path / "docker"
    listing = "\n".join(rows)
    script.write_text(
        f'#!/bin/sh\nif [ "$1" = ps ]; then cat <<\'EOF\'\n{listing}\nEOF\nelse echo "$@" >> {calls}; fi\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), calls


def row(name: str, *, sandbox="1", uid=None, pid=None, start=None) -> str:
    uid = os.getuid() if uid is None else uid
    return f"{name}\t{sandbox}\t{uid}\t{pid}\t{start}"


def test_labels_cannot_be_overridden_by_extra():
    got = lb.labels("code", "0123456789ab", extra={"hal.uid": "0", "hal.started": "t"})
    assert got["hal.uid"] == str(os.getuid())
    assert got["hal.started"] == "t"
    assert got["hal.sandbox"] == "1"


def test_sweep_removes_only_dead_own_boxes(env):
    me = os.getpid()
    rows = [
        row("hal-code-live", pid=me, start=pidstart(me)),
        row("hal-code-dead", pid=DEAD_PID, start=1),
        row("hal-code-reused", pid=me, start=pidstart(me) + 1),
        row("other-uid", uid=os.getuid() + 1, pid=DEAD_PID, start=1),
        row("not-hal", sandbox="", pid=DEAD_PID, start=1),
    ]
    docker, calls = fake_docker(env, rows)
    result = lb.sweep(docker=docker)
    assert set(result.removed) == {"hal-code-dead", "hal-code-reused"}
    assert result.kept == ("hal-code-live",)
    removed = calls.read_text().splitlines()
    assert sorted(removed) == ["rm -f hal-code-dead", "rm -f hal-code-reused"]


def test_sweep_dry_run_removes_nothing(env):
    docker, calls = fake_docker(env, [row("hal-code-dead", pid=DEAD_PID, start=1)])
    assert lb.sweep(docker=docker, dry_run=True).removed == ("hal-code-dead",)
    assert not calls.exists()


def test_sweep_reports_a_missing_docker_and_still_sweeps_dirs(env):
    result = lb.sweep(docker=str(env / "no-such-docker"))
    assert result.error and result.removed == ()


def test_sweep_runtime_dirs(env):
    live = RuntimeDir.create()
    live.write_owner(kind="code", backend="docker", container="c", work="/w")
    dead = RuntimeDir.create()
    owner = dead.write_owner(kind="code", backend="docker", container="c", work="/w")
    owner.update(pid=DEAD_PID)
    (dead.root / "owner.json").write_text(json.dumps(owner))
    fresh = RuntimeDir.create()  # no owner.json yet: a start in progress
    old = RuntimeDir.create()
    past = time.time() - 2 * lb.NO_OWNER_GRACE_S
    os.utime(old.root, (past, past))
    result = lb.sweep(docker=None)
    assert set(result.dirs_removed) == {dead.root, old.root}
    assert live.root.is_dir() and fresh.root.is_dir()


def test_sweep_if_due_runs_once_an_hour(env):
    assert lb.sweep_if_due(docker=None, now=1000.0) is not None
    assert lb.sweep_if_due(docker=None, now=1000.0 + lb.SWEEP_INTERVAL_S - 1) is None
    assert lb.sweep_if_due(docker=None, now=1000.0 + lb.SWEEP_INTERVAL_S + 1) is not None


def _old(iso) -> None:
    past = time.time() - (lb.EMPTY_ISOLATED_DAYS + 1) * 86400
    os.utime(iso.root / "session.json", (past, past))


def test_isolated_gc_keeps_pending_work(env):
    import subprocess

    from halsandbox import isolated

    project = env / "proj"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "a.txt").write_text("one\n")
    clean = isolated.create(project, 10**7)
    dirty = isolated.create(project, 10**7)
    (dirty.work / "a.txt").write_text("two\n")
    _old(clean)
    _old(dirty)
    assert lb.sweep_isolated() == [clean.sid]
    assert isolated.sessions() == [dirty.sid]
