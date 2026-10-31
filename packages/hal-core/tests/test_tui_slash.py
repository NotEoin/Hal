"""Tests for the added slash commands: help table, pickers, cost, context and read-only levels."""

import asyncio
import io

from rich.console import Console

from halcore.tui import slash
from halcore.tui.render import Renderer, TurnStats
from test_tui import NullClient


def state(**kw):
    s = slash.TuiState(NullClient(), "0123456789ab", "/work", Renderer(io.StringIO()), **kw)
    return s


def run(line, st, user=None):
    return asyncio.run(slash.dispatch(line, st, user or {}))


def shown(renderable):
    console = Console(width=100, record=True, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def test_help_table_lists_every_builtin_and_user_command():
    out = shown(run("/help", state(user_commands={"review": "x"})).renderable)
    for name in ("/model", "/accept", "/cost", "/context", "/init", "/theme", "/review"):
        assert name in out


def test_every_builtin_has_a_help_row():
    assert set(slash.BUILTINS) - {"apply"} <= set(slash.COMMAND_HELP)


def test_model_without_arguments_numbers_the_gateway_models_with_context_sizes():
    st = state(list_models=lambda: [{"id": "qwen-30b", "context_length": 32768}, {"id": "gemma-12b"}], model="gemma-12b")
    text = run("/model", st).message
    assert "1  qwen-30b  32k context" in text and "*2  gemma-12b" in text
    run("/model 1", st)
    assert st.model == "qwen-30b" and st.client.models == ["qwen-30b"]


def test_model_picker_survives_a_failing_gateway():
    def boom():
        raise OSError("socket gone")

    text = run("/model", state(list_models=boom, model="m")).message
    assert "model: m" in text and "did not list" in text


def test_cost_sums_turns_and_says_no_money_is_spent():
    st = state()
    t = TurnStats()
    t.out_chars = 4000
    st.turns.append(t)
    result = run("/cost", st)
    assert "local model" in result.message and "1000" in shown(result.renderable)


def test_context_breaks_the_window_down_and_handles_unknown_size():
    st = state()
    assert "unknown" in run("/context", st).message
    st.status.used, st.status.size = 8192, 32768
    text = run("/context", st).message
    assert "25%" in text and "free" in text and "24576" in text


def test_lockdown_and_paranoia_are_read_only():
    st = state()
    st.status.lockdown, st.status.paranoia = "standard", "balanced"
    assert "standard" in run("/lockdown strict", st).message and st.status.lockdown == "standard"
    assert "balanced" in run("/paranoia off", st).message and st.status.paranoia == "balanced"


def test_init_sends_a_prompt_and_quit_still_quits():
    assert "AGENTS.md" in run("/init", state()).prompt
    assert run("/quit", state()).quit and run("/exit", state()).quit


def test_theme_changes_the_look_for_the_session():
    st = state()
    run("/theme mono", st)
    assert not st.renderer.look.color
    assert "pick one" in run("/theme neon", st).message
