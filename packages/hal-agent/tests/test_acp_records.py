"""Tests for `_hal/record`, `session/load` and the in-process `acp.serve` entry point."""

import io
import json
import sys

from halagent import acp, cli
from halagent.sessions import SessionStore
from halagent.settings import AgentSettings
from halkit import paths

SID = "3f9a1c0b7e2d"
CALL_ARGS = json.dumps({"pattern": "*"})
ROOT = ""  # empty: the server falls back to its workspace root


def run_serve(server, root, lines, **kw):
    """Runs `acp.serve` over `lines` (JSON objects) and returns the parsed output messages."""
    rfile = io.StringIO("".join(json.dumps(x) + "\n" for x in lines))
    wfile = io.StringIO()
    settings = AgentSettings(endpoint=server.url, model="fake-model")
    acp.serve(settings, root, rfile, wfile, **kw)
    return [json.loads(x) for x in wfile.getvalue().splitlines()]


def rpc(i, method, params=None):
    return {"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}


def init(records):
    caps = {"_meta": {"hal": {"records": True}}} if records else {}
    return rpc(1, "initialize", {"protocolVersion": 1, "clientCapabilities": caps})


def prompt(i, text):
    return rpc(i, "session/prompt", {"sessionId": SID, "prompt": [{"type": "text", "text": text}]})


def new(i=2):
    return rpc(i, "session/new", {"cwd": ROOT, "_meta": {"hal": {"sessionId": SID}}})


def test_capabilities_follow_the_client_records_flag(server, tmp_path):
    on = run_serve(server, tmp_path, [init(True)])[0]["result"]["agentCapabilities"]
    off = run_serve(server, tmp_path, [init(False)])[0]["result"]["agentCapabilities"]
    assert (on["loadSession"], on["_meta"]["hal"]["records"], off["_meta"]["hal"]["records"]) == (
        True,
        True,
        False,
    )


def test_records_go_to_the_host_and_no_transcript_file_is_written(server, tmp_path):
    server.script("hello")
    out = run_serve(server, tmp_path, [init(True), new(), prompt(3, "hi")])
    recs = [m["params"]["record"] for m in out if m.get("method") == "_hal/record"]
    types = [r["type"] for r in recs]
    assert types[0] == "session" and recs[0]["id"] == SID and "turn_end" in types
    assert all(m["params"]["sessionId"] == SID for m in out if m.get("method") == "_hal/record")
    assert not list((paths.state_dir() / "sessions").glob("*"))


def test_without_the_flag_the_agent_writes_the_transcript_itself(server, tmp_path):
    server.script("hello")
    out = run_serve(server, tmp_path, [init(False), new(), prompt(3, "hi")])
    assert not [m for m in out if m.get("method") == "_hal/record"]
    assert SessionStore(paths.state_dir() / "sessions").load(SID)


def history_records():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "glob", "arguments": CALL_ARGS}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "a.txt"},
        {"role": "assistant", "content": "one file"},
    ]  # fmt: skip
    return [{"type": "message", "turn": 1, "i": i, "msg": m} for i, m in enumerate(msgs)]


def load(i, records=None):
    meta = {"hal": {"records": records}} if records is not None else {}
    return rpc(i, "session/load", {"sessionId": SID, "cwd": ROOT, "mcpServers": [], "_meta": meta})


def test_load_replays_history_as_updates_then_the_session_continues(server, tmp_path):
    server.script("next answer")
    out = run_serve(server, tmp_path, [init(True), load(2, history_records()), prompt(3, "more")])
    kinds = [m["params"]["update"]["sessionUpdate"] for m in out if m.get("method") == "session/update"]
    assert kinds[0] == "user_message_chunk"
    assert {"tool_call", "tool_call_update"} <= set(kinds)
    assert next(m for m in out if m.get("id") == 2)["result"] == {}
    sent = [m["content"] for m in server.requests[0]["body"]["messages"]]
    assert "list files" in sent and "more" in sent[-1]


def test_load_of_an_unknown_session_is_an_invalid_params_error(server, tmp_path):
    out = run_serve(server, tmp_path, [init(False), load(2)])
    assert next(m for m in out if m.get("id") == 2)["error"]["code"] == -32602


def test_load_reads_the_local_store_without_host_records(server, tmp_path):
    server.script("first", "second")
    run_serve(server, tmp_path, [init(False), new(), prompt(3, "one")])
    out = run_serve(server, tmp_path, [init(False), load(2), prompt(3, "two")])
    assert next(m for m in out if m.get("id") == 2)["result"] == {}
    updates = [m["params"]["update"] for m in out if m.get("method") == "session/update"]
    texts = [u["content"]["text"] for u in updates if u["sessionUpdate"] == "user_message_chunk"]
    assert texts == ["one"]


def test_serve_and_the_command_line_send_identical_messages(server, tmp_path, monkeypatch):
    script = [init(False), new(), prompt(3, "hi")]
    server.script("same", "same")
    direct = run_serve(server, tmp_path, script, no_session=True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(json.dumps(x) + "\n" for x in script)))
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    code = cli.main(
        ["--acp", "--no-session", "--endpoint", server.url, "--model", "fake-model", "-C", str(tmp_path)]
    )
    assert code == 0
    assert [json.loads(x) for x in captured.getvalue().splitlines()] == direct


def test_fork_opens_a_copy_under_a_new_id_and_the_source_is_unchanged(server, tmp_path):
    store = SessionStore(paths.state_dir() / "sessions")
    head = {"type": "session", "v": 1, "created": "2026-10-31T00:00:00Z", "id": SID, "cwd": str(tmp_path)}
    writer = store.path(SID)
    writer.parent.mkdir(parents=True, exist_ok=True)
    writer.write_text("".join(json.dumps(r) + "\n" for r in [head, *history_records()]), encoding="ascii")
    out = run_serve(server, tmp_path, [init(False), rpc(2, "session/fork", {"sessionId": SID})])
    new_id = next(m for m in out if m.get("id") == 2)["result"]["sessionId"]
    assert new_id != SID and store.load(new_id)[0]["forked_from"] == SID
    kinds = [m["params"]["update"]["sessionUpdate"] for m in out if m.get("method") == "session/update"]
    assert "user_message_chunk" in kinds and "agent_message_chunk" in kinds
    assert len(store.load(SID)) == 6  # the source file is not touched


def test_fork_of_an_unknown_session_is_a_params_error(server, tmp_path):
    out = run_serve(server, tmp_path, [init(False), rpc(2, "session/fork", {"sessionId": SID})])
    assert next(m for m in out if m.get("id") == 2)["error"]["code"] == -32602


def test_fork_is_refused_when_the_host_keeps_the_records(server, tmp_path):
    out = run_serve(server, tmp_path, [init(True), rpc(2, "session/fork", {"sessionId": SID})])
    assert "host copies its own records" in next(m for m in out if m.get("id") == 2)["error"]["message"]
