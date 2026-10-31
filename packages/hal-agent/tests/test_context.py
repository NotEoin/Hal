"""Tests for `halagent.context`: estimate, masking, summary and note fallback, state note."""

import pytest

from halagent.context import ContextManager, ContextOverflow, StateNote, estimate_tokens
from halagent.loop import Agent
from halagent.sessions import SessionWriter, read_records, replay
from halagent.settings import AgentSettings
from halagent.tools import Toolset
from halkit.testing.model_server import Reply


def history(n_exchanges, size=2000):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for k in range(n_exchanges):
        call = {
            "id": f"c{k}",
            "type": "function",
            "function": {"name": "read", "arguments": f'{{"path": "f{k}"}}'},
        }
        msgs.append({"role": "assistant", "content": None, "tool_calls": [call]})
        msgs.append({"role": "tool", "tool_call_id": f"c{k}", "content": "x" * size})
    return msgs


def test_history_under_the_threshold_is_sent_unchanged():
    msgs = history(2, size=100)
    out = ContextManager(32_768, 1024).fit(msgs, list(range(len(msgs))), StateNote())
    assert out.messages == msgs and out.records == []


def test_old_tool_output_is_masked_but_the_recent_exchanges_are_kept():
    msgs = history(10)
    cm = ContextManager(6_000, 500)
    out = cm.fit(msgs, list(range(len(msgs))), StateNote())
    stubs = [
        m["content"]
        for m in out.messages
        if m.get("role") == "tool" and m["content"].startswith("[hal] output of read f0")
    ]
    assert stubs and "2000 characters" in stubs[0]
    assert out.messages[-1]["content"] == "x" * 2000
    assert out.records[0]["kind"] == "mask" and out.records[0]["masked"]


def test_summary_replaces_old_messages_and_keeps_the_state_note():
    msgs = history(12, size=3000)
    note = StateNote(task="fix it")
    cm = ContextManager(5_000, 500, summarize=lambda old: "## Task\nfix it")
    out = cm.fit(msgs, list(range(len(msgs))), note)
    summary = [r for r in out.records if r["kind"] == "summary"][0]
    assert summary["replaces"][0] == 2  # the latest user message stays in front
    first = out.messages[out.summary_at]["content"]
    assert first.startswith("[hal] The earlier part") and "## Task" in first and "[hal state note]" in first


def test_failing_summary_falls_back_to_the_state_note_alone():
    def boom(old):
        raise RuntimeError("model down")

    msgs = history(12, size=3000)
    out = ContextManager(5_000, 500, summarize=boom).fit(msgs, list(range(len(msgs))), StateNote(task="t"))
    assert [r["kind"] for r in out.records if r["kind"] != "mask"] == ["note"]
    assert "[hal state note]" in out.messages[out.summary_at]["content"]


def test_a_single_huge_message_is_cut_to_head_and_tail():
    msgs = history(1, size=60_000)
    out = ContextManager(10_000, 500).fit(msgs, list(range(len(msgs))), StateNote())
    assert "characters cut" in out.messages[-1]["content"]


def test_window_too_small_for_the_reply_is_refused():
    with pytest.raises(ValueError, match="too small"):
        ContextManager(500, 400)


def test_unfittable_request_raises_context_overflow():
    msgs = [{"role": "system", "content": "s" * 40_000}, {"role": "user", "content": "go"}]
    with pytest.raises(ContextOverflow):
        ContextManager(4_000, 500).fit(msgs, [0, 1], StateNote())


def test_state_note_tracks_reads_changes_commands_and_errors():
    n = StateNote(task="t")
    n.observe("read", {"path": "a.py", "start_line": 1, "end_line": 50}, "ok")
    n.observe("edit", {"path": "a.py"}, "Edited a.py.")
    n.observe("run", {"command": "pytest"}, "exit code 1 after 1.0 s\nFAILED x")
    n.observe("edit", {"path": "b.py"}, '{"status": "error", "error_type": "not_found", "message": "m"}')
    text = n.render()
    assert "a.py:1-50" in text and "a.py (edit) x1" in text and "pytest -> exit code 1" in text
    assert "edit b.py not_found (x1)" in text and len(text) <= 3000


def test_estimate_grows_with_content():
    assert estimate_tokens(history(3)) > estimate_tokens(history(1))


