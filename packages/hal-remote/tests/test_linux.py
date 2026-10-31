"""Tests for Linux tunnel control: unit rendering, systemd detection, detached start and stop."""

from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path

import pytest

from halremote import platform_linux as linux
from halremote.model import profile_paths

EXE = Path("/home/me/.local/bin/hal-remote")


class Systemctl:
    """A runner for `systemctl`: `state` is what `is-system-running` prints."""

    def __init__(self, state: str = "running", fail_on: str = "") -> None:
        self.state, self.fail_on = state, fail_on
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout, check=True, **_kw):
        assert timeout > 0
        self.calls.append(list(argv))
        if argv[2] == "is-system-running":
            return subprocess.CompletedProcess(
                argv, 1 if self.state != "running" else 0, self.state.encode(), b""
            )
        code = 1 if argv[2] == self.fail_on else 0
        return subprocess.CompletedProcess(argv, code, b"", b"Failed to connect to bus" if code else b"")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "hal-state"))
    pp = profile_paths("desktop")
    pp.state.mkdir(parents=True)
    return pp


def _proc_tree(tmp_path: Path, pid: int, cmdline: str, start: str) -> Path:
    root = tmp_path / "proc"
    (root / str(pid)).mkdir(parents=True, exist_ok=True)
    (root / str(pid) / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode())
    fields = ["S"] + ["0"] * 18 + [start]  # state, then fields 4 to 21; start time is field 22
    (root / str(pid) / "stat").write_text(f"{pid} (hal remote) {' '.join(fields)} 0 0\n")
    return root


def test_unit_restarts_a_crash_but_not_a_permanent_error():
    text = linux.render_unit(EXE)
    assert "ExecStart=/home/me/.local/bin/hal-remote tunnel-run --profile %i" in text
    assert "RestartPreventExitStatus=3" in text and "NoNewPrivileges=yes" in text


@pytest.mark.parametrize(("state", "usable"), [("running", True), ("degraded", True), ("offline", False)])
def test_systemd_is_usable_when_running_or_degraded(state, usable):
    assert linux.systemd_usable(Systemctl(state)) is usable


def test_up_with_systemd_installs_the_unit_reloads_and_starts(env):
    runner = Systemctl()
    assert linux.up("desktop", env, EXE, runner=runner) == "systemd"
    assert (linux.unit_dir() / linux.UNIT_TEMPLATE).read_text() == linux.render_unit(EXE)
    verbs = [c[2] for c in runner.calls]
    assert verbs == ["is-system-running", "daemon-reload", "start"]
    assert runner.calls[-1][-1] == "hal-remote-tunnel@desktop.service"


def test_up_does_not_reload_when_the_unit_is_unchanged(env):
    linux.up("desktop", env, EXE, runner=Systemctl())
    runner = Systemctl()
    linux.up("desktop", env, EXE, runner=runner, enable=True)
    assert [c[2] for c in runner.calls] == ["is-system-running", "start", "enable"]


def test_failed_start_names_the_systemctl_error(env):
    with pytest.raises(linux.TunnelControlError, match="Failed to connect"):
        linux.up("desktop", env, EXE, runner=Systemctl(fail_on="start"))


def test_up_without_systemd_spawns_a_detached_supervisor_and_records_it(env, tmp_path):
    root = _proc_tree(tmp_path, 4242, "hal-remote tunnel-run --profile desktop", "777")
    spawned: list[list[str]] = []
    mode = linux.up(
        "desktop",
        env,
        EXE,
        runner=Systemctl("offline"),
        spawn=lambda a: spawned.append(a) or 4242,
        proc_root=root,
    )
    assert mode == "detached"
    assert spawned[0][-2:] == ["--mode", "detached"]
    record = json.loads((env.state / linux.PID_RECORD).read_text())
    assert (record["pid"], record["started"]) == (4242, "777")


def test_up_twice_without_systemd_is_refused(env, tmp_path):
    root = _proc_tree(tmp_path, 4242, "hal-remote tunnel-run --profile desktop", "777")
    linux.up("desktop", env, EXE, runner=Systemctl("offline"), spawn=lambda a: 4242, proc_root=root)
    with pytest.raises(linux.TunnelControlError, match="already running"):
        linux.up("desktop", env, EXE, runner=Systemctl("offline"), spawn=lambda a: 1, proc_root=root)


def test_enable_without_systemd_is_an_error(env):
    with pytest.raises(linux.TunnelControlError, match="--enable"):
        linux.up("desktop", env, EXE, runner=Systemctl("offline"), enable=True)


def test_down_signals_only_a_matching_supervisor(env, tmp_path):
    root = _proc_tree(tmp_path, 4242, "hal-remote tunnel-run --profile desktop", "777")
    linux.up("desktop", env, EXE, runner=Systemctl("offline"), spawn=lambda a: 4242, proc_root=root)
    sent: list[tuple[int, int]] = []
    result = linux.down(
        "desktop", env, runner=Systemctl("offline"), kill=lambda p, s: sent.append((p, s)), proc_root=root
    )
    assert result == "detached" and sent == [(4242, signal.SIGTERM)]


def test_down_ignores_a_recycled_pid(env, tmp_path):
    root = _proc_tree(tmp_path, 4242, "hal-remote tunnel-run --profile desktop", "777")
    linux.up("desktop", env, EXE, runner=Systemctl("offline"), spawn=lambda a: 4242, proc_root=root)
    _proc_tree(tmp_path, 4242, "hal-remote tunnel-run --profile desktop", "999")  # same pid, new process
    sent: list = []
    result = linux.down(
        "desktop", env, runner=Systemctl("offline"), kill=lambda *a: sent.append(a), proc_root=root
    )
    assert result == "not running" and sent == []


def test_down_with_systemd_stops_and_disables(env):
    runner = Systemctl()
    assert linux.down("desktop", env, disable=True, runner=runner) == "systemd"
    assert [c[2] for c in runner.calls] == ["is-system-running", "stop", "disable"]


def test_process_start_time_survives_a_comm_with_spaces_and_parentheses(tmp_path):
    root = tmp_path / "proc" / "5"
    root.mkdir(parents=True)
    fields = ["S"] + ["0"] * 18 + ["321"]
    (root / "stat").write_text(f"5 (a) b) {' '.join(fields)} 0\n")
    assert linux.process_start_time(5, tmp_path / "proc") == "321"
