"""Conformance of every ACP message in a full session against the schema fixture (REQ-AGT-79).

The fixture is a hand-written subset (see tests/fixtures/acp/README.md). When the upstream file
is vendored next to it, an integrity check on its sha256 is enabled as well.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from halkit.testing.model_server import Reply


def _load_helpers():
    """Load the ACP test helpers by path: the suite runs with importlib import mode."""
    path = Path(__file__).with_name("test_acp.py")
    spec = importlib.util.spec_from_file_location("acp_test_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_helpers = _load_helpers()
Client, reply_to, wait_for_request = _helpers.Client, _helpers.reply_to, _helpers.wait_for_request
FIXTURES = Path(__file__).parent / "fixtures" / "acp"
SCHEMA = json.loads((FIXTURES / "schema.subset.json").read_text())


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator({**SCHEMA, "$ref": f"#/$defs/{name}"})


AGENT, CLIENT = _validator("agentMessage"), _validator("clientMessage")


class Recorded(Client):
    """A `Client` that also keeps everything it sends to the agent."""

    def __init__(self, *args):
        super().__init__(*args)
        self.sent: list[dict] = []
        put = self.inp.put

        def record(obj):
            self.sent.append(obj)
            put(obj)

        self.inp.put = record


def _check(c: Recorded, msgs: list[dict]) -> None:
    for m in msgs:
        AGENT.validate(m)
    for m in c.sent:
        CLIENT.validate(m)


def test_session_with_tools_and_review_permission_conforms_to_the_schema(server, tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    server.script(
        Reply(tool_calls=[("read", {"path": "a.txt"})]),
        Reply(tool_calls=[("write", {"path": "n.txt", "content": "x"})]),
        "done",
    )
    c = Recorded(server.url, tmp_path)
    init = c.call("initialize", {"protocolVersion": 1})
    new = c.call("session/new", {"cwd": str(tmp_path), "mcpServers": []})
    sid = c.sid()
    mode = c.call("session/set_mode", {"sessionId": sid, "modeId": "review"})
    prompt = c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})
    ask = wait_for_request(c, "session/request_permission")
    c.inp.put(
        {
            "jsonrpc": "2.0",
            "id": ask["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
        }
    )
    msgs = c.finish()
    _check(c, msgs)
    Draft202012Validator({**SCHEMA, "$ref": "#/$defs/initializeResult"}).validate(
        reply_to(msgs, init)["result"]
    )
    _validator("newSessionResult").validate(reply_to(msgs, new)["result"])
    assert reply_to(msgs, mode)["result"] is not None
    _validator("promptResult").validate(reply_to(msgs, prompt)["result"])


def test_cancelled_prompt_and_error_replies_conform_to_the_schema(server, tmp_path):
    server.script(*[Reply(tool_calls=[("run", {"command": "sleep 0.3"})]) for _ in range(30)])
    c = Recorded(server.url, tmp_path)
    c.call("session/new", {"cwd": str(tmp_path)})
    sid = c.sid()
    rid = c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "x"}]})
    c.inp.put({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
    bad = c.call("session/no_such_method", {})
    msgs = c.finish()
    c.sent = [m for m in c.sent if m.get("id") != bad]  # the unknown method is deliberately invalid
    _check(c, [m for m in msgs if m.get("id") != bad])
    _validator("errorResponse").validate(reply_to(msgs, bad))
    assert reply_to(msgs, bad)["error"]["code"] == -32601
    _validator("promptResult").validate(reply_to(msgs, rid)["result"])


def test_schema_rejects_a_session_update_with_an_unknown_kind():
    bad = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "s", "update": {"sessionUpdate": "tool_call", "title": "x"}},
    }
    with pytest.raises(Exception, match="toolCallId|valid"):
        AGENT.validate(bad)


def test_schema_rejects_a_prompt_whose_stop_reason_is_invented():
    with pytest.raises(Exception, match="stopReason|valid|one of"):
        _validator("promptResult").validate({"stopReason": "finished"})


def test_vendored_upstream_schema_matches_its_recorded_sha256():
    schema, source = FIXTURES / "schema.json", FIXTURES / "schema.json.source"
    if not schema.exists():
        pytest.skip("upstream schema not vendored yet (see tests/fixtures/acp/README.md)")
    lines = dict(line.split("=", 1) for line in source.read_text().splitlines() if "=" in line)
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == lines["sha256"].strip()