def test_agent_compacts_a_long_session_and_a_resume_replays_it(server, tmp_path):
    path = tmp_path / "3f9a1c0b7e2d" / "transcript.jsonl"
    header = {
        "id": "3f9a1c0b7e2d",
        "agent": "t",
        "mode": "headless",
        "toolset": "coding",
        "cwd": "/w",
        "model": "m",
    }
    for k in range(6):  # distinct paths, or the repeat-call watchdog would stop the run
        (tmp_path / f"big{k}.txt").write_text("\n".join(f"line {n:03d} {'y' * 70}" for n in range(60)))
    steps = [Reply(tool_calls=[("read", {"path": f"big{k}.txt"})]) for k in range(6)]
    server.script(*steps, "summary text", "all done")
    w = SessionWriter(path, header)
    settings = AgentSettings(endpoint=server.url, context_window=8_000, max_output=300, max_turns=20)
    agent = Agent(settings, Toolset(tmp_path), session=w)
    agent.run("go " + "z" * 3000)
    w.close()
    kinds = {r.get("kind") for r in read_records(path) if r["type"] == "compaction"}
    assert "mask" in kinds
    replayed = replay(read_records(path))
    assert replayed[0]["role"] == "system" and len(replayed) <= len(agent.messages) + 1


def test_replayed_summary_sorts_where_the_replaced_messages_were():
    def msg(i, role, text):
        return {"type": "message", "i": i, "msg": {"role": role, "content": text}}

    recs = [msg(0, "system", "s"), msg(1, "user", "task"), msg(2, "assistant", "a"), msg(3, "user", "b")]
    recs += [msg(4, "assistant", "tail")]
    recs += [{"type": "compaction", "kind": "summary", "replaces": [2, 3], "summary_i": 5}]
    recs += [msg(5, "user", "SUMMARY")]
    assert [m["content"] for m in replay(recs)] == ["s", "task", "SUMMARY", "tail"]


def _two_turn_agent(server, tmp_path, updates):
    settings = AgentSettings(endpoint=server.url, context_window=16_000, max_output=300)
    return Agent(settings, Toolset(tmp_path), on_update=updates.append)


def test_manual_compact_passes_the_focus_text_to_the_summary_request(server, tmp_path):
    updates: list[dict] = []
    server.script("first answer", "SUMMARY OF WORK")
    agent = _two_turn_agent(server, tmp_path, updates)
    agent.run("hello there")
    agent.compact("keep the db schema")
    summary_request = server.requests[-1]["body"]["messages"]
    assert "keep the db schema" in summary_request[0]["content"]


def test_manual_compact_emits_a_compaction_update_with_token_counts(server, tmp_path):
    updates: list[dict] = []
    server.script("first answer", "SUMMARY OF WORK")
    agent = _two_turn_agent(server, tmp_path, updates)
    agent.run("hello there")
    agent.compact()
    event = next(u for u in updates if u["kind"] == "compaction")
    assert event["reason"] == "manual" and event["before_tokens"] >= 0 and "after_tokens" in event


def test_usage_update_reports_context_used_and_tokens_per_second(server, tmp_path):
    updates: list[dict] = []
    server.script("answer")
    agent = _two_turn_agent(server, tmp_path, updates)
    agent.run("hi")
    usage = [u for u in updates if u["kind"] == "usage"]
    assert usage and {"context_used", "tokens_per_s", "window"} <= set(usage[0])


def test_consecutive_turns_share_an_identical_prompt_prefix_and_tool_list(server, tmp_path):
    server.script("one", "two")
    agent = Agent(AgentSettings(endpoint=server.url), Toolset(tmp_path))
    agent.run("first")
    agent.run("second")
    a, b = (r["body"] for r in server.requests[-2:])
    assert b["messages"][: len(a["messages"])] == a["messages"]  # append-only history
    assert a["tools"] == b["tools"] and a["messages"][0] == b["messages"][0]


def test_requests_ask_llama_cpp_to_reuse_the_prompt_cache(server, tmp_path):
    server.script("one")
    Agent(AgentSettings(endpoint=server.url), Toolset(tmp_path)).run("first")
    assert server.requests[-1]["body"]["cache_prompt"] is True
