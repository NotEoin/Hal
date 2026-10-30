"""Tests for the box environment allowlist."""

import pytest

from halsandbox.env import box_env

HOST = {"TERM": "xterm", "LANG": "de_DE.UTF-8", "OPENAI_API_KEY": "sk-x", "HOME": "/home/me", "MYVAR": "v"}


def test_host_secrets_and_home_are_dropped():
    env = box_env("code", "s1", "docker", HOST)
    assert "OPENAI_API_KEY" not in env and env["HOME"] == "/home/hal" and "MYVAR" not in env


def test_passthrough_names_override_the_default_lang():
    env = box_env("code", "s1", "docker", HOST)
    assert env["TERM"] == "xterm" and env["LANG"] == "de_DE.UTF-8"
    assert box_env("code", "s1", "docker", {})["LANG"] == "C.UTF-8"


def test_configured_passthrough_name_is_copied():
    assert box_env("code", "s1", "docker", HOST, ["MYVAR"])["MYVAR"] == "v"


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "lower", "GH_TOKEN"])
def test_secret_like_or_malformed_passthrough_is_refused(name):
    with pytest.raises(ValueError, match="cannot pass"):
        box_env("code", "s1", "docker", HOST, [name])


def test_only_box_kind_sets_harness_and_only_relay_kinds_set_model_url():
    assert box_env("box", "s", "docker", {})["HAL_HARNESS"] == "1"
    assert "HAL_HARNESS" not in box_env("shell", "s", "docker", {})
    assert box_env("shell", "s", "docker", {}, relay_port=9)["HAL_MODEL_URL"] == "http://127.0.0.1:9"
    assert "HAL_MODEL_URL" not in box_env("code", "s", "docker", {})


def test_box_is_offline_and_telemetry_free():
    env = box_env("box", "s", "bwrap", {})
    assert env["UV_OFFLINE"] == "1" and env["PIP_NO_INDEX"] == "1" and env["DO_NOT_TRACK"] == "1"
    assert env["HAL_SANDBOX"] == "bwrap" and env["HAL_SESSION"] == "s"


def test_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="kind"):
        box_env("weird", "s", "docker", {})
