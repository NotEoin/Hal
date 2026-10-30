"""Tests for the forced command (REQ-RMT-20 to 24)."""

from __future__ import annotations

import json
import os

import pytest

from halremote import serve

CONN = "192.168.1.20 50000 192.168.1.5 22"


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A private state dir, a runtime dir and an executable fake hub."""
    state = tmp_path / "state"
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    hub = tmp_path / "hal-hub"
    hub.write_text("#!/bin/sh\n")
    hub.chmod(0o755)
    env = {
        "HAL_STATE_DIR": str(state),
        "HAL_RUNTIME_DIR": str(runtime),
        "HAL_CONFIG": str(tmp_path / "none.toml"),
        "HOME": str(tmp_path),
        "SSH_CONNECTION": CONN,
    }
    for key in list(os.environ):
        if key.startswith("HAL_"):
            monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return {"hub": hub, "env": env, "state": state}


def call(box, verb=None, *, execve=None, now=None, extra=None, client="laptop"):
    env = dict(box["env"], **(extra or {}))
    if verb is not None:
        env["SSH_ORIGINAL_COMMAND"] = verb
    kw = {"execve": execve} if execve else {}
    return serve.main(["--client", client, "--hub", str(box["hub"])], environ=env, now=now, **kw)


def out_json(capsys):
    out = capsys.readouterr().out
    assert out.endswith("\n") and out.count("\n") == 1
    return json.loads(out)


def test_unknown_verb_gets_one_json_error_line_and_exit_64(box, capsys):
    assert call(box, "rm -rf x") == 64
    assert out_json(capsys)["error"] == "unknown_verb"


@pytest.mark.parametrize("verb", ["hello extra", "mcp-stdio; id", "'unterminated"])
def test_extra_words_or_bad_quoting_are_unknown_verbs(box, capsys, verb):
    assert call(box, verb) == 64
    assert out_json(capsys)["error"] == "unknown_verb"


def test_bad_client_name_is_a_usage_error(box, capsys):
    assert call(box, "check", client="Bad Name") == 64
    assert out_json(capsys)["error"] == "bad_invocation"


def test_check_reports_ok_and_exit_0(box, capsys):
    assert call(box, "check") == 0
    msg = out_json(capsys)
    assert msg["schema"] == "hal.remote.check/1"
    assert msg["ok"] is True
    assert msg["hub"]["executable"] is True


def test_check_with_a_missing_hub_fails_with_a_problem(box, capsys):
    box["hub"].unlink()
    assert call(box, "check") == 1
    msg = out_json(capsys)
    assert msg["ok"] is False
    assert msg["problems"]


def test_check_reports_a_runtime_dir_error_instead_of_guessing(box, capsys, monkeypatch):
    loose = box["env"]["HAL_RUNTIME_DIR"]
    os.chmod(loose, 0o777)  # a runtime dir open to others is refused by `halkit.paths`
    assert call(box, "check") == 1
    assert any("runtime_dir" in p for p in out_json(capsys)["problems"])


def test_mcp_stdio_execs_the_hub_with_origin_and_a_minimal_env(box):
    seen = {}

    def fake_exec(path, argv, env):
        seen.update(path=path, argv=argv, env=dict(env))

    extra = {"SSH_ORIGINAL_COMMAND": "", "LD_PRELOAD": "/evil.so", "PATH": "/home/x/bin", "SECRET": "s"}
    assert call(box, None, execve=fake_exec, extra=extra) == 1
    assert seen["path"] == str(box["hub"])
    assert seen["argv"][1:] == [
        "mcp",
        "--transport",
        "stdio",
        "--channel",
        "remote",
        "--origin",
        "remote:laptop",
    ]
    env = seen["env"]
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HAL_REMOTE_CLIENT"] == "laptop"
    assert env["HAL_REMOTE_ADDR"] == "192.168.1.20"
    assert "LD_PRELOAD" not in env
    assert "SECRET" not in env
    assert "SSH_ORIGINAL_COMMAND" not in env


def test_mcp_stdio_with_an_empty_environment_reports_the_runtime_dir_error(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "s"))
    (tmp_path / "rt").mkdir(mode=0o777)
    (tmp_path / "rt").chmod(0o777)
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(tmp_path / "rt"))
    hub = tmp_path / "hub"
    hub.write_text("")
    hub.chmod(0o755)
    env = {}
    code = serve.main(["--client", "laptop", "--hub", str(hub)], environ=env, execve=lambda *a: None)
    assert code == 1
    assert out_json(capsys)["error"] == "runtime_dir"


def test_mcp_stdio_with_a_missing_hub_exits_127(box, capsys):
    box["hub"].unlink()
    assert call(box, "mcp-stdio", execve=lambda *a: None) == 127
    assert out_json(capsys)["error"] == "hub_missing"


def test_session_cap_answers_busy_with_exit_75(box, capsys):
    held = [serve._acquire_slot("laptop", 4) for _ in range(4)]
    try:
        assert all(fd is not None for fd in held)
        assert call(box, "mcp-stdio", execve=lambda *a: None) == 75
        assert out_json(capsys)["error"] == "busy"
        assert serve._acquire_slot("other", 4) is not None
    finally:
        for fd in held:
            os.close(fd)


def _write_delivery(box, token="tok-123", client="laptop"):
    d = serve._private_dir("remote", "delivery")
    path = d / f"{client}.json"
    path.write_text(json.dumps({"schema": serve.SCHEMA_DELIVERY, "tokens": {"gateway": token}}))
    path.chmod(0o600)
    return path


def test_hello_delivers_the_token_once(box, capsys):
    _write_delivery(box)
    assert call(box, "hello") == 0
    first = out_json(capsys)
    assert first["tokens"] == {"gateway": "tok-123"}
    assert first["forwards"][0]["port"] == 8741
    assert call(box, "hello") == 0
    assert out_json(capsys)["tokens"] is None


def test_an_expired_delivery_is_deleted_and_not_delivered(box, capsys):
    path = _write_delivery(box)
    later = path.stat().st_mtime + 86400 + 1
    assert call(box, "hello", now=later) == 0
    assert out_json(capsys)["tokens"] is None
    assert not path.exists()


def test_a_corrupt_delivery_is_an_error_and_is_removed(box, capsys):
    path = _write_delivery(box)
    path.write_text("not json")
    assert call(box, "hello") == 1
    assert out_json(capsys)["error"] == "delivery_failed"
    assert not path.exists()


def test_hello_without_a_delivery_has_null_tokens(box, capsys):
    assert call(box, "hello") == 0
    assert out_json(capsys)["tokens"] is None


def test_stdout_is_ascii_only_for_every_verb(box, capsys):
    for verb in ("hello", "check", "bogus"):
        call(box, verb)
    capsys.readouterr().out.encode("ascii")


def test_log_goes_to_the_serve_log_not_stdout(box, capsys):
    call(box, "check")
    capsys.readouterr()
    assert "verb check" in (box["state"] / "remote" / "serve.log").read_text()
