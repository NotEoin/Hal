"""Tests for Windows tunnel control with a scripted `tasklist`, `taskkill` and `schtasks`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from halremote import platform_windows as win
from halremote.model import profile_paths

EXE = Path("C:/Users/me/AppData/hal-remote-tunnelw.exe")


class Tools:
    """Runner for the Windows tools; `alive` maps pid to image name."""

    def __init__(
        self, alive: dict[int, str] | None = None, schtasks_exit: int = 0, exits_after_stop: bool = True
    ):
        self.alive = dict(alive or {})
        self.schtasks_exit = schtasks_exit
        self.exits_after_stop = exits_after_stop
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout, check=True, **_kw):
        assert timeout > 0
        self.calls.append(list(argv))
        tool = argv[0]
        if tool == "tasklist":
            pid = int(argv[argv.index("/FI") + 1].split()[-1])
            out = (
                f'"{self.alive[pid]}","{pid}","Console","1","10,000 K"\n'
                if pid in self.alive
                else "INFO: No tasks.\n"
            )
            return subprocess.CompletedProcess(argv, 0, out.encode(), b"")
        if tool == "taskkill":
            self.alive.pop(int(argv[2]), None)
        code = self.schtasks_exit if tool == "schtasks" else 0
        return subprocess.CompletedProcess(argv, code, b"", b"Access is denied" if code else b"")


@pytest.fixture
def pp(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    paths = profile_paths("desktop")
    paths.state.mkdir(parents=True)
    return paths


def test_up_records_the_pid_of_a_detached_supervisor(pp):
    spawned = []
    mode = win.up("desktop", pp, EXE, runner=Tools(), spawn=lambda a: spawned.append(a) or 77)
    assert mode == "detached" and spawned[0][1:3] == ["tunnel-run", "--profile"]
    assert win._read_pid(pp.state / win.PID_RECORD) == 77


def test_up_while_running_is_refused(pp):
    tools = Tools({77: win.GUI_IMAGE})
    win.up("desktop", pp, EXE, runner=Tools(), spawn=lambda a: 77)
    with pytest.raises(win.WindowsControlError, match="already running"):
        win.up("desktop", pp, EXE, runner=tools, spawn=lambda a: 1)


def test_up_clears_a_stale_stop_file(pp):
    (pp.state / "stop").write_text("x")
    win.up("desktop", pp, EXE, runner=Tools(), spawn=lambda a: 5)
    assert not (pp.state / "stop").exists()


def test_down_writes_the_stop_file_waits_then_kills_the_tree(pp):
    win.up("desktop", pp, EXE, runner=Tools(), spawn=lambda a: 77)
    tools, slept = Tools({77: win.GUI_IMAGE}), []
    assert win.down("desktop", pp, runner=tools, sleep=slept.append) == "detached"
    assert slept == [win.STOP_WAIT_S]
    assert ["taskkill", "/PID", "77", "/T", "/F"] in tools.calls
    assert not (pp.state / win.PID_RECORD).exists()


def test_down_does_not_kill_a_pid_that_belongs_to_another_program(pp):
    win.up("desktop", pp, EXE, runner=Tools(), spawn=lambda a: 77)
    tools = Tools({77: "notepad.exe"})
    assert win.down("desktop", pp, runner=tools, sleep=lambda s: None) == "not running"
    assert not any(c[0] == "taskkill" for c in tools.calls)


def test_install_task_uses_schtasks(pp):
    tools = Tools()
    assert win.install_logon("desktop", EXE, tools) == "task"
    argv = tools.calls[0]
    assert argv[argv.index("/TN") + 1] == "Hal\\remote-desktop" and "ONLOGON" in argv


def test_install_task_falls_back_to_a_startup_file_when_denied(pp):
    assert win.install_logon("desktop", EXE, Tools(schtasks_exit=1)) == "startup"
    text = win.startup_file("desktop").read_text()
    assert text.startswith('@start ""') and "tunnel-run --profile desktop" in text


def test_down_disable_removes_the_task_and_the_startup_file(pp):
    win.install_logon("desktop", EXE, Tools(schtasks_exit=1))
    tools = Tools()
    win.down("desktop", pp, disable=True, runner=tools)
    assert not win.startup_file("desktop").exists()
    assert any(c[:2] == ["schtasks", "/Delete"] for c in tools.calls)
