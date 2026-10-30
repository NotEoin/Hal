"""Tests for `halsandbox.session.Session` with detection faked and no real docker."""

import json
import os
import stat
from pathlib import Path

import pytest

from halsandbox import detect, runtime
from halsandbox.errors import LockdownError, SandboxError
from halsandbox.session import Session


@pytest.fixture(autouse=True)
def env(sockdir, tmp_path, monkeypatch):
    os.chmod(sockdir, 0o700)
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(sockdir))
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "none.toml"))
    log = tmp_path / "docker.log"
    fake = tmp_path / "docker"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    fake.chmod(0o755)
    monkeypatch.setenv("HAL_DOCKER", str(fake))
    probe = detect.Probe("docker", True, "runc", "rootful", "ok", "2026-10-30T00:00:00Z", None)
    monkeypatch.setattr(detect, "detect", lambda *a, **k: probe)
    return log


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    (p / "a.py").write_text("print(1)\n")
    (p / ".env").write_text("TOKEN=synthetic\n")
    return p


@pytest.fixture
def session(project):
    s = Session.start(project, channel="cli")
    yield s
    s.close()


def test_code_session_works_on_an_isolated_copy_and_never_mounts_the_project(session, project):
    assert session.iso is not None and session.work != project
    joined = " ".join(session.agent_argv)
    assert str(project) not in joined and str(session.work) in joined


def test_runtime_dir_has_owner_json_and_session_json(session):
    owner = json.loads((session.rd.root / "owner.json").read_text())
    assert set(owner) == set(runtime.OWNER_KEYS) and owner["backend"] == "docker"
    data = json.loads((session.rd.run / "session.json").read_text())
    assert (
        data["sandbox"]["isolated"] is True and data["sandbox"]["masked"] == []
    )  # the copy never holds .env
    assert stat.S_IMODE(session.rd.root.stat().st_mode) == 0o700


def test_broker_and_lifeline_sockets_exist_after_start(session):
    assert session.broker_socket.exists() and session.rd.socket("lifeline.sock").exists()


def test_masked_file_is_bound_over_with_the_empty_file_in_direct_mode(project, tmp_path, monkeypatch):
    cfg = tmp_path / "relaxed.toml"
    cfg.write_text('[sandbox]\nlockdown = "relaxed"\n')
    monkeypatch.setenv("HAL_CONFIG", str(cfg))
    s = Session.start(project, isolated_copy=False, checkpoints=False)
    try:
        assert s.masked == (".env",)
        assert any(a.endswith('"dst=/work/.env",readonly') for a in s.agent_argv)
    finally:
        s.close()


def test_direct_mode_is_refused_under_the_default_lockdown(project):
    with pytest.raises(LockdownError):
        Session.start(project, isolated_copy=False)


def test_unknown_kind_is_refused(project):
    with pytest.raises(SandboxError, match="unknown session kind"):
        Session.start(project, kind="weird")


def test_chat_session_uses_the_assistant_dir_and_refuses_a_project(tmp_path):
    s = Session.start(None, kind="chat", checkpoints=False)
    try:
        assert s.work.name == "assistant" and s.iso is None and s.container.startswith("hal-chat-")
        assert s.capabilities()["work"] is None
    finally:
        s.close()
    with pytest.raises(SandboxError, match="chat session runs in"):
        Session.start(tmp_path, kind="chat", checkpoints=False)


def test_run_files_callable_gets_the_session_id(project):
    seen = []

    def files(sid):
        seen.append(sid)
        return {"agent.json": json.dumps({"session": sid}).encode()}

    s = Session.start(project, run_files=files)
    try:
        assert json.loads((s.rd.run / "agent.json").read_text()) == {"session": s.id} and seen == [s.id]
    finally:
        s.close()


@pytest.mark.parametrize("name", ["/etc/x", "../x", "a/../../x"])
def test_run_file_paths_that_escape_are_refused_and_nothing_is_left(project, name, sockdir):
    with pytest.raises(SandboxError, match="relative path"):
        Session.start(project, run_files={name: b"x"})
    assert not list((sockdir / "s").glob("*")) if (sockdir / "s").exists() else True


def test_run_file_holding_the_session_token_is_refused(project):
    with pytest.raises(SandboxError, match="secret"):
        Session.start(project, run_files={"agent.json": b'{"k":"tok123"}'}, forbidden_secrets=["tok123"])


def test_checkpoint_diff_and_undo_round_trip(session):
    (session.work / "a.py").write_text("print(2)\n")
    assert "print(2)" in session.diff()
    assert session.report().base
    assert session.checkpoint("edit") == 1
    session.undo(0)
    assert (session.work / "a.py").read_text() == "print(1)\n"


def test_apply_isolated_changes_the_real_project(session, project):
    (session.work / "new.txt").write_text("hello\n")
    session.apply_isolated()
    assert (project / "new.txt").read_text() == "hello\n"


def test_close_stops_lifeline_then_removes_container_and_dir(project, env):
    s = Session.start(project)
    order = []
    real_stop = s.lifeline.request_stop
    s.lifeline.request_stop = lambda g: (order.append("stop"), real_stop(g))[1]
    s.broker.stop = lambda: order.append("broker")
    s.close()
    s.close()  # repeating is harmless
    assert order == ["stop", "broker"] and not s.rd.root.exists()
    assert f"rm -f hal-code-{s.id}" in Path(env).read_text()


def test_failed_start_removes_the_runtime_dir_and_the_copy(project, sockdir):
    from halsandbox import isolated

    with pytest.raises(SandboxError):
        Session.start(project, run_files={"/abs": b"x"})
    assert isolated.sessions() == []
