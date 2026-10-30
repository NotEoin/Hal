"""Tests for `halgateway.llamacmd`: argv and environment rules of REQ-SEC-76."""

import pytest

from halgateway import llamacmd
from halgateway.llamacmd import CommandRefused, Preset, llama_server_command

HOSTILE_ENV = {
    "PATH": "/usr/bin", "HOME": "/home/u", "LANG": "C", "LLAMA_ARG_RPC": "10.0.0.5:50052",
    "LLAMA_API_KEY": "synthetic-key", "HF_TOKEN": "synthetic-token", "LD_LIBRARY_PATH": "/evil",
    "LLAMA_ARG_HF_REPO": "x/y", "OPENAI_API_KEY": "synthetic",
}  # fmt: skip


@pytest.fixture
def preset(tmp_path):
    return Preset(tmp_path / "mode.ini", "0" * 64)


def build(preset, tmp_path, listen="127.0.0.1:8081", key=True, **kw):
    key_file = tmp_path / "key" if key else None
    return llama_server_command(
        preset, listen=listen, api_key_file=key_file, state=tmp_path, environ=HOSTILE_ENV, **kw
    )


def test_tcp_command_listens_on_loopback_with_a_key_file_and_no_webui(preset, tmp_path):
    argv, _ = build(preset, tmp_path)
    assert argv[argv.index("--host") + 1] == "127.0.0.1" and argv[argv.index("--port") + 1] == "8081"
    assert argv[argv.index("--api-key-file") + 1] == str(tmp_path / "key")
    assert "--no-webui" in argv and "--models-preset" in argv and "--models-max" in argv


def test_the_key_is_never_on_the_command_line(preset, tmp_path):
    argv, _ = build(preset, tmp_path)
    assert "--api-key" not in argv and "synthetic-key" not in " ".join(argv)


@pytest.mark.parametrize("listen", ["0.0.0.0:8081", "localhost:8081", "[::]:8081", "127.0.0.1", "8081"])
def test_a_non_loopback_or_malformed_listen_address_is_refused(preset, tmp_path, listen):
    with pytest.raises(CommandRefused):
        build(preset, tmp_path, listen=listen)


def test_tcp_without_a_key_file_is_refused(preset, tmp_path):
    with pytest.raises(CommandRefused, match="api-key-file"):
        build(preset, tmp_path, key=False)


def test_a_unix_socket_below_the_run_directory_needs_no_key(preset, tmp_path):
    (tmp_path / "gateway" / "run").mkdir(parents=True)
    sock = str(tmp_path / "gateway" / "run" / "llama.sock")
    argv, _ = build(preset, tmp_path, listen=sock, key=False)
    assert argv[argv.index("--host") + 1] == sock and "--port" not in argv


def test_a_unix_socket_outside_the_run_directory_is_refused(preset, tmp_path):
    with pytest.raises(CommandRefused, match="outside"):
        build(preset, tmp_path, listen="/tmp/elsewhere.sock", key=False)


def test_no_denied_flag_is_ever_in_the_argv(preset, tmp_path):
    argv, _ = build(preset, tmp_path)
    flags = {a.removeprefix("--") for a in argv if a.startswith("--")}
    assert not flags & llamacmd.DENIED_FLAGS


def test_environment_is_the_allowlist_only(preset, tmp_path):
    _, env = build(preset, tmp_path)
    assert set(env) <= {*llamacmd.ENV_ALLOWLIST, "GPU_LIGHT_SERVICE", "LD_LIBRARY_PATH"}
    assert env["PATH"] == "/usr/bin" and env["GPU_LIGHT_SERVICE"] == llamacmd.RECLAIMER_NAME
    for name in ("LLAMA_ARG_RPC", "LLAMA_API_KEY", "HF_TOKEN", "LLAMA_ARG_HF_REPO", "OPENAI_API_KEY"):
        assert name not in env


def test_the_library_path_is_only_the_llama_directory(preset, tmp_path):
    _, env = build(preset, tmp_path, llama_dir=tmp_path / "llama.cpp")
    assert env["LD_LIBRARY_PATH"] == str(tmp_path / "llama.cpp")
    _, plain = build(preset, tmp_path)
    assert "LD_LIBRARY_PATH" not in plain  # the caller's value (/evil) is dropped


def test_slot_save_path_must_stay_below_the_state_directory(tmp_path):
    (tmp_path / "gateway" / "slots").mkdir(parents=True)
    llamacmd.check_slot_path(tmp_path / "gateway" / "slots" / "a", tmp_path)
    with pytest.raises(CommandRefused):
        llamacmd.check_slot_path(tmp_path / "gateway" / "slots" / ".." / ".." / "x", tmp_path)
