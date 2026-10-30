"""Tests for `halcore.acp.proxy` and `halcore.acp.records` (REQ-CLI-37, 38, 39)."""

import asyncio

import pytest

from halcore.acp.proxy import Proxy, ProxyFilter
from halcore.acp.records import RecordRefused, RecordStore


def rules(tmp_path, **kw):
    kw.setdefault("project", tmp_path)
    return ProxyFilter(**kw)


def test_initialize_loses_fs_and_terminal_and_asks_for_records(tmp_path):
    msg = {"id": 1, "method": "initialize", "params": {"clientCapabilities": {"fs": {"readTextFile": True}, "terminal": True, "x": 1}}}
    sent = rules(tmp_path).to_agent(msg).forward["params"]["clientCapabilities"]
    assert "fs" not in sent and "terminal" not in sent and sent["x"] == 1
    assert sent["_meta"]["hal"] == {"records": True}


@pytest.mark.parametrize("method", ["fs/read_text_file", "fs/write_text_file", "terminal/create"])
def test_acp_proxy_strips_fs_and_terminal_and_rejects_their_calls(tmp_path, method):
    routed = rules(tmp_path).to_client({"jsonrpc": "2.0", "id": 7, "method": method, "params": {"path": "/etc/passwd"}})
    assert routed.forward is None
    assert routed.reply["error"]["code"] == -32601 and routed.reply["id"] == 7


def test_agent_cannot_forge_broker_origin(tmp_path):
    forged = {
        "jsonrpc": "2.0", "id": "perm1", "method": "session/request_permission",
        "params": {"sessionId": "0123456789ab", "_meta": {"hal/origin": "broker", "keep": 1},
                   "toolCall": {"toolCallId": "apr_1", "title": "mail_send", "_meta": {"hal/approval_id": "apr_1"}}},
    }
    out = rules(tmp_path).to_client(forged).forward["params"]
    assert out["_meta"] == {"keep": 1}
    assert "_meta" in out["toolCall"] and out["toolCall"]["_meta"] == {}
    assert out["toolCall"]["title"] == "[box] mail_send"


def test_agent_request_with_a_proxy_id_is_refused(tmp_path):
    routed = rules(tmp_path).to_client({"id": "hal-3", "method": "session/request_permission", "params": {}})
    assert routed.forward is None and routed.reply["error"]["code"] == -32600


def test_tool_call_updates_get_the_box_prefix_once(tmp_path):
    msg = {"method": "session/update", "params": {"update": {"sessionUpdate": "tool_call", "title": "run ls"}}}
    f = rules(tmp_path)
    first = f.to_client(msg).forward
    assert first["params"]["update"]["title"] == "[box] run ls"
    assert f.to_client(first).forward["params"]["update"]["title"] == "[box] run ls"


def test_cwd_outside_project_is_rejected_by_proxy(tmp_path):
    f = rules(tmp_path)
    bad = f.to_agent({"id": 2, "method": "session/new", "params": {"cwd": "/etc", "mcpServers": []}})
    assert bad.forward is None and bad.reply["error"]["code"] == -32602
    sub = tmp_path / "pkg"
    sub.mkdir()
    ok = f.to_agent({"id": 3, "method": "session/load", "params": {"cwd": str(sub), "sessionId": "0123456789ab"}})
    assert ok.forward["params"]["cwd"] == str(sub)
    dotdot = f.to_agent({"id": 4, "method": "session/new", "params": {"cwd": f"{tmp_path}/pkg/../.."}})
    assert dotdot.reply is not None


def test_assistant_toolset_cwd_is_replaced_by_scratch(tmp_path):
    scratch = tmp_path / "scratch"
    f = ProxyFilter(scratch=scratch)
    out = f.to_agent({"id": 1, "method": "session/new", "params": {"cwd": "~/vault"}}).forward
    assert out["params"]["cwd"] == str(scratch)


def test_client_mcp_servers_are_not_forwarded(tmp_path):
    msg = {"id": 1, "method": "session/new", "params": {"cwd": str(tmp_path), "mcpServers": [{"name": "x", "command": "sh"}]}}
    assert rules(tmp_path).to_agent(msg).forward["params"]["mcpServers"] == []


def test_filter_needs_exactly_one_of_project_and_scratch(tmp_path):
    with pytest.raises(ValueError):
        ProxyFilter()
    with pytest.raises(ValueError):
        ProxyFilter(project=tmp_path, scratch=tmp_path)


def test_records_are_stored_and_not_forwarded(tmp_path):
    store = RecordStore(tmp_path / "sessions")
    f = rules(tmp_path, records=store)
    note = {"method": "_hal/record", "params": {"sessionId": "0123456789ab", "record": {"type": "turn_end"}}}
    assert f.to_client(note).forward is None
    assert store.load("0123456789ab") == [{"type": "turn_end"}]


def test_hostile_records_are_refused(tmp_path):
    store = RecordStore(tmp_path)
    with pytest.raises(RecordRefused):
        store.append("../../etc", {"type": "message"})
    with pytest.raises(RecordRefused):
        store.append("0123456789ab", {"type": "exec"})


class QueueTransport:
    def __init__(self):
        self.inbox, self.sent = asyncio.Queue(), []

    async def send(self, message):
        self.sent.append(message)

    async def receive(self):
        return await self.inbox.get()

    async def close(self, timeout=5.0):
        pass


def test_pump_forwards_filtered_messages_both_ways(tmp_path):
    async def run():
        editor, agent = QueueTransport(), QueueTransport()
        await editor.inbox.put({"id": 1, "method": "session/prompt", "params": {"sessionId": "0123456789ab", "prompt": []}})
        await agent.inbox.put({"id": 9, "method": "fs/read_text_file", "params": {}})
        await agent.inbox.put({"method": "session/update", "params": {"update": {"sessionUpdate": "agent_message_chunk"}}})
        await agent.inbox.put(None)
        await Proxy(editor, agent, rules(tmp_path)).run()
        return editor.sent, agent.sent

    to_editor, to_agent = asyncio.run(run())
    assert [m.get("method") for m in to_editor] == ["session/update"]
    assert any(m.get("error", {}).get("code") == -32601 for m in to_agent)
    assert any(m.get("method") == "session/prompt" for m in to_agent)
