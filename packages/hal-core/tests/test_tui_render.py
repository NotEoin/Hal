"""Tests for the rich renderer: markdown, tool cards, diffs, thinking, todo and injection safety."""

import io

from halcore.tui import theme
from halcore.tui.render import Renderer, clean, diff_rows

RLO = chr(0x202E)
HOSTILE = "\x1b[2J\x1b]0;owned\x07 " + RLO + " [bold red]x[/] evil"


class Utf8(io.StringIO):
    encoding = "utf-8"


def make(unicode_on=False):
    out = Utf8() if unicode_on else io.StringIO()
    look = theme.load_look({"unicode": unicode_on}, stream=out, environ={})
    return Renderer(out, look), out


def msg(text):
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


def test_markdown_blocks_are_committed_before_the_turn_ends():
    r, out = make()
    r.update(msg("# Title\n\nfirst para\n\nsecond, still open"))
    assert "Title" in out.getvalue() and "first para" in out.getvalue()
    assert "second" not in out.getvalue()
    r.end_turn()
    assert "second, still open" in out.getvalue()


def test_open_code_fence_stays_in_the_tail_until_closed():
    r, out = make()
    r.update(msg("```python\nx = 1\n\ny = 2\n"))
    assert "x = 1" not in out.getvalue()
    r.update(msg("```\n\nafter"))
    assert "y = 2" in out.getvalue()


def test_model_text_cannot_inject_escapes_or_markup():
    r, out = make()
    r.update(msg(HOSTILE))
    r.end_turn()
    text = out.getvalue()
    assert "\x1b" not in text and "\x07" not in text and RLO not in text
    assert "[bold red]x[/]" in text


def test_markdown_links_show_their_target():
    r, out = make()
    r.update(msg("[safe](https://evil.example/x)"))
    r.end_turn()
    assert "https://evil.example/x" in out.getvalue()


def test_tool_card_is_one_line_with_status_and_duration():
    r, out = make()
    r.update({"sessionUpdate": "tool_call", "toolCallId": "1", "title": "Read a.py", "status": "pending"})
    r.update({"sessionUpdate": "tool_call_update", "toolCallId": "1", "status": "completed"})
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1 and "Read a.py" in lines[0] and "ok" in lines[0] and "s" in lines[0]


def test_tool_output_is_folded_and_ctrl_o_shows_it_all():
    r, out = make()
    body = "\n".join(f"line{i}" for i in range(30))
    r.update({"sessionUpdate": "tool_call", "toolCallId": "1", "title": "run", "status": "completed",
              "content": [{"type": "content", "content": {"type": "text", "text": body}}]})  # fmt: skip
    assert "line7" in out.getvalue() and "line8" not in out.getvalue() and "Ctrl-O" in out.getvalue()
    r.toggle_verbose()
    assert "line29" in out.getvalue()


def test_edit_shows_a_numbered_coloured_diff_with_counts():
    old, new = "a\nb\nc\n", "a\nB\nc\nd\n"
    rows = diff_rows(old, new)
    assert [k for k, *_ in rows] == ["hunk", "ctx", "del", "add", "ctx", "add"]
    r, out = make()
    r.update({"sessionUpdate": "tool_call", "toolCallId": "1", "title": "Edit", "status": "completed",
              "content": [{"type": "diff", "path": "a.txt", "oldText": old, "newText": new}]})  # fmt: skip
    text = out.getvalue()
    assert "+2 -1" in text and "   2 - b" in text and "   2 + B" in text
    assert r.stats.files == {"a.txt"} and (r.stats.added, r.stats.removed) == (2, 1)


def test_long_diff_is_cut_and_escaped():
    r, out = make()
    r.update({"sessionUpdate": "tool_call", "toolCallId": "1", "title": HOSTILE, "status": "completed",
              "content": [{"type": "diff", "path": "a\x1b.py", "oldText": "x\n" * 200, "newText": HOSTILE}]})  # fmt: skip
    assert "\x1b" not in out.getvalue() and "more lines" in out.getvalue()


def test_subagent_calls_nest_under_their_parent():
    r, out = make()
    r.update({"sessionUpdate": "tool_call", "toolCallId": "p", "title": "task", "status": "pending"})
    r.update({"sessionUpdate": "tool_call", "toolCallId": "c", "title": "child", "status": "completed", "parentToolCallId": "p"})
    child = [ln for ln in out.getvalue().splitlines() if "child" in ln][0]
    assert child.startswith("  `- ")


def test_thinking_is_collapsed_to_a_token_count():
    r, out = make()
    r.update({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "secret " * 40}})
    assert out.getvalue() == ""
    r.update(msg("answer\n\n"))
    assert "thought for ~70 tokens" in out.getvalue() and "secret" not in out.getvalue()


