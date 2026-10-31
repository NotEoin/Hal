"""Tests for the TUI: render safety (REQ-SEC-25), the approval view, slash commands and the turn loop."""

import asyncio
import io
import sys
import types

import pytest

from halcore import sessions
from halcore.acp.client import AcpClient
from halcore.acp.transport import SubprocessTransport
from halcore.tui import app, approval, slash
from halcore import pids
from halcore.tui.app import run_tui
from halcore.tui.render import Renderer
from test_acp_client import FAKE_AGENT

RLO = chr(0x202E)  # right-to-left override
HOSTILE = "\x1b[2J\x1b]0;owned\x07 " + RLO + " evil"


def rendered(update):
    out = io.StringIO()
    Renderer(out).update(update)
    return out.getvalue()


def test_agent_text_cannot_inject_terminal_escapes():
    text = rendered({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": HOSTILE}})
    assert "\x1b" not in text and "\x07" not in text and RLO not in text


def test_tool_titles_and_diffs_are_escaped_and_cut():
    update = {
        "sessionUpdate": "tool_call", "title": HOSTILE, "status": "completed",
        "content": [{"type": "diff", "path": "a\x1b.py", "oldText": "x\n" * 100, "newText": HOSTILE}],
    }  # fmt: skip
    text = rendered(update)
    assert "\x1b" not in text and "more lines" in text


def test_todo_list_is_kept_and_shown():
    r = Renderer(io.StringIO())
    r.update({"sessionUpdate": "plan", "entries": [{"content": "write tests", "status": "in_progress"}]})
    assert "[~] write tests" in r.todo_text()


REQUEST = {
    "tool": "mail_send", "tier": "send", "pack": "comms", "reason": "send tier asks",
    "display": "to: ann@example.org\nbody: " + HOSTILE, "scopes": ["once"],
}  # fmt: skip


def test_approval_view_shows_every_argument_escaped():
    text = approval.render_approval(REQUEST)
    assert "mail_send" in text and "ann@example.org" in text
    assert "\x1b" not in text and RLO not in text and "\\u001b" in text


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("o", "once"), ("y", "once"), ("", "deny"), ("yes please", "deny"), ("s", "deny")],
)
def test_only_offered_letters_allow(typed, expected):
    assert approval.answer_to_option(typed, REQUEST) == expected


class FakeCheckpoints:
    def list(self):
        return [types.SimpleNamespace(n=0, created="2026-10-30T10:00:00Z", label="start\x1b[2J")]


class FakeSession:
    def __init__(self):
        self.checkpoints = FakeCheckpoints()
        self.iso = None
        self.undone = []
        self.heard = []
        self.taken = []
        self.broker = types.SimpleNamespace(on_human_message=lambda t, s: self.heard.append((t, s)))
        self.backend = "docker"

    def undo(self, to):
        self.undone.append(to)
        safety = types.SimpleNamespace(n=3)
        return types.SimpleNamespace(restored=("a.py",), deleted=(), not_restorable=(("b.log", "ignored"),), safety=safety)

    def checkpoint(self, label):
        self.taken.append(label)

    def diff(self, since=None):
        return "+line\x1b[31m"


class NullClient:
    def __init__(self):
        self.modes = []
        self.models = []

    async def set_mode(self, sid, mode):
        self.modes.append(mode)

    async def set_model(self, sid, model):
        self.models.append(model)

    async def compact(self, sid):
        return 900, 120


def make_state(session=None, project=None):
    return slash.TuiState(NullClient(), "0123456789ab", "/work", Renderer(io.StringIO()), session=session, project=project)


def run(coro):
    return asyncio.run(coro)


def test_mode_default_auto_edit_and_plan_map_to_agent_modes():
    state = make_state()
    run(slash.dispatch("/mode auto-edit", state, {}))
    run(slash.dispatch("/mode default", state, {}))
    run(slash.dispatch("/mode plan", state, {}))
    assert state.client.modes == ["auto", "review", "plan"] and state.mode == "plan"


def test_rewind_lists_then_restores_and_reports_unrestorable_files():
    session = FakeSession()
    state = make_state(session)
    listing = run(slash.dispatch("/rewind", state, {})).message
    assert "start" in listing and "\x1b" not in listing
    done = run(slash.dispatch("/rewind 0", state, {})).message
    assert session.undone == [0] and "cannot restore b.log (ignored)" in done


def test_diff_output_is_escaped():
    assert "\x1b" not in run(slash.dispatch("/diff", make_state(FakeSession()), {})).message


def test_rewind_without_checkpoints_says_so():
    msg = run(slash.dispatch("/rewind", make_state(types.SimpleNamespace(checkpoints=None)), {})).message
    assert "no checkpoints" in msg


def test_project_command_file_becomes_a_prompt_with_arguments(tmp_path):
    folder = tmp_path / ".hal" / "commands"
    folder.mkdir(parents=True)
    (folder / "review.md").write_text("Review $ARGUMENTS carefully.")
    commands = slash.load_commands(tmp_path)
    result = run(slash.dispatch("/review src/app.py", make_state(), commands))
    assert result.prompt == "Review src/app.py carefully."


