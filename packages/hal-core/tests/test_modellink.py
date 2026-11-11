"""Tests for `halcore.modellink`: which way a box reaches the model, and the gateway session it gets."""

from __future__ import annotations

import logging
import socket

import pytest

from halcore import modellink
from halgateway import client as gwclient
from halkit.testing import TempHalHome
from halsandbox.errors import SandboxError


@pytest.fixture
def home():
    with TempHalHome() as h:
        h.config.write_text('[hal]\nchat_model = "chat-tag"\ncode_model = "code-tag"\nollama_url = "http://127.0.0.1:11434"\n')
        yield h


@pytest.fixture
def gateway(home):
    """A listening unix socket where the gateway's would be."""
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(home.runtime / modellink.SOCKET_NAME))
    sock.listen()
    yield home.runtime / modellink.SOCKET_NAME
    sock.close()


def test_with_the_gateway_a_code_box_asks_for_hal_code_and_a_chat_box_for_hal_chat(gateway):
    # regression: hal code asked Ollama for chat_model, bypassing the code role and its context
    code = modellink.route("code")
    assert (code.model, code.via_gateway, code.gateway_socket) == ("hal-code", True, gateway)
    assert modellink.route("chat").model == "hal-chat"
    assert modellink.route("job").model == "hal-code"


def test_without_the_gateway_the_box_uses_the_hal_model_at_ollama_and_says_so(home, caplog):
    with caplog.at_level(logging.WARNING):
        code = modellink.route("code")
    assert (code.model, code.upstream, code.via_gateway) == ("code-tag", "http://127.0.0.1:11434", False)
    assert modellink.route("chat").model == "chat-tag"
    assert "gateway is not running" in caplog.text


def test_an_empty_code_model_falls_back_to_chat_model(home):
    home.config.write_text('[hal]\nchat_model = "chat-tag"\n')
    assert modellink.route("code").model == "chat-tag"


def test_an_explicit_upstream_or_model_wins_over_the_gateway(gateway, monkeypatch):
    found = modellink.route("code", upstream="http://127.0.0.1:9000")
    assert (found.upstream, found.via_gateway, found.model) == ("http://127.0.0.1:9000", False, "code-tag")
    assert modellink.route("code", model="hal-code-128k").model == "hal-code-128k"
    monkeypatch.setenv(modellink.MODEL_ENV, "from-env")
    assert modellink.route("code").model == "from-env"


class FakeOwner:
    """Stands in for `GatewayClient`: records the sessions it mints and revokes."""

    minted: list[dict] = []
    revoked: list[str] = []

    def __init__(self, socket_path, token):
        self.socket_path = socket_path

    def create_session(self, label, **kw):
        FakeOwner.minted.append({"label": label, **kw})
        n = len(FakeOwner.minted)
        return gwclient.SessionGrant(f"s{n}", f"hal-sess-secret{n}", "2026-11-12T00:00:00Z")

    def revoke_session(self, session_id):
        FakeOwner.revoked.append(session_id)


def test_a_gateway_link_mints_a_session_that_cannot_reach_a_remote_backend_and_revokes_it(gateway, home, monkeypatch):
    FakeOwner.minted, FakeOwner.revoked = [], []
    monkeypatch.setattr(gwclient, "GatewayClient", FakeOwner)
    link = modellink.open(modellink.route("code"), home.runtime / "model.sock", "code box abc")
    try:
        assert (home.runtime / "model.sock").is_socket()
        assert FakeOwner.minted == [{"label": "code box abc", "allow_remote": False}]
    finally:
        link.close()
    assert FakeOwner.revoked == ["s1"] and not (home.runtime / "model.sock").exists()


def test_a_rejected_token_is_replaced_with_a_fresh_session(gateway, home, monkeypatch):
    FakeOwner.minted, FakeOwner.revoked = [], []
    monkeypatch.setattr(gwclient, "GatewayClient", FakeOwner)
    forwarders = []
    real = modellink.SessionForwarder

    def spy(*a, **kw):
        forwarders.append(real(*a, **kw))
        return forwarders[-1]

    monkeypatch.setattr(modellink, "SessionForwarder", spy)
    link = modellink.open(modellink.route("code"), home.runtime / "model.sock", "code box abc")
    try:
        forwarders[0].on_event(modellink.ForwarderEvent("token_rejected", {}))
        assert len(FakeOwner.minted) == 2 and forwarders[0]._token == "hal-sess-secret2"  # noqa: SLF001 - the token it relays with
    finally:
        link.close()
    assert FakeOwner.revoked == ["s2"]


def test_a_gateway_that_refuses_a_session_stops_the_box_with_the_fix(gateway, home, monkeypatch):
    class Refusing(FakeOwner):
        def create_session(self, label, **kw):
            raise gwclient.ClientError("gateway answered 503: down", 503)

    monkeypatch.setattr(gwclient, "GatewayClient", Refusing)
    with pytest.raises(SandboxError, match="hal-gateway serve"):
        modellink.open(modellink.route("code"), home.runtime / "model.sock", "code box abc")
