"""Tests for host-side git safety during isolated sessions (ATK-12, REQ-BOX-48).

The agent controls files in `/work`, including git config and hooks in anything it creates, so
the host's git calls must never run them.
"""

import os
import subprocess
from pathlib import Path

import pytest

from halsandbox import isolated

CAP = 10**7


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", *args], cwd=cwd, check=True, capture_output=True
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _run(r, "init", "-q")
    (r / "app.py").write_text("x = 1\n")
    _run(r, "add", "app.py")
    _run(r, "commit", "-qm", "init")
    return r


def _trap(path: Path) -> Path:
    """A script that records that it ran, as a hook or fsmonitor command would."""
    marker = path.parent / "ran.marker"
    path.write_text(f"#!/bin/sh\necho ran > {marker}\n")
    path.chmod(0o755)
    return marker


def test_nested_repo_fsmonitor_never_runs(repo, tmp_path):
    iso = isolated.create(repo, CAP)
    nested = iso.work / "vendored"
    nested.mkdir()
    _run(nested, "init", "-q")
    marker = _trap(tmp_path / "monitor.sh")
    _run(nested, "config", "core.fsmonitor", str(marker.parent / "monitor.sh"))
    (nested / "f.txt").write_text("f\n")
    isolated.changes(iso)
    isolated.accept(iso)
    assert not marker.exists()


def test_a_hook_the_agent_plants_in_the_work_tree_never_runs(repo, tmp_path):
    iso = isolated.create(repo, CAP)
    hooks = iso.work / ".git" / "hooks"
    hooks.mkdir(parents=True)
    marker = _trap(hooks / "pre-commit")
    (iso.work / "new.txt").write_text("n\n")
    isolated.changes(iso)
    assert not marker.exists() and not (tmp_path / "ran.marker").exists()


def test_inherited_git_environment_is_not_passed_to_git(repo, monkeypatch, tmp_path):
    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT", "GIT_SSH_COMMAND"):
        monkeypatch.setenv(name, str(other))
    iso = isolated.create(repo, CAP)
    (iso.work / "new.txt").write_text("n\n")
    assert [c.path for c in isolated.changes(iso)] == ["new.txt"]
    assert not any(other.glob("refs/heads/*"))


def test_git_runs_with_no_hooks_fsmonitor_or_global_config(repo):
    iso = isolated.create(repo, CAP)
    env = isolated._base_env()
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    args = " ".join(isolated._GIT_BASE)
    assert "core.hooksPath=/dev/null" in args and "core.fsmonitor=false" in args
    assert iso.gitdir.is_dir()
