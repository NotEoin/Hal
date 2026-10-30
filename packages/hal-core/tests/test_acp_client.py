"""Tests for `halcore.acp.transport` and `halcore.acp.client` against a scripted fake agent."""

import asyncio
import sys
import textwrap

import pytest

from halcore.acp import transport
from halcore.acp.client import AcpClient, AcpError
from halcore.acp.transport import AgentCrashed, SubprocessTransport

FAKE_AGENT = textwrap.dedent(
    """
    import json, sys
    mode = sys.argv[1]
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
    for raw in sys.stdin:
        msg = json.loads(raw)
        if "method" not in msg:
            continue
        rid, method = msg.get("id"), msg["method"]
        if mode == "huge" and method == "initialize":
            sys.stdout.write("x" * 5000 + "\\n"); sys.stdout.flush(); continue
        if mode == "deep" and method == "initialize":
            sys.stdout.write("[" * 200 + "]" * 200 + "\\n"); sys.stdout.flush(); continue
        if mode == "noisy":
            sys.stderr.write("e" * 100 + "\\n"); sys.stderr.flush()
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "0123456789ab"}})
        elif method == "session/prompt":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "0123456789ab",
                  "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}}})
            send({"jsonrpc": "2.0", "id": "p1", "method": "fs/read_text_file", "params": {"path": "/etc/passwd"}})
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nope"}})
    """
)


class Collect:
    def __init__(self):
        self.updates = []

    async def on_update(self, session_id, update):
        self.updates.append((session_id, update))

    async def on_record(self, session_id, record):
        pass

    async def request_permission(self, session_id, tool_call, options):
        return {"outcome": "cancelled"}


async def spawn(tmp_path, mode):
    script = tmp_path / "agent.py"
    script.write_text(FAKE_AGENT)
    return await SubprocessTransport.spawn(
        [sys.executable, str(script), mode], env=None, stderr_log=tmp_path / "stderr.log"
    )


def test_prompt_streams_updates_and_returns_the_stop_reason(tmp_path):
    async def run():
        handler = Collect()
        client = AcpClient(await spawn(tmp_path, "ok"), handler)
        await client.initialize()
        sid = await client.new_session("/work", [])
        reason = await client.prompt(sid, [{"type": "text", "text": "hello"}])
        await client.close()
        return sid, reason, handler.updates

    sid, reason, updates = asyncio.run(run())
    assert (sid, reason) == ("0123456789ab", "end_turn")
    assert updates[0][1]["content"]["text"] == "hi"


def test_agent_error_response_raises_acp_error(tmp_path):
    async def run():
        client = AcpClient(await spawn(tmp_path, "ok"), Collect())
        await client.initialize()
        try:
            await client.set_mode("0123456789ab", "auto")
        finally:
            await client.close()

    with pytest.raises(AcpError, match="nope"):
        asyncio.run(run())


def test_oversize_acp_line_ends_session(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "ACP_MAX_MESSAGE_BYTES", 1024)

    async def run():
        client = AcpClient(await spawn(tmp_path, "huge"), Collect())
        try:
            await client.initialize()
        finally:
            await client.close()

    with pytest.raises(AgentCrashed, match="longer than"):
        asyncio.run(run())


def test_deep_json_ends_session(tmp_path):
    async def run():
        client = AcpClient(await spawn(tmp_path, "deep"), Collect())
        try:
            await client.initialize()
        finally:
            await client.close()

    with pytest.raises(AgentCrashed, match="nests deeper"):
        asyncio.run(run())


def test_agent_stderr_log_rotates(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "AGENT_STDERR_MAX_BYTES", 300)

    async def run():
        client = AcpClient(await spawn(tmp_path, "noisy"), Collect())
        await client.initialize()
        for _ in range(8):
            await client.new_session("/work", [])
        await client.close()

    asyncio.run(run())
    assert (tmp_path / "stderr.log.1").exists()
    assert not (tmp_path / "stderr.log.3").exists()


def test_depth_check_ignores_brackets_inside_strings():
    assert transport.json_depth_ok(b'{"a": "' + b"[" * 500 + b'"}')
    assert not transport.json_depth_ok(b"[" * 65)
