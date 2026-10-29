"""Integration tests for `hal-hub mcp` on stdio: a real subprocess in both eras (REQ-HUB-36, REQ-HUB-37).

Marked `slow`: each test starts a Python process that loads the SDK and spawns workers. They
stay hermetic: the Hal home is the temporary one from `conftest.py`, the packs are the
synthetic fixtures, and no network is used.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import anyio
import pytest
from hubkit import add_probe_key
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.slow

ERAS = ("legacy", "2026-07-28")
SERVE_TIMEOUT_S = 60  # a cold start loads the SDK and starts workers; far above the usual 2 s
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "hal-test", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.fixture
def served(hal_home, demo_token, fixtures, tmp_path, monkeypatch):
    """Config for a server over `demo`, `probe` and an in-process module `chatty` that prints."""
    chatty = tmp_path / "chatty"
    (chatty / "halchatty").mkdir(parents=True)
    (chatty / "halchatty" / "__init__.py").write_text("")
    (chatty / "halchatty" / "tools.py").write_text(
        textwrap.dedent(
            """
            def say(text: str) -> str:
                print("unflushed print from an in-process tool")  # noqa: T201 - the test's point
                return text
            """
        )
    )
    (chatty / "hal-module.toml").write_text(
        textwrap.dedent(
            """
            schema = 2
            [module]
            name = "chatty"
            summary = "An in-process tool that prints"
            python = "halchatty.tools"
            inprocess = true

            [[tools]]
            name = "chatty_say"
            function = "say"
            description = "Print a line, then return the text."
            tier = "read"
            [tools.params.text]
            required = true
            """
        )
    )
    dirs = [str(fixtures / "demo"), str(fixtures / "probe"), str(chatty)]
    with hal_home.config.open("a") as fh:  # after `demo_token`'s `[demo]` table
        fh.write(
            f"[hub]\nmodule_paths = {json.dumps(dirs)}\nuse_entry_points = false\n"
            '[hub.runner]\ninprocess_modules = ["chatty"]\nbwrap = "off"\n'
        )
    add_probe_key(hal_home.config, tmp_path)
    # an in-process module imports from the hub's own path, as an installed pack would
    path = os.pathsep.join(p for p in (str(chatty), os.environ.get("PYTHONPATH", "")) if p)
    monkeypatch.setenv("PYTHONPATH", path)
    return hal_home


def raw_session(messages: list[dict], expect: set[int]) -> tuple[list[str], str]:
    """Sends `messages` to `hal-hub mcp` and reads stdout until every id in `expect` has a reply.

    Stdin stays open until then, because the server cancels in-flight calls when it closes.
    Returns every stdout line and the server's stderr.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "halhub", "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )
    assert proc.stdin is not None and proc.stdout is not None
    lines: list[str] = []
    deadline = time.monotonic() + SERVE_TIMEOUT_S
    try:
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in messages))
        proc.stdin.flush()
        waiting = set(expect)
        while waiting:
            ready, _, _ = select.select([proc.stdout], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                raise AssertionError(f"no reply for ids {sorted(waiting)} in {SERVE_TIMEOUT_S} s")
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            waiting.discard(_reply_id(line))
        proc.stdin.close()
        rest, stderr = proc.communicate(timeout=SERVE_TIMEOUT_S)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=SERVE_TIMEOUT_S)
    lines += rest.splitlines()
    return [line for line in lines if line.strip()], stderr


def _reply_id(line: str) -> object:
    try:
        return json.loads(line).get("id")
    except (ValueError, AttributeError):
        return None  # not JSON-RPC: the test's parse step reports it


def test_stdout_carries_only_json_rpc_when_tools_print(served):
    calls = [
        ("chatty_say", {"text": "hi"}),
        ("hal_call", {"tool": "probe_shout", "arguments": {"text": "a"}}),
    ]
    legacy = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-11-25",
            "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]  # fmt: skip
    legacy += [
        {"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": name, "arguments": args}}
        for n, (name, args) in enumerate(calls, 1)
    ]
    modern = [
        {"jsonrpc": "2.0", "id": n, "method": "tools/call",
         "params": {"name": name, "arguments": args, "_meta": MODERN_META}}
        for n, (name, args) in enumerate(calls, 1)
    ]  # fmt: skip
    for messages in (legacy, modern):
        lines, stderr = raw_session(messages, expect={1, 2})
        replies = [json.loads(line) for line in lines]  # a stray print would fail to parse here
        assert all(r["jsonrpc"] == "2.0" for r in replies)
        texts = {r["id"]: r["result"]["content"][0]["text"] for r in replies if r.get("id") in (1, 2)}
        assert texts == {1: "hi", 2: "A"}
        assert "unflushed print from an in-process tool" in stderr


@pytest.mark.parametrize("mode", ERAS)
def test_sdk_client_lists_and_calls_over_stdio(served, mode):
    params = StdioServerParameters(command=sys.executable, args=["-m", "halhub", "mcp"], env=dict(os.environ))

    async def main():
        async with Client(params, mode=mode, read_timeout_seconds=SERVE_TIMEOUT_S) as client:
            listed = await client.list_tools()
            echoed = await client.call_tool("hal_call", {"tool": "demo_echo", "arguments": {"text": "yo"}})
            return [t.name for t in listed.tools], echoed

    names, echoed = anyio.run(main)
    assert names[:3] == ["hal_find_tools", "hal_call", "hal_modules"]
    assert echoed.content[0].text == "yo" and echoed.meta["hal"]["status"] == "ok"


@pytest.mark.parametrize("mode", ERAS)
def test_elicitation_approves_a_call_over_stdio(served, mode):
    import mcp_types as types

    forms = []

    async def accept_once(context, params):
        forms.append(params.message)
        return types.ElicitResult(action="accept", content={"decision": "once"})

    params = StdioServerParameters(command=sys.executable, args=["-m", "halhub", "mcp"], env=dict(os.environ))

    async def main():
        async with Client(
            params, mode=mode, read_timeout_seconds=SERVE_TIMEOUT_S, elicitation_callback=accept_once
        ) as client:
            return await client.call_tool("demo_wipe", {"path": "~/Downloads/stdio"})

    result = anyio.run(main)
    expanded = Path.home() / "Downloads" / "stdio"  # the server runs the tool with the resolved path
    assert result.content[0].text == f"wiped {expanded}" and not result.is_error
    assert len(forms) == 1 and f"path: {expanded}" in forms[0]
