"""Tests for `hal kill` with fakes for systemd, power and the container sweep."""
import subprocess
import sys

import pytest

from halcore import killcmd, pids, power, units


@pytest.fixture
def world(tmp_path, monkeypatch):
    rt = tmp_path / "rt"
    rt.mkdir(mode=0o700)
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HAL_ROOT", raising=False)
    calls = []
    monkeypatch.setattr(power, "off", lambda quiet=False: calls.append("off") or "")
    monkeypatch.setattr(power, "_notify", lambda *a: calls.append("notify"))
    monkeypatch.setattr(units, "installed", lambda: {"hal-voice.service": "/x"})
    monkeypatch.setattr(
        units, "systemctl", lambda *a, **k: calls.append(("stop", a)) or subprocess.CompletedProcess(a, 0, "", "")
    )
    return calls


def test_kill_stops_units_and_registered_processes_and_sweeps(world):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (pids.pid_dir() / f"acp-{child.pid}.pid").write_text(f"{child.pid} {pids.pidstart(child.pid)}\n")
        swept = []
        stopped, left = killcmd.kill_all(sweep=lambda: swept.append(1))
        assert (stopped, left) == (1, []) and swept == [1]
        assert child.wait(timeout=5) != 0
    finally:
        child.kill()
        child.wait()
    stop = [c for c in world if isinstance(c, tuple)][0][1]
    assert stop[0] == "stop" and "hal-voice.service" in stop and "hal.target" in stop
    assert world[0] == "off"


def test_kill_does_not_signal_a_process_that_never_registered(world):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert killcmd.kill_all(sweep=lambda: None) == (0, [])
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_main_exit_status_is_one_when_a_process_survives(world, monkeypatch, capsys):
    monkeypatch.setattr(killcmd, "kill_all", lambda **k: (1, ["acp (pid 7)"]))
    assert killcmd.main(["--quiet"]) == 1
    assert "acp (pid 7)" in capsys.readouterr().err
