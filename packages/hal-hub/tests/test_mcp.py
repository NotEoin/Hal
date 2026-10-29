"""Tests for `halhub.mcp`, the v1 stdio loop, now calling through the hub facade.

Plan step 18 replaces the loop with the mcp SDK server and this file with its tests.
"""

from __future__ import annotations

import json

from hubkit import SpyRunner

from halhub import mcp


def test_mcp_lists_meta_tools_and_calls_through_the_pipeline(process_hub):
    process_hub(runner=SpyRunner(result=2))
    listed = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "hal_call" in [t["name"] for t in listed["result"]["tools"]]
    request = {"name": "hal_call", "arguments": {"tool": "demo_echo", "arguments": {"text": "hi"}}}
    reply = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": request})
    data = json.loads(reply["result"]["content"][0]["text"])
    assert (data["status"], data["result"]) == ("ok", 2) and not reply["result"]["isError"]


def test_mcp_call_of_ask_tool_is_an_error_not_a_confirmation_request(process_hub):
    process_hub(runner=SpyRunner())
    request = {"name": "demo_wipe", "arguments": {"path": "/tmp/x", "_confirmed": True}}
    reply = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": request})
    data = json.loads(reply["result"]["content"][0]["text"])
    assert data["rule"] == "no-approver" and reply["result"]["isError"]
