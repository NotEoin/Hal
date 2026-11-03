"""Tests for `halhub.gatewaychat`."""

import json

import pytest

from halhub import gatewaychat
from halhub.reviewer import Reviewer
from halkit import llm
from halkit.testing import FakeModelServer, TempHalHome


@pytest.fixture(autouse=True)
def _fresh_gpu_choice(monkeypatch):
    monkeypatch.setattr(gatewaychat, "_choice", (0.0, {}))


@pytest.fixture
def home():
    with TempHalHome() as h:
        yield h


def test_gateway_chat_sends_the_alias_and_limits_to_the_gateway(home):
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script('{"risk": "low", "reason": "x"}')
        reply = gatewaychat.gateway_chat(
            "hal-fast", [{"role": "user", "content": "q"}], temperature=0, max_tokens=50, timeout_s=5
        )
    body = server.requests[0]["body"]
    assert json.loads(reply)["risk"] == "low"
    assert (body["model"], body["max_tokens"], body["temperature"]) == ("hal-fast", 50, 0)


def test_gateway_chat_raises_when_no_gateway_is_running(home, monkeypatch):
    monkeypatch.setattr(llm, "_gateway_installed", lambda: True)
    with pytest.raises(llm.ModelUnavailable):
        gatewaychat.gateway_chat("hal-fast", [], temperature=0, max_tokens=1, timeout_s=1)


def test_hub_kwargs_always_carries_the_classifier_chat(home):
    kwargs = gatewaychat.hub_kwargs()
    assert kwargs["classify_chat"] is gatewaychat.gateway_chat
    assert "reviewer" not in kwargs


def test_hub_kwargs_builds_the_reviewer_only_when_enabled(home):
    home.policy.write_text("schema_version = 1\n[reviewer]\nenabled = true\n")
    home.policy.chmod(0o600)
    assert isinstance(gatewaychat.hub_kwargs()["reviewer"], Reviewer)


def _fake_gpu_light(monkeypatch, tmp_path, state):
    exe = tmp_path / "gpu-light"
    exe.write_text(f'#!/bin/sh\necho \'{{"state": "{state}"}}\'\n')
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))


def test_gpu_options_are_empty_without_gpu_light(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert gatewaychat.gpu_options() == {}


def test_gpu_options_use_the_gpu_while_the_light_is_green(monkeypatch, tmp_path):
    _fake_gpu_light(monkeypatch, tmp_path, "green")
    assert gatewaychat.gpu_options() == {}


def test_gpu_options_fall_back_to_cpu_while_the_light_is_red(monkeypatch, tmp_path):
    _fake_gpu_light(monkeypatch, tmp_path, "red")
    assert gatewaychat.gpu_options() == gatewaychat.CPU_OPTIONS


def test_gateway_chat_asks_for_cpu_when_the_gpu_is_busy(home, monkeypatch, tmp_path):
    _fake_gpu_light(monkeypatch, tmp_path, "red")
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script("ok")
        gatewaychat.gateway_chat("hal-fast", [], temperature=0, max_tokens=5, timeout_s=5)
    assert server.requests[0]["body"]["options"] == gatewaychat.CPU_OPTIONS


def test_gateway_chat_returns_text_and_logprobs_when_asked(home):
    from halkit.testing import Reply

    entries = [{"token": " yes", "logprob": -0.2, "top_logprobs": []}]
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script(Reply(text="yes", logprobs=entries))
        got = gatewaychat.gateway_chat(
            "m", [{"role": "user", "content": "q"}], temperature=0, max_tokens=16, timeout_s=5, logprobs=True
        )
    body = server.requests[0]["body"]
    assert got == ("yes", entries) and body["logprobs"] is True and body["top_logprobs"] == 5
    assert body["think"] is False  # Guardian must answer at once, not reason through 16 tokens


def test_gateway_chat_forces_cpu_options_with_gpu_false_even_when_the_light_is_green(
    home, monkeypatch, tmp_path
):
    _fake_gpu_light(monkeypatch, tmp_path, "green")
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script("ok")
        gatewaychat.gateway_chat("m", [], temperature=0, max_tokens=5, timeout_s=5, gpu=False)
    assert server.requests[0]["body"]["options"] == gatewaychat.CPU_OPTIONS


def test_hub_kwargs_classifier_chat_runs_on_the_cpu_when_policy_gpu_is_false(home, monkeypatch, tmp_path):
    _fake_gpu_light(monkeypatch, tmp_path, "green")
    home.policy.write_text("schema_version = 1\n[policy]\ngpu = false\n")
    home.policy.chmod(0o600)
    chat = gatewaychat.hub_kwargs()["classify_chat"]
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script("ok")
        chat("m", [], temperature=0, max_tokens=5, timeout_s=5)
    assert server.requests[0]["body"]["options"] == gatewaychat.CPU_OPTIONS


def test_cpu_options_keep_the_model_in_ram_with_a_small_context():
    """Regression: each CPU classification after a 5-minute gap reloaded the model (about 7 s)."""
    assert gatewaychat.CPU_OPTIONS["num_gpu"] == 0
    assert gatewaychat.CPU_OPTIONS["keep_alive"] == gatewaychat.CPU_KEEP_ALIVE
    assert gatewaychat.CPU_OPTIONS["num_ctx"] == gatewaychat.CPU_NUM_CTX