def test_ctrl_o_expands_the_last_folded_thinking_block():
    r, out = make()
    r.update({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hidden reasoning"}})
    r.update(msg("answer\n\n"))
    assert "hidden reasoning" not in out.getvalue() and "Ctrl-O shows it" in out.getvalue()
    r.toggle_verbose()
    assert "hidden reasoning" in out.getvalue()


def test_show_thinking_prints_the_reasoning_dimmed():
    r, out = make()
    r.look = theme.load_look({"show_thinking": True, "unicode": False}, stream=out, environ={})
    r.update({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "pondering"}})
    r.end_turn()
    assert "pondering" in out.getvalue()


def test_todo_panel_is_a_checklist_in_both_glyph_sets():
    entries = [{"content": "write tests", "status": "in_progress"}, {"content": "ship", "status": "pending"}]
    for uni in (False, True):
        r, out = make(uni)
        r.update({"sessionUpdate": "plan", "entries": entries})
        text = out.getvalue()
        assert "write tests" in text and "Todo" in text
        assert (chr(0x25D0) in text) is uni and ("[~]" in text) is not uni


def test_ascii_mode_prints_only_ascii_even_for_cards():
    r, out = make()
    r.update({"sessionUpdate": "tool_call", "toolCallId": "1", "title": "Edit", "status": "completed",
              "content": [{"type": "diff", "path": "a", "oldText": "x", "newText": "y"}]})  # fmt: skip
    r.update({"sessionUpdate": "plan", "entries": [{"content": "t", "status": "completed"}]})
    assert out.getvalue().isascii()


def test_unicode_mode_uses_rounded_boxes():
    r, out = make(True)
    r.update({"sessionUpdate": "plan", "entries": [{"content": "t", "status": "pending"}]})
    assert chr(0x256D) in out.getvalue()


def test_usage_update_is_kept_for_the_status_line():
    r, _ = make()
    r.update({"sessionUpdate": "usage_update", "used": 1200, "size": 16384})
    assert (r.stats.used, r.stats.size) == (1200, 16384)


def test_clean_strips_controls_and_shows_bidi():
    assert clean("a\x1b[31mb\x1b[0m" + RLO) == "ab\\u202e"


def test_approval_panel_shows_risk_args_keys_and_escapes_hostile_text():
    from halcore.tui import approval

    request = {
        "tool": "shell_run", "tier": "system", "pack": "core", "reason": "ask", "scopes": ["once", "session"],
        "risk": "high", "risk_reason": "rm -rf " + HOSTILE, "display": "cmd: " + HOSTILE,
    }  # fmt: skip
    look = theme.load_look({"unicode": False}, stream=io.StringIO(), environ={})
    out = io.StringIO()
    theme.make_console(look, out).print(approval.approval_panel(request, look))
    text = out.getvalue()
    assert "classifier: high" in text and "[a] always this session" in text and "[e]" in text
    assert "\x1b" not in text and RLO not in text and "[bold red]x[/]" in text
    assert text.splitlines()[0].startswith("+") and text.isascii()


def test_approval_edit_key_denies_the_call_and_returns_the_note():
    import asyncio
    import types

    from halcore.tui import approval

    request = {"tool": "t", "tier": "x", "pack": "p", "reason": "r", "display": "d", "scopes": ["once"]}
    sent, notes = [], []
    bridge = types.SimpleNamespace(request_for=lambda rid: request, consume=sent.append)

    async def go():
        async def line():
            return "e"

        async def note():
            return " use pytest -x instead "

        ask = approval.make_asker(bridge, line, io.StringIO(), read_note=note, on_note=notes.append)
        await ask({"id": "r1"})

    asyncio.run(go())
    assert sent[0]["result"]["outcome"]["optionId"] == "deny" and notes == ["use pytest -x instead"]


def test_usage_update_meta_gives_the_measured_tokens_per_second():
    r, _ = make()
    r.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "x" * 400}})
    r.update({"sessionUpdate": "usage_update", "used": 10, "size": 100, "_meta": {"hal": {"tokensPerSecond": 42.5}}})
    assert r.stats.tok_s == 42.5


def test_hal_compaction_prints_a_line_and_lowers_the_context_use():
    r, out = make()
    r.update({"sessionUpdate": "usage_update", "used": 900, "size": 1000})
    r.update({"sessionUpdate": "hal_compaction", "reason": "auto", "method": "summary", "beforeTokens": 900, "afterTokens": 300})
    assert "compacted context (auto, summary) ~900 -> ~300 tokens" in out.getvalue()
    assert r.stats.used == 300 and r.stats.compactions == 1


def test_hal_subagent_lines_nest_under_the_parent_turn():
    r, out = make()
    r.update({"sessionUpdate": "hal_subagent", "subagentId": "1", "agent": "explorer", "phase": "start", "task": "find the parser"})
    r.update({"sessionUpdate": "hal_subagent", "subagentId": "1", "agent": "explorer", "phase": "tool", "tool": "grep"})
    r.update({"sessionUpdate": "hal_subagent", "subagentId": "1", "agent": "explorer", "phase": "done"})
    lines = out.getvalue().splitlines()
    assert "explorer" in lines[0] and "find the parser" in lines[0]
    assert lines[1].startswith("  `-") and "grep" in lines[1]
    assert "explorer done ok" in lines[2]


def test_hostile_subagent_text_is_cleaned():
    r, out = make()
    r.update({"sessionUpdate": "hal_subagent", "subagentId": "1", "agent": HOSTILE, "phase": "start", "task": HOSTILE})
    assert "\x1b" not in out.getvalue() and "\x07" not in out.getvalue()
