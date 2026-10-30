"""Tests for `halhub.gatewaychat`."""

import json

import pytest

from halhub import gatewaychat
from halhub.reviewer import Reviewer
from halkit import llm
from halkit.testing import FakeModelServer, TempHalHome


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
