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


def test_unboxed_assistant_needs_recorded_consent(tmp_path, monkeypatch):
    from halsandbox.errors import SandboxError

    from halcore import unboxed

    monkeypatch.setenv("HAL_STATE", str(tmp_path))
    with pytest.raises(SandboxError, match="no consent"):
        unboxed.require_consent("chat", interactive=False)
    with pytest.raises(SandboxError, match="not confirmed"):
        unboxed.require_consent("chat", interactive=True, ask=lambda _: "yes")
    unboxed.require_consent("chat", interactive=True, ask=lambda _: "y", now="2026-10-30T00:00:00Z")
    assert unboxed.has_consent("chat") and not unboxed.has_consent("voice")
    unboxed.require_consent("chat", interactive=False)  # recorded: no terminal needed now


def test_unboxed_consent_file_that_is_not_an_object_fails_loudly(tmp_path, monkeypatch):
    from halsandbox.errors import SandboxError

    from halcore import unboxed

    monkeypatch.setenv("HAL_STATE", str(tmp_path))
    (tmp_path / unboxed.CONSENT_FILE).write_text("[1]")
    with pytest.raises(SandboxError, match="JSON object"):
        unboxed.has_consent("chat")


def test_code_refusal_by_the_sandbox_exits_3(monkeypatch, capsys):
    from halcore import code

    def refuse(*a, **k):
        raise code.sandbox.SandboxError("docker answered before and does not now")

    monkeypatch.setattr(code.sandbox, "run_code", refuse)
    monkeypatch.setattr(sys, "argv", ["hal", "code", "--", "x"])
    assert code.main(["--", "x"]) == 3
    assert "docker answered before" in capsys.readouterr().err


def _headless_run(output_format, tmp_path, monkeypatch, assistant=True):
    from halcore import headless

    log = []
    broker = fake_box(tmp_path, monkeypatch, log)
    monkeypatch.setenv("HAL_STATE", str(tmp_path / "state"))
    args = argparse.Namespace(
        output_format=output_format, assistant=assistant, dir=str(tmp_path), model=None, prompt=[]
    )
    out = io.StringIO()
    status = asyncio.run(headless.run(args, "hello", out))
    return status, out.getvalue(), broker, log


def test_headless_json_prints_one_result_object_and_tells_the_broker(tmp_path, monkeypatch):
    import json

    status, out, broker, log = _headless_run("json", tmp_path, monkeypatch)
    result = json.loads(out)
    assert status == 0 and result["reply"] == "hi" and result["stop_reason"] == "end_turn"
    assert broker.heard == [("hello", "headless")] and log[0]["kind"] == "chat"


def test_headless_stream_json_emits_updates_then_a_result_line(tmp_path, monkeypatch):
    import json

    status, out, _, _ = _headless_run("stream-json", tmp_path, monkeypatch, assistant=False)
    lines = [json.loads(line) for line in out.splitlines()]
    assert status == 0 and lines[0]["type"] == "update" and lines[-1]["type"] == "result"


def test_headless_text_streams_the_reply_without_json(tmp_path, monkeypatch):
    status, out, _, _ = _headless_run("text", tmp_path, monkeypatch)
    assert status == 0 and out.strip() == "hi"


def test_headless_without_a_prompt_is_a_usage_error():
    from halcore import headless

    with pytest.raises(ValueError, match="no prompt"):
        headless._read_prompt([], io.StringIO(""))
