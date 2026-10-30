"""The forced command through a real sshd, and SC14 with a real login shell (REQ-RMT-17, 20, 23)."""

from __future__ import annotations

import json
import os
import shutil

import pytest

from halkit import proc
from halremote import authkeys, keys, servercheck

pytestmark = pytest.mark.slow
PEER = "127.0.0.1"


def _mcp_line(server, hal_remote_exe, hub):
    key = server.new_key("mcp")
    pub = keys.parse_public_key(key.with_suffix(".pub").read_text())
    line = authkeys.render_line(
        "mcp", client="laptop", key=pub, from_pattern="127.0.0.0/8", forwards=[],
        serve_path=hal_remote_exe, hub_path=hub,
    )  # fmt: skip
    server.authorize(line)
    return key, line


def test_check_verb_answers_ok_through_sshd(start_sshd, hal_remote_exe, fake_hub):
    server = start_sshd()
    key, _ = _mcp_line(server, hal_remote_exe, fake_hub)
    res = server.run(key, command="check")
    assert res.returncode == 0, res.stderr
    msg = json.loads(res.stdout.splitlines()[0])
    assert msg["ok"] is True
    assert msg["client"] == "laptop"


def test_empty_command_execs_the_hub_with_the_remote_origin(start_sshd, hal_remote_exe, fake_hub):
    server = start_sshd()
    key, _ = _mcp_line(server, hal_remote_exe, fake_hub)
    res = server.run(key)
    assert "hub mcp --transport stdio --channel remote --origin remote:laptop" in res.stdout
    assert f"client laptop addr {PEER}" in res.stdout


def _noisy_home(tmp_path, quiet_first):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    guard = "case $- in *i*) ;; *) return;; esac\n"
    (home / ".bashrc").write_text((guard + "echo noisy\n") if quiet_first else ("echo noisy\n" + guard))
    return home


def _sc14(tmp_path, hal_remote_exe, fake_hub, *, quiet_first):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash")
    home = _noisy_home(tmp_path, quiet_first)
    runtime = tmp_path / "rt"
    runtime.mkdir(mode=0o700)
    command = f"{hal_remote_exe} serve --client laptop --hub {fake_hub}"
    extra = {
        "HAL_STATE_DIR": str(tmp_path / "state"),
        "HAL_RUNTIME_DIR": str(runtime),
        "HAL_CONFIG": str(tmp_path / "none"),
    }
    user = os.environ.get("USER", "u")
    return servercheck._forced_command_problem(proc.run, bash, command, "laptop", home, user, extra)


def test_rc_file_that_prints_before_its_interactive_check_fails_sc14(
    tmp_path, hal_remote_exe, fake_hub, monkeypatch
):
    problem = _sc14(tmp_path, hal_remote_exe, fake_hub, quiet_first=False)
    assert problem is not None
    assert "not JSON" in problem


def test_rc_file_that_returns_early_for_non_interactive_shells_passes_sc14(
    tmp_path, hal_remote_exe, fake_hub, monkeypatch
):
    assert _sc14(tmp_path, hal_remote_exe, fake_hub, quiet_first=True) is None
