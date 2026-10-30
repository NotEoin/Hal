"""Containment suite, git side: a hostile box process plants hooks and config in the copy (RESOLUTIONS I6).

The attacker runs in the real box (`docker_argv`, `runc` and `runsc` when registered) on the isolated
copy of a real git project. It plants a git hook, a `core.fsmonitor` config, a nested repository and
a husky hook. The host-side report and `accept` must never run any of it, must refuse the high-risk
paths without `--allow-flags`, and must leave the real repository's hooks and config untouched.
Needs docker and a local `alpine:3.20`; skipped when either is missing.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from halsandbox import isolated
from halsandbox import runtime as sbruntime
from halsandbox.backends.docker import LaunchSpec, docker_argv
from halsandbox.code import docker_runtimes
from halsandbox.env import box_env
from halsandbox.limits import Limits

pytestmark = pytest.mark.docker

IMAGE = "alpine:3.20"  # a few MB; tests never pull it
RUN_TIMEOUT_S = 120
GIT_TIMEOUT_S = 30
MIB = 1 << 20
LIMITS = Limits(memory_bytes=256 * MIB, cpus=1.0, pids=64, tmp_bytes=32 * MIB)
MAX_COPY_BYTES = 10 * MIB


def available_runtimes() -> list[str]:
    try:
        have = docker_runtimes()
        subprocess.run(["docker", "image", "inspect", IMAGE], check=True, capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001 - any failure means "no usable docker here", so skip
        return []
    return [r for r in ("runc", "runsc") if r in have]


RUNTIMES = available_runtimes()


def git(cwd, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=True
    )
    return done.stdout


@pytest.mark.parametrize(
    "runtime", RUNTIMES or [pytest.param("none", marks=pytest.mark.skip("no docker or image"))]
)
def test_planted_git_hooks_and_config_never_run_or_reach_the_real_repo(tmp_path, runtime, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    project = tmp_path / "proj"
    project.mkdir()
    git(project, "init", "-q")
    (project / "a.txt").write_text("a\n")
    git(project, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "a.txt")
    git(project, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "base")
    hooks_before = sorted(p.name for p in (project / ".git" / "hooks").iterdir())
    config_before = (project / ".git" / "config").read_text()

    iso = isolated.create(project, MAX_COPY_BYTES)
    marker = tmp_path / "HOST-RAN-PLANTED-CODE"
    iso.work.chmod(0o777)  # the box user is the caller's uid; this keeps the test portable to rootless docker
    root = tmp_path / "session"
    (root / "run").mkdir(parents=True)
    (root / "etc").mkdir()
    (root / "etc" / "passwd").write_text(sbruntime.passwd_text(os.getuid(), os.getgid()))
    (root / "etc" / "group").write_text(sbruntime.group_text(os.getgid()))
    spec = LaunchSpec(
        kind="code", sid="0123456789ab", image=IMAGE, runtime=runtime, work=iso.work, session_root=root,
        uid=os.getuid(), gid=os.getgid(), limits=LIMITS, env=box_env("code", "0123456789ab", "docker", {}),
        child=(), pid=os.getpid(), pidstart=1, started="2026-10-30T00:00:00Z", scratch=None, entrypoint=None,
    )  # fmt: skip
    attack = (
        "mkdir -p /work/.git/hooks /work/sub/.git /work/.husky; "
        f"printf '#!/bin/sh\\ntouch {marker}\\n' > /work/.git/hooks/pre-commit; "
        "chmod +x /work/.git/hooks/pre-commit; "
        f"printf '[core]\\n\\tfsmonitor = touch {marker}\\n\\thooksPath = /work/.git/hooks\\n' "
        "> /work/.git/config; "
        f"printf '[core]\\n\\tfsmonitor = touch {marker}\\n' > /work/sub/.git/config; "
        f"printf '#!/bin/sh\\ntouch {marker}\\n' > /work/.husky/pre-commit; "
        "echo 'b' > /work/b.txt; echo done"
    )
    base = docker_argv(spec)
    argv = base[: base.index(IMAGE) + 1] + ["sh", "-c", attack]
    out = subprocess.run(argv, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False)
    assert "done" in out.stdout, out.stderr

    assert (project / "a.txt").read_text() == "a\n" and not (project / "b.txt").exists()
    changed = {c.path for c in isolated.changes(iso)}
    assert "b.txt" in changed and not any(p.startswith(".git/") or p.startswith("sub/.git") for p in changed)
    with pytest.raises(isolated.AcceptRefused, match="high-risk"):
        isolated.accept(iso)
    assert not (project / "b.txt").exists()
    isolated.accept(iso, allow_flags=True)
    git(project, "status", "--short")  # a host git call in the real repo must not run a planted hook
    assert (project / "b.txt").exists() and not marker.exists()
    assert sorted(p.name for p in (project / ".git" / "hooks").iterdir()) == hooks_before
    assert (project / ".git" / "config").read_text() == config_before
    assert not (project / "sub" / ".git").exists()
