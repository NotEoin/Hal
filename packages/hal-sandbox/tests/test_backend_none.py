"""Tests for backend `none`: host argv mapping, environment and session capabilities."""

import json
import os
from pathlib import Path

import pytest

from halsandbox import detect
from halsandbox.backends.docker import LaunchSpec
from halsandbox.backends.none import none_launch
from halsandbox.errors import SandboxError
from halsandbox.limits import Limits
from halsandbox.session import Session


def spec(tmp_path: Path, **over) -> LaunchSpec:
    base = dict(
        kind="code", sid="0123456789ab", image="x", runtime="runc", work=tmp_path / "w",
        session_root=tmp_path / "s", uid=1000, gid=1000, limits=Limits(2**31, 1.0),
        env={"HOME": "/home/hal", "HAL_SESSION": "0123456789ab", "PIP_NO_INDEX": "1"},
        child=["hal-agent", "--acp", "--config", "/run/hal/agent.json", "-C", "/work"],
        pid=1, pidstart=1, started="2026-10-30T00:00:00Z",
    )  # fmt: skip
    base.update(over)
    (tmp_path / "s").mkdir(exist_ok=True)
    return LaunchSpec(**base)


def test_child_paths_are_mapped_to_host_paths(tmp_path):
    launch = none_launch(spec(tmp_path), {"PATH": "/usr/bin"})
    assert launch.argv[-4:] == ["--config", f"{tmp_path}/s/run/agent.json", "-C", f"{tmp_path}/w"]
    assert launch.cwd == str(tmp_path / "w") and launch.new_session


def test_environment_is_confined_and_names_the_host_sockets(tmp_path):
    env = none_launch(spec(tmp_path), {"PATH": "/usr/bin", "SECRET_TOKEN": "x"}).env
    assert env["HAL_SANDBOX"] == "none" and env["HAL_WORKSPACE"] == str(tmp_path / "w")
    assert env["HAL_BROKER_SOCKET"] == f"{tmp_path}/s/run/broker.sock"
    assert env["HOME"] == f"{tmp_path}/s/home" and "SECRET_TOKEN" not in env and "PIP_NO_INDEX" not in env


def test_session_with_backend_none_says_run_false_and_paths_confined(tmp_path, sockdir, monkeypatch):
    os.chmod(sockdir, 0o700)
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(sockdir))
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    cfg = tmp_path / "off.toml"
    cfg.write_text('[sandbox]\nlockdown = "off"\n')
    monkeypatch.setenv("HAL_CONFIG", str(cfg))
    probe = detect.Probe("none", True, None, None, "no isolation", "2026-10-30T00:00:00Z")
    monkeypatch.setattr(detect, "detect", lambda *a, **k: probe)
    project = tmp_path / "p"
    project.mkdir()
    with pytest.raises(SandboxError):  # no recorded consent and no terminal
        Session.start(project, isolated_copy=False, checkpoints=False)
    from halsandbox import projects

    projects.set_choice(project, "none_consent", is_tty=lambda: True, ask=lambda _p: projects.CONFIRM_WORD)
    s = Session.start(project, isolated_copy=False, checkpoints=False)
    try:
        data = json.loads((s.rd.run / "session.json").read_text())
        assert data["sandbox"]["run"] is False and data["sandbox"]["paths"] == "confined"
        assert s.launch.env["HAL_SANDBOX"] == "none" and s.launch.new_session
    finally:
        s.close()


def test_a_session_without_a_work_tree_works_in_scratch(tmp_path):
    scratch = tmp_path / "scratch"
    launch = none_launch(
        spec(tmp_path, kind="chat", work=None, scratch=scratch), host_env={"PATH": "/usr/bin"}
    )
    assert launch.cwd == str(scratch)
    assert launch.env["HAL_WORKSPACE"] == str(scratch) and launch.env["HAL_SCRATCH"] == str(scratch)
