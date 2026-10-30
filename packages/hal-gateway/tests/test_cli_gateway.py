"""Tests for the `hal-gateway` commands that talk to a running gateway, and for `serve`."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from halgateway import cli
from halgateway.config import load_config
from halgateway.server import Gateway

TOKEN = "hal-gw-cli-test"
SERVE_START_TIMEOUT_S = 20
SERVE_STOP_TIMEOUT_S = 20


@pytest.fixture
def cfg(tmp_path):
    token = tmp_path / "token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    path = tmp_path / "config.toml"
    path.write_text(
        f'[gateway]\nunix_path = "{tmp_path}/run/gateway.sock"\ntoken_file = "{token}"\n'
        '[gateway.gpu]\ncoordinator = "none"\n'
        '[gateway.backends.far]\ntype = "openai"\nurl = "https://far.example.invalid"\nallow_remote = true\n'
        '[gateway.aliases.away]\nbackend = "far"\nmodel = "m"\n'
    )
    return path


@pytest.fixture
def running(cfg):
    gw = Gateway(load_config(cfg))
    gw.start()
    yield gw
    gw.stop()


def test_status_command_marks_the_remote_backend_and_prints_warnings(running, cfg, capsys):
    assert cli.main(["status", "--config", str(cfg)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "REMOTE far.example.invalid" in out and "warning:" in out


def test_status_json_is_one_document(running, cfg, capsys):
    assert cli.main(["status", "--json", "--config", str(cfg)]) == cli.EXIT_OK
    assert "far" in json.loads(capsys.readouterr().out)["backends"]


def test_models_command_lists_the_remote_entry_with_its_owner(running, cfg, capsys):
    assert cli.main(["models", "--config", str(cfg)]) == cli.EXIT_OK
    assert "remote:far.example.invalid" in capsys.readouterr().out


def test_mode_without_a_name_prints_the_active_mode(running, cfg, capsys):
    assert cli.main(["mode", "--config", str(cfg)]) == cli.EXIT_OK
    assert isinstance(json.loads(capsys.readouterr().out), dict)


def test_router_commands_without_a_router_fail_with_the_gateway_exit_code(running, cfg, capsys):
    for argv in (["mode", "gpu"], ["load", "m"], ["unload", "m"], ["router", "stop"]):
        assert cli.main([*argv, "--config", str(cfg)]) == cli.EXIT_GATEWAY
    assert "404" in capsys.readouterr().err


def test_status_with_the_gateway_down_is_the_gateway_exit_code(cfg, capsys):
    assert cli.main(["status", "--config", str(cfg)]) == cli.EXIT_GATEWAY
    assert "cannot reach the gateway" in capsys.readouterr().err


def test_serve_runs_until_sigterm_and_removes_its_socket(cfg, tmp_path):
    env = {**os.environ, "HAL_RUNTIME_DIR": str(tmp_path / "run")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "halgateway", "serve", "--config", str(cfg)],
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )
    sock = tmp_path / "run" / "gateway.sock"
    try:
        deadline = time.monotonic() + SERVE_START_TIMEOUT_S
        while not sock.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert sock.exists(), "serve did not create its socket"
        assert cli.main(["status", "--json", "--config", str(cfg)]) == cli.EXIT_OK
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(SERVE_STOP_TIMEOUT_S) == cli.EXIT_OK
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert not sock.exists()


def test_serve_with_a_bad_config_exits_with_the_config_code(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("[gateway]\nno_such_key = 1\n")
    assert cli.main(["serve", "--config", str(bad)]) == cli.EXIT_CONFIG
