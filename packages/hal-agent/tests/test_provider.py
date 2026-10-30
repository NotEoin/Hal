"""Tests for `halagent.provider` against the fake model server."""

import pytest

from halagent.provider import ChatProvider, ProviderError
from halagent.settings import load_settings
from halkit.testing.model_server import FakeModelServer, Reply


def test_plain_reply_is_returned(server):
    server.script("hello")
    out = ChatProvider(server.url, "m").complete([{"role": "user", "content": "hi"}])
    assert out.text == "hello" and out.tool_calls == []


def test_native_tool_call_is_parsed(server):
    server.script(Reply(tool_calls=[("read", {"path": "a"})]))
    out = ChatProvider(server.url, "m").complete([{"role": "user", "content": "x"}], tools=[{}])
    assert out.tool_calls[0][1] == "read" and '"a"' in out.tool_calls[0][2]


def test_streamed_reply_is_assembled(server):
    server.script("streamed text")
    out = ChatProvider(server.url, "m", stream=True).complete([{"role": "user", "content": "x"}])
    assert out.text.strip() == "streamed text"


def test_unix_socket_endpoint_works(tmp_path):
    with FakeModelServer(tmp_path / "m.sock") as srv:
        srv.script("over unix")
        out = ChatProvider(str(tmp_path / "m.sock"), "m").complete([{"role": "user", "content": "x"}])
    assert out.text == "over unix"


def test_http_error_raises_provider_error(server):
    server.script(Reply(status=500))
    with pytest.raises(ProviderError, match="500"):
        ChatProvider(server.url, "m", max_retries=0).complete([{"role": "user", "content": "x"}])


def test_bad_endpoint_fails_loudly():
    with pytest.raises(ValueError):
        ChatProvider("ftp://x", "m")


def test_settings_env_overrides_defaults(tmp_path):
    s = load_settings(
        path=tmp_path / "none.toml", environ={"HAL_AGENT_ENDPOINT": "http://h:1", "HAL_AGENT_STREAM": "true"}
    )
    assert s.endpoint == "http://h:1" and s.stream is True


def test_finish_reason_and_usage_are_returned(server):
    server.script(Reply(text="cut", finish_reason="length", prompt_tokens=7, completion_tokens=3))
    out = ChatProvider(server.url, "m").complete([{"role": "user", "content": "x"}])
    assert (out.finish, out.prompt_tokens, out.completion_tokens) == ("length", 7, 3)


def test_streamed_reply_carries_finish_reason(server):
    server.script(Reply(text="abc", finish_reason="length"))
    out = ChatProvider(server.url, "m", stream=True).complete([{"role": "user", "content": "x"}])
    assert out.finish == "length"


def test_transient_error_is_retried_with_backoff(server):
    server.script(Reply(status=503), Reply(status=429), "fine")
    waits: list[float] = []
    out = ChatProvider(server.url, "m", sleep=waits.append).complete([{"role": "user", "content": "x"}])
    assert out.text == "fine" and waits == [1.0, 2.0]


def test_client_error_is_not_retried(server):
    server.script(Reply(status=400), "never")
    waits: list[float] = []
    with pytest.raises(ProviderError, match="400"):
        ChatProvider(server.url, "m", sleep=waits.append).complete([{"role": "user", "content": "x"}])
    assert waits == []


def test_retries_stop_after_the_limit(server):
    server.script(*[Reply(status=500)] * 5)
    waits: list[float] = []
    with pytest.raises(ProviderError, match="500"):
        ChatProvider(server.url, "m", max_retries=2, sleep=waits.append).complete(
            [{"role": "user", "content": "x"}]
        )
    assert len(waits) == 2


def test_retry_after_overrides_backoff_and_is_capped():
    from halagent.provider import BACKOFF_CAP_S, backoff_delay

    assert backoff_delay(0, 5.0) == 5.0 and backoff_delay(0, 9999.0) == BACKOFF_CAP_S
    assert backoff_delay(10, None) == BACKOFF_CAP_S


def test_profile_extra_keys_reach_the_request(server):
    server.script("ok")
    ChatProvider(server.url, "m", extra={"temperature": 0.3}).complete([{"role": "user", "content": "x"}])
    assert server.requests[-1]["body"]["temperature"] == 0.3
