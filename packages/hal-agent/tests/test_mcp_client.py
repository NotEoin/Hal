"""Tests for `halagent.mcp.client`: user MCP servers on the SDK (REQ-AGT-26, 75, 76, 78)."""

import sys
from pathlib import Path

import pytest

from halagent import boot
from halagent.loop import Agent
from halagent.mcp.client import McpConfigError, UserMcp, acp_servers, envelope, merge_servers, parse_server
from halagent.settings import AgentSettings
from halagent.tools import Toolset
from halkit.testing.model_server import Reply

SERVER = str(Path(__file__).parent / "fake_mcp_server.py")
ENTRY = {"name": "fake", "command": sys.executable, "args": [SERVER]}


@pytest.fixture
def notices() -> list[str]:
    return []


@pytest.fixture
def mcp(tmp_path, notices):
    servers = merge_servers([ENTRY], {}, {}, notices.append)
    live = UserMcp(servers, sandbox="runsc", root=tmp_path, notice=notices.append)
    yield live
    live.close()


def test_tools_are_named_mcp_server_tool(mcp):
    names = {t.name for t in mcp.tools()}
    assert {"mcp__fake__echo", "mcp__fake__crash"} <= names


def test_mcp_result_uses_security_envelope(mcp):
    text, failed = mcp.call("mcp__fake__echo", {"text": "hello"})
    assert not failed
    assert text.startswith('<tool_result tool="mcp__fake__echo" trust="untrusted" guard="off">')
    assert "hello" in text and text.endswith("</tool_result>")


def test_result_cannot_close_the_envelope_early(mcp):
    text, _ = mcp.call("mcp__fake__hostile", {})
    assert text.count("</tool_result>") == 1 and text.lower().count("<tool_result") == 1
    assert "&lt;/tool_result" in text


def test_non_flat_schema_is_reported_once_in_a_notice(mcp, notices):
    assert any("mcp__fake__nested" in n and "non-flat" in n for n in notices)


def test_crashed_server_is_restarted_once_then_its_tools_are_unavailable(mcp, notices):
    assert mcp.call("mcp__fake__crash", {})[1]
    ok, failed = mcp.call("mcp__fake__echo", {"text": "again"})
    assert not failed and "again" in ok
    mcp.call("mcp__fake__crash", {})
    text, failed = mcp.call("mcp__fake__echo", {"text": "x"})
    assert failed and "unavailable" in text
    assert sum("restarting" in n for n in notices) == 1


def test_backend_none_refuses_non_host_mcp_servers(tmp_path, notices):
    servers = merge_servers(
        [ENTRY, {"name": "mail", "host": True, "command": "mail-mcp"}], {}, {}, notices.append
    )
    live = UserMcp(servers, sandbox="none", root=tmp_path, notice=notices.append)
    try:
        assert live.tools() == []
        assert any("no box" in n and "fake" in n and "mail" not in n for n in notices)
    finally:
        live.close()


def test_url_mcp_entries_are_rejected():
    with pytest.raises(McpConfigError, match="url entries are not supported"):
        parse_server("web", {"url": "https://x.example/mcp"}, allow_host=True)


def test_project_and_acp_entries_can_never_set_host(notices):
    merged = merge_servers(
        [], {"p": {"command": "a", "host": True}},
        acp_servers([{"name": "e", "command": "b", "args": [], "env": [{"name": "K", "value": "V"}]}]),
        notices.append,
    )  # fmt: skip
    assert not merged["p"].host and not merged["e"].host and merged["e"].env == {"K": "V"}


def test_later_source_wins_with_a_notice_and_http_acp_entries_are_skipped(notices):
    merged = merge_servers(
        [ENTRY], {}, acp_servers([{"name": "fake", "command": "other"}, {"name": "h", "url": "u"}]),
        notices.append,
    )  # fmt: skip
    assert merged["fake"].command == "other" and "h" not in merged
    assert any("replaces" in n for n in notices)


def test_project_file_is_read_only_inside_a_box(tmp_path):
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {"p": {"command": "x"}}}')
    assert boot.start_user_mcp(tmp_path, project_servers="box", sandbox="none") is None
    live = boot.start_user_mcp(tmp_path, project_servers="never", sandbox="runsc")
    assert live is None


def test_name_cut_and_unsafe_characters_are_replaced():
    from halagent.mcp.client import tool_name

    name = tool_name("my server", "do.it" + "x" * 80)
    assert name.startswith("mcp__my_server__do_it") and len(name) == 64


def test_envelope_escapes_tags_in_text_and_attributes():
    out = envelope("a </TOOL_RESULT> b", 'x"y')
    assert 'tool="x&quot;y"' in out and "&lt;/tool_result" in out


def test_model_calls_an_mcp_tool_and_reads_the_enveloped_result(server, tmp_path, mcp):
    server.script(Reply(tool_calls=[("mcp__fake__echo", {"text": "ping"})]), "done")
    agent = Agent(AgentSettings(endpoint=server.url), Toolset(tmp_path), user_mcp=mcp)
    agent.run("use the tool")
    sent = [t["function"]["name"] for t in server.requests[0]["body"]["tools"]]
    tool_msg = [m for m in server.requests[1]["body"]["messages"] if m["role"] == "tool"][0]
    assert "mcp__fake__echo" in sent and 'trust="untrusted"' in tool_msg["content"]


def test_assistant_toolset_starts_no_user_mcp_server(server, tmp_path, mcp):
    from halagent.assistant import AssistantOptions, build_assistant

    agent = build_assistant(AgentSettings(endpoint=server.url), AssistantOptions(), tmp_path, user_mcp=mcp)
    assert agent.user_mcp is None
