"""Tests for reading and rendering the supervisor status file (REQ-RMT-30)."""

from __future__ import annotations

import json
import os

import pytest

from halremote import status
from halremote.model import ProfilePaths


@pytest.fixture
def paths(tmp_path):
    root = tmp_path / "cfg"
    return ProfilePaths(root, root / "c", root / "k", root / "t", root / "m", root / "g", tmp_path / "state")


def write(paths, **over):
    base = {
        "schema": "hal.remote.status/1", "profile": "desktop", "state": "up", "mode": "systemd",
        "supervisor_pid": os.getpid(), "ssh_pid": 1, "since": "2026-10-23T18:01:00Z", "attempt": 1,
        "next_retry_at": None, "last_error": None, "fix": None,
        "forwards": [{"name": "gateway", "local": "127.0.0.1:8741", "remote": "127.0.0.1:8741"}],
        "gateway": {"ok": True, "checked_at": "2026-10-23T18:05:30Z", "status": 200, "detail": "3 models"},
        **over,
    }  # fmt: skip
    paths.state.mkdir(parents=True, exist_ok=True)
    (paths.state / "status.json").write_text(json.dumps(base))


def test_no_file_means_never_run_and_exit_4(paths):
    assert status.read(paths) is None
    assert status.exit_code(None) == 4
    assert "not running" in status.render(None, "desktop", "desk.lan")


def test_up_and_healthy_renders_the_documented_line_and_exits_0(paths):
    write(paths)
    st = status.read(paths)
    assert status.render(st, "desktop", "desk.lan") == (
        "desktop  up since 18:01Z (systemd)   gateway ok (3 models)   127.0.0.1:8741 -> desk.lan:8741"
    )
    assert status.exit_code(st) == 0


def test_up_with_an_unhealthy_gateway_exits_4(paths):
    write(paths, gateway={"ok": False, "status": 401, "detail": "token rejected: rotate it"})
    assert status.exit_code(status.read(paths)) == 4


def test_failed_state_shows_the_error_and_the_fix(paths):
    write(
        paths,
        state="failed",
        last_error="key revoked",
        fix="hal remote setup desk.lan --rotate",
        gateway=None,
    )
    text = status.render(status.read(paths), "desktop", "desk.lan")
    assert "last error: key revoked" in text
    assert "fix: hal remote setup desk.lan --rotate" in text
    assert status.exit_code(status.read(paths)) == 4


def test_a_dead_supervisor_reads_as_stopped(paths):
    write(paths, supervisor_pid=2**22 + 12345)
    st = status.read(paths)
    assert st["state"] == "stopped"
    assert status.exit_code(st) == 4


def test_a_corrupt_or_foreign_file_is_an_error(paths):
    paths.state.mkdir(parents=True)
    (paths.state / "status.json").write_text("{")
    with pytest.raises(status.StatusError):
        status.read(paths)
    (paths.state / "status.json").write_text('{"schema": "other"}')
    with pytest.raises(status.StatusError):
        status.read(paths)


def test_windows_checks_a_pid_without_sending_it_ctrl_c(monkeypatch):
    # regression: os.kill(pid, 0) is CTRL_C_EVENT on Windows; checking pytest's own pid interrupted CI
    def no_kill(*_args):
        raise AssertionError("os.kill must not be called on Windows")

    monkeypatch.setattr(status, "WINDOWS", True)
    monkeypatch.setattr(status.os, "kill", no_kill)
    monkeypatch.setattr(status, "_windows_alive", lambda pid: pid == 4242)
    assert status.pid_alive(4242) and not status.pid_alive(4243)


@pytest.mark.skipif(os.name != "nt", reason="the Win32 process query exists only on Windows")
def test_the_windows_check_sees_this_process_and_not_one_that_exited():
    import subprocess  # noqa: PLC0415 - only this test starts a process
    import sys  # noqa: PLC0415 - only this test starts a process

    assert status.pid_alive(os.getpid())
    # Popen keeps the exited process's handle open, so Windows cannot hand its pid to another process
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=60)
    assert not status.pid_alive(child.pid)