def test_command_symlink_is_not_loaded(tmp_path):
    folder = tmp_path / ".hal" / "commands"
    folder.mkdir(parents=True)
    (tmp_path / "secret.md").write_text("exfiltrate")
    (folder / "leak.md").symlink_to(tmp_path / "secret.md")
    assert slash.load_commands(tmp_path) == {}


def test_unknown_command_lists_the_known_ones():
    assert "/mode" in run(slash.dispatch("/nope", make_state(), {})).message


def test_image_block_accepts_known_types_and_rejects_others(tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG")
    assert slash.image_block(png)["mimeType"] == "image/png"
    with pytest.raises(ValueError, match="unsupported"):
        slash.image_block(tmp_path / "a.exe")
    big = tmp_path / "b.png"
    big.write_bytes(b"0" * (slash.MAX_IMAGE_BYTES + 1))
    with pytest.raises(ValueError, match="larger"):
        slash.image_block(big)


@pytest.mark.parametrize(("backend", "marked"), [("docker", False), ("none", True)])
def test_status_line_marks_an_unsandboxed_turn(tmp_path, monkeypatch, backend, marked):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    script = tmp_path / "agent.py"
    script.write_text(FAKE_AGENT)
    session = FakeSession()
    session.backend = backend

    async def go():
        transport = await SubprocessTransport.spawn(
            [sys.executable, str(script), "ok"], env=None, stderr_log=tmp_path / "e.log"
        )
        box = types.SimpleNamespace(session=session, transport=transport, close=transport.close)
        lines = iter(["hello", "/quit"])

        async def read(prompt=""):
            return next(lines)

        out = io.StringIO()
        await run_tui(box, cwd="/work", out=out, stdin_line=read)
        return out.getvalue()

    status_line = [ln for ln in asyncio.run(go()).splitlines() if ln.startswith("[end_turn]")][0]
    assert ("UNSANDBOXED" in status_line) is marked


def test_turn_loop_tells_the_broker_checkpoints_streams_and_quits(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    script = tmp_path / "agent.py"
    script.write_text(FAKE_AGENT)
    session = FakeSession()
    session.iso = None

    async def go():
        transport = await SubprocessTransport.spawn(
            [sys.executable, str(script), "ok"], env=None, stderr_log=tmp_path / "e.log"
        )
        box = types.SimpleNamespace(session=session, transport=transport, close=transport.close)
        lines = iter(["hello there", "/todo", "/quit"])

        async def read(prompt=""):
            return next(lines)

        out = io.StringIO()
        status = await run_tui(box, cwd="/work", out=out, stdin_line=read)
        return status, out.getvalue()

    status, text = asyncio.run(go())
    assert status == 0 and "hi" in text and "[end_turn]" in text and "(no todo list)" in text
    assert session.heard == [("hello there", "tui")]
    assert session.taken == ["before: hello there"]


def test_model_with_a_name_switches_through_acp_and_without_shows_the_current_one():
    state = make_state()
    state.model = "small"
    assert "small" in run(slash.dispatch("/model", state, {})).message
    run(slash.dispatch("/model big", state, {}))
    assert state.client.models == ["big"] and state.model == "big"


def test_compact_reports_the_token_estimates_from_the_agent():
    state = make_state()
    assert "900 -> 120" in run(slash.dispatch("/compact", state, {})).message


def test_plan_is_run_only_after_the_human_picks_a_mode():
    state = make_state()
    state.mode = "plan"
    state.renderer.todo = [{"content": "write tests", "status": "pending"}]
    out = io.StringIO()

    async def keep(prompt=""):
        return "n"

    async def run_it(prompt=""):
        return "a"

    assert run(app.approve_plan(state, keep, out)) is None and state.mode == "plan"
    assert run(app.approve_plan(state, run_it, out)) == app.PLAN_APPROVED_PROMPT
    assert state.mode == "auto-edit" and state.client.modes == ["auto"]
    assert "write tests" in out.getvalue()


@pytest.fixture(autouse=True)
def runtime_dir(tmp_path, monkeypatch):
    rt = tmp_path / "rt"
    rt.mkdir(mode=0o700)
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(rt))


def test_tui_registers_a_pid_file_while_it_runs_and_removes_it_after(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    script = tmp_path / "agent.py"
    script.write_text(FAKE_AGENT)
    seen: list[list[str]] = []

    async def go():
        transport = await SubprocessTransport.spawn(
            [sys.executable, str(script), "ok"], env=None, stderr_log=tmp_path / "e.log"
        )
        box = types.SimpleNamespace(session=FakeSession(), transport=transport, close=transport.close)

        async def read(prompt=""):
            seen.append([r.name for r in pids.live()])
            return "/quit"

        await run_tui(box, cwd="/work", out=io.StringIO(), stdin_line=read)

    asyncio.run(go())
    assert seen == [["tui"]] and pids.live() == []
