"""Tests that `hal chat` and `hal acp` reach the agent only over ACP through a box (RESOLUTIONS I1)."""

import argparse
import asyncio
import io
import sys
import types

import pytest
from halhub.approvals import control

from halcore import acpcmd, chat, sessions
from halcore.acp import permissions
from halcore.acp.transport import SubprocessTransport
from test_acp_client import FAKE_AGENT


class FakeBroker:
    def __init__(self):
        self.heard = []

    def on_human_message(self, text, surface):
        self.heard.append((text, surface))


def fake_box(tmp_path, monkeypatch, log):
    """Replaces `BoxedAgent.start` with a fake agent process and a recording broker."""
    script = tmp_path / "agent.py"
    script.write_text(FAKE_AGENT)
    broker = FakeBroker()

    async def start(**kwargs):
        log.append(kwargs)
        transport = await SubprocessTransport.spawn(
            [sys.executable, str(script), "ok"], env=None, stderr_log=tmp_path / "stderr.log"
        )
        session = types.SimpleNamespace(broker=broker, iso=None, id="0123456789ab")
        return types.SimpleNamespace(session=session, transport=transport, close=transport.close)

    monkeypatch.setattr(sessions.BoxedAgent, "start", staticmethod(start))
    monkeypatch.setattr(sessions, "BoxedAgent", sessions.BoxedAgent)
    return broker


def test_chat_message_runs_in_an_assistant_box_and_tells_the_broker_first(tmp_path, monkeypatch, capsys):
    log = []
    broker = fake_box(tmp_path, monkeypatch, log)
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    args = argparse.Namespace(message=["hello"], model=None, json=True, channel="chat")
    assert asyncio.run(chat.run(args)) == 0
    assert log[0]["kind"] == "chat" and log[0]["project"] is None
    assert broker.heard == [("hello", "tui")]
    assert '"reply": "hi"' in capsys.readouterr().out


def test_chat_output_is_escaped_before_it_reaches_the_terminal():
    out = io.StringIO()
    handler = chat.ChatHandler(out)
    update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "a\x1b]0;pwned\x07b"}}
    asyncio.run(handler.on_update("s", update))
    assert "\x1b" not in out.getvalue() and "\\u001b" in out.getvalue()


class EditorPipe:
    def __init__(self, messages):
        self.inbox, self.sent = asyncio.Queue(), []
        for m in messages:
            self.inbox.put_nowait(m)

    async def send(self, message):
        self.sent.append(message)

    async def receive(self):
        return await self.inbox.get()

    async def close(self, timeout=5.0):
        pass


def test_acp_prompt_reaches_the_broker_hook_with_surface_acp_before_the_agent(tmp_path, monkeypatch):
    log = []
    broker = fake_box(tmp_path, monkeypatch, log)
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(control, "list_pending", lambda: [])
    monkeypatch.setattr(permissions, "POLL_INTERVAL_S", 0.01)
    editor = EditorPipe(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientCapabilities": {"fs": {}}}},
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path), "mcpServers": []}},
            {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
             "params": {"sessionId": "0123456789ab", "prompt": [{"type": "text", "text": "list files"}]}},
        ]
    )  # fmt: skip

    async def stdio():
        return editor

    monkeypatch.setattr(acpcmd, "stdio_transport", stdio)

    async def run():
        opts = acpcmd._parser().parse_args([str(tmp_path)])
        task = asyncio.ensure_future(acpcmd.serve(opts))
        for _ in range(200):
            await asyncio.sleep(0.02)
            if any(m.get("id") == 3 for m in editor.sent):
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert broker.heard == [("list files", "acp")]
    assert log[0]["kind"] == "code" and log[0]["project"] == tmp_path.resolve()
    replies = {m.get("id"): m for m in editor.sent if "id" in m and "method" not in m}
    assert replies[3]["result"]["stopReason"] == "end_turn"
    assert not any(str(m.get("method", "")).startswith("fs/") for m in editor.sent)


def test_acp_assistant_toolset_needs_a_channel():
    opts = acpcmd._parser().parse_args(["--toolset", "assistant"])
    with pytest.raises(acpcmd.SandboxError, match="--channel"):
        asyncio.run(acpcmd.serve(opts))
