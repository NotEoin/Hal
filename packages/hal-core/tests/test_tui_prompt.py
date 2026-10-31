"""Tests for the input editor, completion, @path mentions and the status line."""

import asyncio
import io
import os
import stat

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from halcore.tui import mentions, prompt, status, theme

LOOK = theme.load_look(stream=io.StringIO(), environ={})
COMMANDS = {"help": "list commands", "model": "pick a model", "mode": "change mode"}


def read_keys(keys, hooks=None, root=None):
    async def go():
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            box = prompt.PromptInput(
                LOOK, COMMANDS, lambda: [("", "status")], root=lambda: root, hooks=hooks, input=pipe, output=DummyOutput()
            )
            return await box.read()

    return asyncio.run(go())


def test_enter_sends_the_line():
    assert read_keys("hello there\r") == "hello there"


def test_alt_enter_and_ctrl_j_insert_newlines():
    assert read_keys("a\x1b\rb\nc\r") == "a\nb\nc"


def test_multiline_paste_becomes_a_chip_that_expands_on_send():
    assert read_keys("see \x1b[200~l1\nl2\nl3\x1b[201~\r") == "see l1\nl2\nl3"


def test_short_paste_is_inserted_as_it_is():
    assert read_keys("\x1b[200~one\ntwo\x1b[201~\r") == "one\ntwo"


def test_ctrl_d_on_an_empty_line_ends_input():
    with pytest.raises(EOFError):
        read_keys("\x04")


def test_shift_tab_ctrl_o_and_ctrl_t_call_their_hooks():
    calls = []
    hooks = prompt.Hooks(
        cycle_mode=lambda: calls.append("mode"),
        toggle_verbose=lambda: calls.append("verbose"),
        toggle_todo=lambda: calls.append("todo"),
    )
    read_keys("\x1b[Z\x0f\x14x\r", hooks)
    assert calls == ["mode", "verbose", "todo"]


def test_double_escape_asks_for_rewind():
    assert read_keys("\x1b\x1b") == "/rewind"


def complete(text, root=None):
    completer = prompt.HalCompleter(COMMANDS, lambda: root)
    return list(completer.get_completions(Document(text), CompleteEvent()))


def test_slash_completion_lists_matching_commands_with_descriptions():
    found = complete("/mo")
    assert [c.text for c in found] == ["/mode", "/model"]
    assert found[0].display_meta_text == "change mode"


def test_at_completion_lists_workspace_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    mentions._cache.clear()
    assert [c.text for c in complete("look at @ap", tmp_path)] == ["@src/app.py"]


def test_mentions_attach_files_and_skip_symlinks_and_big_files(tmp_path):
    (tmp_path / "a.py").write_text("print(1)\n")
    (tmp_path / "big.txt").write_text("x" * 50)
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret")
    os.symlink(outside, tmp_path / "link.txt")
    blocks, notes = mentions.attach_mentions("see @a.py @big.txt @link.txt and a@b.c", tmp_path, 20)
    assert len(blocks) == 1 and "print(1)" in blocks[0]["text"] and 'path="a.py"' in blocks[0]["text"]
    assert any("big.txt skipped" in n for n in notes)
    assert "secret" not in str(blocks)


def test_history_file_is_private(tmp_path):
    path = tmp_path / "state" / "history"
    prompt.private_history(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def info(**kw):
    return status.StatusInfo(model="qwen3-coder-30b", used=8192, size=32768, tok_s=41.2, lockdown="standard", **kw)


def test_status_line_shows_the_context_bar_and_the_listed_fields():
    text = status.to_rich(status.segments(info(jobs=2, branch="main", changes=3), LOOK), LOOK).plain
    for part in ("qwen3-coder-30b", "25%", "41 tok/s", "mode default", "standard", "2 jobs", "main", "3 unaccepted"):
        assert part in text
    assert "##" in text and text.isascii()


def test_status_line_flags_unsandboxed_and_a_full_window():
    parts = status.segments(status.StatusInfo(used=31000, size=32768, unsandboxed=True), LOOK)
    assert ("bad", "UNSANDBOXED") in parts and any(r == "bad" and "%" in t for r, t in parts)


def test_context_bar_clamps_and_handles_unknown_size():
    assert status.context_bar(0, 0, LOOK.glyphs) == "-" * 10
    assert status.context_bar(99, 10, LOOK.glyphs) == "#" * 10


def test_turn_keys_are_parsed_from_one_read():
    from halcore.tui import keys

    assert keys.parse_keys(b"\x1b") == ["escape"]
    assert keys.parse_keys(b"\x0f\x14") == ["verbose", "todo"]
    assert keys.parse_keys(b"\x1b[I") == ["focus-in"] and keys.parse_keys(b"\x1b[O") == ["focus-out"]
    assert keys.parse_keys(b"\x1b[A") == [] and keys.parse_keys(b"abc") == []


def test_key_watcher_does_nothing_without_a_terminal(monkeypatch):
    from halcore.tui import keys

    monkeypatch.setattr("sys.stdin", io.StringIO())
    watcher = keys.KeyWatcher(lambda key: None)
    watcher.start()
    watcher.stop()
    with watcher.suspended():
        pass


def test_a_mentioned_fifo_is_skipped_without_blocking(tmp_path):
    # regression: `@pipe` naming a FIFO the box planted hung the TUI on open
    os.mkfifo(tmp_path / "pipe")
    blocks, _ = mentions.attach_mentions("see @pipe", tmp_path, 20)
    assert blocks == []
