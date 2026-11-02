"""Tests for `halkit.llm`."""

import pytest

from halkit import llm
from halkit.testing import FakeModelServer, Reply, TempHalHome
from halkit.testing.model_server import fake_embedding

MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture
def home():
    with TempHalHome() as h:
        yield h


def test_chat_uses_the_gateway_unix_socket_when_it_exists(home):
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script("hello")
        assert llm.chat(MESSAGES, model="hal-fast", timeout=5, max_tokens=7, temperature=0) == "hello"
    body = server.requests[0]["body"]
    assert body["model"] == "hal-fast" and body["max_tokens"] == 7 and body["temperature"] == 0


def test_client_mode_sends_the_bearer_token_from_the_token_file(home):
    with FakeModelServer() as server:
        token = home.root / "tok"
        token.write_text("s3cret\n")
        token.chmod(0o600)
        home.config.write_text(f'[gateway]\nurl = "{server.url}"\ntoken_file = "{token}"\n')
        server.script("ok")
        assert llm.chat(MESSAGES, timeout=5) == "ok"
    assert server.requests[0]["headers"]["Authorization"] == "Bearer s3cret"


def test_a_stopped_gateway_on_a_hal_install_never_falls_back_to_ollama(home, monkeypatch):
    monkeypatch.setattr(llm, "_gateway_installed", lambda: True)
    with pytest.raises(llm.ModelUnavailable, match="hal-gateway.service"):
        llm.chat(MESSAGES, timeout=1)


def test_standalone_pack_falls_back_to_the_configured_ollama_url(home, monkeypatch):
    monkeypatch.setattr(llm, "_gateway_installed", lambda: False)
    with FakeModelServer() as server:
        home.config.write_text(f'[hal]\nollama_url = "{server.url}"\n')
        server.script("direct")
        assert llm.chat(MESSAGES, timeout=5) == "direct"
    assert server.requests[0]["path"] == "/v1/chat/completions"


@pytest.mark.parametrize("status", [502, 503])
def test_backend_down_statuses_raise_model_unavailable(home, status):
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script(Reply(status=status))
        with pytest.raises(llm.ModelUnavailable) as err:
            llm.chat(MESSAGES, timeout=5)
    assert err.value.status == status


def test_other_error_statuses_raise_gateway_error_with_status_and_body(home):
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script(Reply(status=400))
        with pytest.raises(llm.GatewayError) as err:
            llm.chat(MESSAGES, timeout=5)
    assert err.value.status == 400 and not isinstance(err.value, llm.ModelUnavailable)
    assert "scripted error" in err.value.body


def test_a_slow_gateway_hits_the_timeout_as_model_unavailable(home):
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script(Reply(text="late", delay=1.0))
        with pytest.raises(llm.ModelUnavailable):
            llm.chat(MESSAGES, timeout=0.2)


def test_a_dead_socket_file_raises_model_unavailable(home):
    sock = home.runtime / "gateway.sock"
    import socket

    s = socket.socket(socket.AF_UNIX)
    s.bind(str(sock))
    s.close()  # a socket file with nobody listening
    with pytest.raises(llm.ModelUnavailable):
        llm.chat(MESSAGES, timeout=1)


def test_stream_chat_yields_the_text_chunks_in_order(home):
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script("one two three")
        assert "".join(llm.stream_chat(MESSAGES, timeout=5)).strip() == "one two three"


def test_embed_returns_one_vector_per_text_in_order(home):
    with FakeModelServer(home.runtime / "gateway.sock"):
        vectors = llm.embed(["a", "b"], timeout=5)
    assert vectors == [fake_embedding("a"), fake_embedding("b")]


def test_ping_lists_the_gateway_models(home):
    with FakeModelServer(home.runtime / "gateway.sock"):
        assert llm.ping(timeout=5) == ["fake-model"]


def test_models_returns_the_rows_with_their_ids(home):
    with FakeModelServer(home.runtime / "gateway.sock"):
        assert [row["id"] for row in llm.models(timeout=5)] == ["fake-model"]


def test_chat_sends_ollama_options_only_when_given(home):
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script("a")
        server.script("b")
        llm.chat(MESSAGES, timeout=5, options={"num_gpu": 0})
        llm.chat(MESSAGES, timeout=5)
    assert server.requests[0]["body"]["options"] == {"num_gpu": 0}
    assert "options" not in server.requests[1]["body"]


def test_chat_logprobs_asks_for_logprobs_and_returns_the_token_entries(home):
    entries = [{"token": "yes", "logprob": -0.1, "top_logprobs": [{"token": "yes", "logprob": -0.1}]}]
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script(Reply(text="yes", logprobs=entries))
        text, got = llm.chat_logprobs(MESSAGES, timeout=5, top_logprobs=5)
    body = server.requests[0]["body"]
    assert (text, got) == ("yes", entries)
    assert (body["logprobs"], body["top_logprobs"]) == (True, 5)


def test_chat_logprobs_sends_think_only_when_asked(home):
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script("no", "no")
        llm.chat_logprobs(MESSAGES, timeout=5, think=False)
        llm.chat_logprobs(MESSAGES, timeout=5)
    assert server.requests[0]["body"]["think"] is False
    assert "think" not in server.requests[1]["body"]


def test_chat_logprobs_returns_an_empty_list_when_the_backend_sent_none(home):
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script("no")
        assert llm.chat_logprobs(MESSAGES, timeout=5) == ("no", [])


def test_plain_chat_never_asks_for_logprobs(home):
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.script("a")
        llm.chat(MESSAGES, timeout=5)
    assert "logprobs" not in server.requests[0]["body"]
