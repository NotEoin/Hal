"""Tests for dependency prep: detection, offers, approval, lockdown and the prep box command line."""

from __future__ import annotations

import os
import subprocess

import pytest

from halsandbox import deps, detect
from halsandbox.errors import SandboxError

LOCK = "# synthetic lockfile\n"


@pytest.fixture
def env(sockdir, tmp_path, monkeypatch):
    os.chmod(sockdir, 0o700)
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(sockdir))
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    cfg = tmp_path / "config.toml"
    cfg.write_text('[sandbox]\nlockdown = "relaxed"\n')
    monkeypatch.setenv("HAL_CONFIG", str(cfg))
    log = tmp_path / "docker.log"
    fake = tmp_path / "docker"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\nif [ "$1" = run ]; then exit 0; fi\n')
    fake.chmod(0o755)
    monkeypatch.setenv("HAL_DOCKER", str(fake))
    probe = detect.Probe("docker", True, "runc", "rootful", "ok", "2026-10-30T00:00:00Z", None)
    monkeypatch.setattr(detect, "detect", lambda *a, **k: probe)
    return log


@pytest.fixture
def work(tmp_path):
    w = tmp_path / "work"
    w.mkdir()
    (w / "uv.lock").write_text(LOCK)
    return w


def test_commands_per_lockfile(work):
    (work / "package-lock.json").write_text("{}")
    (work / "Cargo.lock").write_text(LOCK)
    got = {lf.manager: deps.command_for(lf) for lf in deps.detect_lockfiles(work)}
    assert got == {
        "uv": ("uv", "sync", "--frozen"),
        "npm": ("npm", "ci", "--no-audit", "--no-fund"),
        "cargo": ("cargo", "fetch", "--locked"),
    }


def test_a_symlinked_lockfile_is_not_detected(work, tmp_path):
    (work / "uv.lock").unlink()
    (tmp_path / "elsewhere").write_text(LOCK)
    (work / "uv.lock").symlink_to(tmp_path / "elsewhere")
    assert deps.detect_lockfiles(work) == []


def test_prep_env_pins_image_python_and_drops_only_the_offline_switches():
    env = deps.prep_env("0123456789ab")
    assert env["UV_PYTHON"] == "/usr/bin/python3" and env["UV_PYTHON_DOWNLOADS"] == "never"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:3128" and "HTTP_PROXY" not in env
    assert not any(name in env for name in deps.OFFLINE_NAMES)
    assert env["UV_PROJECT_ENVIRONMENT"] == "/work/.hal/venv"


def test_offer_only_when_lockfile_changed(env, work):
    assert [lf.path for lf in deps.pending(work)] == ["uv.lock"]
    deps._record(work, deps.detect_lockfiles(work)[0])
    assert deps.pending(work) == []
    (work / "uv.lock").write_text(LOCK + "# changed\n")
    assert [lf.path for lf in deps.pending(work)] == ["uv.lock"]


def test_denied_approval_runs_nothing(env, work):
    assert deps.prepare(work, approve=lambda lf: False) == []
    assert not env.exists()  # the fake docker was never called


def test_requires_docker(env, work, monkeypatch):
    other = detect.Probe("bwrap", True, None, "", "ok", "2026-10-30T00:00:00Z", None)
    monkeypatch.setattr(detect, "detect", lambda *a, **k: other)
    with pytest.raises(SandboxError, match="docker"):
        deps.prepare(work, approve=lambda lf: True)


def test_standard_lockdown_refuses_the_prep_box(env, work, tmp_path):
    (tmp_path / "config.toml").write_text('[sandbox]\nlockdown = "standard"\n')
    with pytest.raises(SandboxError, match="relaxed"):
        deps.prepare(work, approve=lambda lf: True)


def test_successful_prep_records_the_hash_and_writes_a_gitignore(env, work):
    (result,) = deps.prepare(work, approve=lambda lf: True)
    assert result.ok and deps.pending(work) == []
    assert (work / ".hal" / ".gitignore").read_text() == "*\n"
    run_line = next(line for line in env.read_text().splitlines() if line.startswith("run "))
    assert "--network none" in run_line and "--relay-socket /run/hal/egress.sock" in run_line
    assert "uv sync --frozen" in run_line and "broker.sock" not in run_line and "model.sock" not in run_line


def test_failed_prep_is_not_recorded(env, work, tmp_path):
    (tmp_path / "docker").write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    (result,) = deps.prepare(work, approve=lambda lf: True)
    assert not result.ok and result.exit_code == 3 and "boom" in result.log_tail
    assert len(deps.pending(work)) == 1


def test_timeout_stops_prep_box(env, work, tmp_path):
    (tmp_path / "docker").write_text(f'#!/bin/sh\necho "$@" >> {env}\nif [ "$1" = run ]; then sleep 5; fi\n')
    (result,) = deps.prepare(work, approve=lambda lf: True, timeout_s=0.5)
    assert not result.ok and "timed out" in result.log_tail
    assert any(line.startswith("kill hal-deps-") for line in env.read_text().splitlines())


def test_checkpoint_before_prep(env, work):
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    deps.prepare(work, approve=lambda lf: True)
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)"], cwd=work, capture_output=True, text=True
    ).stdout
    assert "hal" in refs or (work / ".git").exists()
