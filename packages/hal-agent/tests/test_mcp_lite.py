"""Tests for `halagent.mcp.lite` against the scripted fake broker."""

import pytest

from halagent.mcp.lite import LiteMcpClient, McpError
from halkit.testing.broker import FakeBroker


@pytest.fixture
def broker(tmp_path):
    with FakeBroker(tmp_path / "b.sock") as b:
        b.add_tool("web_read", "page text", description="Read a page")
        b.add_tool("boom", RuntimeError("denied"))
        yield b


def test_tools_are_listed_after_handshake(broker):
    c = LiteMcpClient(str(broker.socket_path))
    c.initialize()
    assert [t["name"] for t in c.list_tools()] == ["web_read", "boom"]
    c.close()


def test_call_returns_text_and_error_flag(broker):
    c = LiteMcpClient(str(broker.socket_path))
    c.initialize()
    assert c.call_tool("web_read", {"url": "x"}) == ("page text", False)
    assert c.call_tool("boom", {}) == ("denied", True)
    assert broker.calls[0] == ("web_read", {"url": "x"})


def test_unknown_tool_raises_mcp_error(broker):
    c = LiteMcpClient(str(broker.socket_path))
    c.initialize()
    with pytest.raises(McpError):
        c.call_tool("nope", {})


def test_missing_socket_fails_loudly(tmp_path):
    with pytest.raises(McpError, match="cannot connect"):
        LiteMcpClient(str(tmp_path / "none.sock")).initialize()
