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
        ChatProvider(server.url, "m").complete([{"role": "user", "content": "x"}])


def test_bad_endpoint_fails_loudly():
    with pytest.raises(ValueError):
        ChatProvider("ftp://x", "m")


def test_settings_env_overrides_defaults(tmp_path):
    s = load_settings(
        path=tmp_path / "none.toml", environ={"HAL_AGENT_ENDPOINT": "http://h:1", "HAL_AGENT_STREAM": "true"}
    )
    assert s.endpoint == "http://h:1" and s.stream is True
