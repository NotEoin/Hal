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


class IsoSession:
    """A session with a real isolated copy, so `/accept` runs the real report and apply."""

    def __init__(self, iso):
        self.iso = iso

    def apply_isolated(self, *, allow_flags=False, expected_patch=None):
        from halsandbox import isolated

        return [c.path for c in isolated.accept(self.iso, allow_flags=allow_flags, expected_patch=expected_patch)]


def accept_setup(tmp_path, monkeypatch):
    from halsandbox import isolated

    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.txt").write_text("one\n")
    iso = isolated.create(project, 10**6)
    (iso.work / "a.txt").write_text("two\n")
    return project, iso


def answering(answer, seen=None):
    async def confirm(question):
        if seen is not None:
            seen.append(question)
        return answer

    return confirm


def test_accept_applies_nothing_until_the_user_confirms(tmp_path, monkeypatch):
    # regression: /accept applied the copy at once, without the confirmation the docs promise
    project, iso = accept_setup(tmp_path, monkeypatch)
    out = io.StringIO()
    st = slash.TuiState(NullClient(), "0123456789ab", "/work", Renderer(out), session=IsoSession(iso))
    assert "nothing applied" in run("/accept", st).message
    st.confirm = answering(False)
    assert "nothing applied" in run("/accept", st).message
    assert (project / "a.txt").read_text() == "one\n"
    assert "a.txt" in out.getvalue()
    seen = []
    st.confirm = answering(True, seen)
    assert "applied 1 changed paths" in run("/accept", st).message
    assert (project / "a.txt").read_text() == "two\n" and "[y/N]" in seen[0]


def test_accept_refuses_changes_made_while_the_user_was_deciding(tmp_path, monkeypatch):
    project, iso = accept_setup(tmp_path, monkeypatch)

    async def swap_then_yes(question):
        (iso.work / "a.txt").write_text("swapped\n")
        return True

    st = state(session=IsoSession(iso), confirm=swap_then_yes)
    assert "changed after the review" in run("/accept", st).message
    assert (project / "a.txt").read_text() == "one\n"


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 8


def test_image_inside_the_workspace_is_attached(tmp_path):
    (tmp_path / "a.png").write_bytes(PNG)
    st = state(project=tmp_path)
    assert "attached a.png" in run("/image a.png", st).message
    assert len(st.attachments) == 1


def test_image_outside_the_workspace_is_refused_without_the_flag(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "x.png").write_bytes(PNG)
    st = state(project=ws)
    assert "outside the workspace" in run(f"/image {tmp_path / 'x.png'}", st).message
    assert st.attachments == []


def test_image_symlink_out_of_the_workspace_is_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "x.png").write_bytes(PNG)
    (ws / "link.png").symlink_to(tmp_path / "x.png")
    st = state(project=ws)
    assert "outside the workspace" in run("/image link.png", st).message


def test_image_outside_flag_attaches_with_a_warning(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "x.png").write_bytes(PNG)
    st = state(project=ws)
    msg = run(f"/image --outside {tmp_path / 'x.png'}", st).message
    assert msg.startswith("WARNING:") and len(st.attachments) == 1
