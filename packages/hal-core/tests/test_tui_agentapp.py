"""Pilot tests for the full-screen `hal code` and `hal chat` with an in-process scripted ACP agent.

No box, no gateway, no GPU: the agent is `ScriptedAgent`, a transport that answers the session
methods and streams scripted updates; the session is a fake with checkpoints.
"""

from __future__ import annotations

import asyncio
import io
import time
import types

import pytest
from textual import events
from textual.widgets import Button

from halcore.acp.records import RecordStore
from halcore.tui import agentapp, screens
from halcore.tui.agentapp import AgentApp, Composer, UserMessage
from halcore.tui.approvalmodal import ApprovalModal
from halcore.tui.controller import Controller
from halcore.tui.render import Renderer
from halcore.tui.theme import load_look
from halcore.tui.widgets import SafeStatic, ToolCard

SID = "0123456789ab"
LOOK = load_look({"unicode": False, "animations": False, "notify": "off"}, stream=io.StringIO(), environ={})
SIZE = (120, 36)
WAIT_CANCEL = "wait-cancel"


def chunk(text):
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


class ScriptedAgent:
    """An in-process ACP agent: answers session methods and streams one script per prompt."""

    def __init__(self, turns=()):
        self.inbox, self.sent, self.calls = asyncio.Queue(), [], []
        self.turns = list(turns)
        self.prompts = []
        self.cancelled = None

    async def send(self, msg):
        self.sent.append(msg)
        method, rid = msg.get("method"), msg.get("id")
        if method == "session/prompt":
            self.prompts.append(msg["params"]["prompt"])
            asyncio.ensure_future(self._turn(rid))
        elif method == "session/cancel":
            if self.cancelled is not None:
                self.cancelled.set()
        elif method:
            self.calls.append((method, msg.get("params")))
            result = {"initialize": {"protocolVersion": 1}, "session/new": {"sessionId": SID}}.get(method, {})
            await self.inbox.put({"jsonrpc": "2.0", "id": rid, "result": result})

    async def _turn(self, rid):
        script = self.turns.pop(0) if self.turns else [chunk("hi")]
        for step in script:
            if step == WAIT_CANCEL:
                self.cancelled = asyncio.Event()
                await self.cancelled.wait()
                await self.inbox.put({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
                return
            if isinstance(step, float):
                await asyncio.sleep(step)
                continue
            await self.inbox.put({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": SID, "update": step}})
        await self.inbox.put({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})

    async def receive(self):
        return await self.inbox.get()

    async def close(self, timeout=5.0):
        pass


class Checkpoints:
    def __init__(self):
        self.items = [types.SimpleNamespace(n=0, created="2026-11-03T10:00:00Z", label="start")]

    def list(self):
        return list(self.items)


class FakeSession:
    def __init__(self, backend="docker", work=None, iso=None):
        self.checkpoints = Checkpoints()
        self.iso = iso
        self.work = work
        self.backend = backend
        self.runtime = "runsc"
        self.heard, self.undone, self.applied = [], [], []
        self.broker = types.SimpleNamespace(on_human_message=lambda t, s: self.heard.append(t))

    def checkpoint(self, label):
        self.checkpoints.items.append(types.SimpleNamespace(n=len(self.checkpoints.items), created="t", label=label))

    def undo(self, to):
        self.undone.append(to)
        return types.SimpleNamespace(restored=(), deleted=(), not_restorable=(), safety=types.SimpleNamespace(n=9))

    def apply_isolated(self, *, allow_flags, expected_patch):
        self.applied.append((allow_flags, expected_patch))
        return ["a.txt"]


@pytest.fixture(autouse=True)
def private_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    rt = tmp_path / "rt"
    rt.mkdir(mode=0o700)
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(rt))


def drive(tmp_path, script, *, turns=(), session=None, mode="code", size=SIZE, project=None, setup=None):
    """Starts a controller on `ScriptedAgent`, runs the app and `script(pilot, app, agent)`; returns its result."""
    agent = ScriptedAgent(turns)
    session = session or FakeSession()
    box = types.SimpleNamespace(session=session, transport=agent)

    async def go():
        controller = Controller(box, Renderer(io.StringIO(), LOOK), cwd="/work", project=project,
                                store=RecordStore(tmp_path / "sessions"), mode=mode, live=False)
        await controller.start()
        if setup is not None:
            setup(controller)
        app = AgentApp(controller, LOOK)
        controller.renderer.sink = app.sink
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            return await script(pilot, app, agent)

    return asyncio.run(go())


async def settle(pilot, n=6):
    for _ in range(n):
        await pilot.pause()
    await asyncio.sleep(0.02)
    await pilot.pause()


async def type_text(pilot, text):
    await pilot.press(*text)


def plain(widget):
    """What a `SafeStatic` shows, as plain text (markdown rendered the way the screen draws it)."""
    from rich.console import Console

    console = Console(width=200, record=True, file=io.StringIO(), color_system=None)
    console.print(widget.content)
    return console.export_text()


def transcript_text(app):
    return "\n".join(plain(w) for w in app.transcript.query(SafeStatic))


async def until(pilot, check, tries=60):
    for _ in range(tries):
        if check():
            return True
        await asyncio.sleep(0.02)
        await pilot.pause()
    return check()


def test_enter_sends_and_alt_enter_inserts_newline(tmp_path):
    async def script(pilot, app, agent):
        await type_text(pilot, "fix")
        await pilot.press("ctrl+j")
        await type_text(pilot, "it")
        assert app.query_one(Composer).text == "fix\nit"
        await pilot.press("enter")
        assert await until(pilot, lambda: "hi" in transcript_text(app))
        return agent.prompts, app.query_one(Composer).text

    prompts, left = drive(tmp_path, script)
    assert prompts[0][0]["text"] == "fix\nit" and left == ""


def test_multiline_paste_becomes_one_chip_and_backspace_removes_it(tmp_path):
    async def script(pilot, app, agent):
        composer = app.query_one(Composer)
        composer.post_message(events.Paste("a\nb\nc\nd"))
        await settle(pilot)
        chip_text = composer.text
        await pilot.press("backspace")
        after_backspace = composer.text
        composer.post_message(events.Paste("1\n2\n3"))
        await settle(pilot)
        await pilot.press("enter")
        assert await until(pilot, lambda: agent.prompts)
        return chip_text, after_backspace, agent.prompts[0][0]["text"]

    chip, after, sent = drive(tmp_path, script)
    assert chip == "[pasted 4 lines #1]" and after == "" and sent == "1\n2\n3"


def test_slash_popup_completes_commands_and_arguments(tmp_path):
    async def script(pilot, app, agent):
        await type_text(pilot, "/mo")
        shown = [o.prompt.plain.split()[0] for o in app.popup._options]
        await pilot.press("down", "tab")
        await type_text(pilot, "pl")
        await pilot.press("tab")
        return shown, app.query_one(Composer).text

    shown, text = drive(tmp_path, script)
    assert shown == ["/model", "/mode"] and text == "/mode plan"


def test_at_popup_completes_workspace_files_in_code_only(tmp_path):
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "parse.py").write_text("x = 1\n")

    async def script(pilot, app, agent):
        await type_text(pilot, "look at @pars")
        shown = [o.prompt.plain for o in app.popup._options]
        await pilot.press("tab")
        return shown, app.query_one(Composer).text

    shown, text = drive(tmp_path, script, session=FakeSession(work=work))
    assert shown == ["@src/parse.py"] and text == "look at @src/parse.py "

    async def chat(pilot, app, agent):
        await type_text(pilot, "@pars")
        return app.popup.display

    assert drive(tmp_path, chat, session=FakeSession(work=work), mode="chat") is False


def test_streaming_text_lands_as_cleaned_markdown_and_markup_stays_literal(tmp_path):
    hostile = "[@click=app.quit]press[/] \x1b[2J" + chr(0x202E) + " done\n\n"

    async def script(pilot, app, agent):
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: "press" in transcript_text(app))
        for widget in app.transcript.query(SafeStatic):
            if "press" in plain(widget):
                await pilot.click(widget)
        await settle(pilot)
        return transcript_text(app), app.is_running

    text, running = drive(tmp_path, script, turns=[[chunk(hostile)]])
    assert running and "[@click=app.quit]" in text and "\x1b" not in text and chr(0x202E) not in text


def test_long_running_tool_card_is_visible_before_it_finishes(tmp_path):
    turn = [{"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Run pytest -q", "status": "in_progress"}, 0.3,
            {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed",
             "content": [{"type": "content", "content": {"type": "text", "text": "3 passed"}}]}]

    async def script(pilot, app, agent):
        await type_text(pilot, "test")
        await pilot.press("enter")
        assert await until(pilot, lambda: app.query(ToolCard))
        card = app.query_one(ToolCard)
        running = card.has_class("running") and "running" in str(card.query_one(".header").render())
        assert await until(pilot, lambda: not card.has_class("running"))
        return running, str(card.query_one(".header").render())

    running, done = drive(tmp_path, script, turns=[turn])
    assert running and "Run pytest -q" in done and "ok" in done


def test_esc_stops_a_running_turn(tmp_path):
    async def script(pilot, app, agent):
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: app.busy)
        await pilot.press("escape")
        assert await until(pilot, lambda: not app.busy)
        return transcript_text(app)

    text = drive(tmp_path, script, turns=[[WAIT_CANCEL]])
    assert "cancelled" in text


def test_ctrl_c_clears_the_composer_then_stops_a_running_turn(tmp_path):
    async def script(pilot, app, agent):
        await type_text(pilot, "draft")
        await pilot.press("ctrl+c")
        cleared = app.query_one(Composer).text == ""
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: app.busy)
        await pilot.press("ctrl+c")
        assert await until(pilot, lambda: not app.busy)
        return cleared, app.is_running

    assert drive(tmp_path, script, turns=[[WAIT_CANCEL]]) == (True, True)


def test_ctrl_c_on_empty_idle_composer_needs_a_second_press_to_quit(tmp_path):
    async def script(pilot, app, agent):
        await pilot.press("ctrl+c")
        await settle(pilot)
        first = app.is_running
        await pilot.press("ctrl+c")
        await until(pilot, lambda: not app.is_running)
        return first, app.is_running

    assert drive(tmp_path, script) == (True, False)


def test_model_chip_click_opens_picker_and_switches_model(tmp_path):
    def setup(controller):
        controller.state.list_models = lambda: [{"id": "qwen3:8b"}, {"id": "gemma3:12b", "context_length": 32768}]

    async def script(pilot, app, agent):
        await pilot.click("#chip-model")
        assert await until(pilot, lambda: isinstance(app.screen, screens.PickerModal))
        await type_text(pilot, "gemma")
        await pilot.press("enter")
        assert await until(pilot, lambda: ("session/set_model", {"sessionId": SID, "modelId": "gemma3:12b"}) in agent.calls)
        await settle(pilot)
        return str(app.query_one("#chip-model", Button).label)

    label = drive(tmp_path, script, setup=setup)
    assert "gemma3:12b" in label


def test_shift_tab_cycles_mode_and_status_updates(tmp_path):
    async def script(pilot, app, agent):
        await pilot.press("shift+tab")
        await settle(pilot)
        return app.state.mode, str(app.query_one("#status").render()), str(app.query_one("#chip-mode", Button).label)

    mode, status, chip = drive(tmp_path, script)
    assert mode == "auto-edit" and "mode auto-edit" in status and chip == "mode: auto-edit"


def test_plan_card_keeps_planning_by_default_and_runs_only_on_button(tmp_path):
    plan = {"sessionUpdate": "plan", "entries": [{"content": "write the test", "status": "pending"}]}

    def setup(controller):
        controller.state.mode = controller.state.status.mode = "plan"

    async def script(pilot, app, agent):
        await type_text(pilot, "plan it")
        await pilot.press("enter")
        assert await until(pilot, lambda: isinstance(app.screen, screens.ChoiceModal))
        assert app.screen.focused.id == "choice-keep"
        await pilot.press("enter")
        assert await until(pilot, lambda: not app.busy and not isinstance(app.screen, screens.ChoiceModal))
        kept = (app.state.mode, len(agent.prompts))
        await type_text(pilot, "again")
        await pilot.press("enter")
        assert await until(pilot, lambda: isinstance(app.screen, screens.ChoiceModal))
        await pilot.click("#choice-default")
        assert await until(pilot, lambda: len(agent.prompts) == 3)
        return kept, app.state.mode, agent.prompts[-1][0]["text"]

    kept, mode, follow_up = drive(tmp_path, script, turns=[[plan], [plan]], setup=setup)
    assert kept == ("plan", 1) and mode == "default" and follow_up == "The plan is approved. Carry it out."


def isolated_session(tmp_path, changed=True):
    from halsandbox import isolated

    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.txt").write_text("one\n")
    iso = isolated.create(project, 10**6)
    if changed:
        (iso.work / "a.txt").write_text("two\n")
    return FakeSession(iso=iso), project


def test_accept_modal_applies_exactly_the_reviewed_patch(tmp_path):
    from halsandbox import isolated

    session, project = isolated_session(tmp_path)

    async def script(pilot, app, agent):
        await type_text(pilot, "/accept")
        await pilot.press("enter")
        assert await until(pilot, lambda: isinstance(app.screen, agentapp.AcceptModal))
        modal = app.screen
        assert modal.focused.id == "cancel" and not modal.query("#allow-flags")
        patch_shown = "\n".join(str(w.render()) for w in modal.query("#patch SafeStatic"))
        await pilot.click("#apply")
        assert await until(pilot, lambda: session.applied)
        return patch_shown

    shown = drive(tmp_path, script, session=session, project=project)
    allow_flags, expected = session.applied[0]
    assert allow_flags is False and expected == isolated.patch_bytes(session.iso) and "+two" in shown


def test_accept_modal_cancel_applies_nothing(tmp_path):
    session, project = isolated_session(tmp_path)

    async def script(pilot, app, agent):
        await type_text(pilot, "/accept")
        await pilot.press("enter")
        assert await until(pilot, lambda: isinstance(app.screen, agentapp.AcceptModal))
        await pilot.press("enter")  # Enter on the focused Cancel
        await settle(pilot)
        return transcript_text(app)

    text = drive(tmp_path, script, session=session, project=project)
    assert session.applied == [] and "nothing applied" in text


def test_exit_with_unaccepted_changes_offers_keep_review_discard(tmp_path):
    session, project = isolated_session(tmp_path)

    async def script(pilot, app, agent):
        await pilot.press("ctrl+d")
        assert await until(pilot, lambda: isinstance(app.screen, screens.ChoiceModal))
        ids = [b.id for b in app.screen.query(Button)]
        focused = app.screen.focused.id
        await pilot.press("enter")
        await until(pilot, lambda: not app.is_running)
        return ids, focused, app.is_running

    ids, focused, running = drive(tmp_path, script, session=session, project=project)
    assert ids == ["choice-keep", "choice-review", "choice-discard", "choice-cancel"]
    assert focused == "choice-keep" and not running and (session.iso.work / "a.txt").read_text() == "two\n"


def test_exit_without_changes_quits_at_once(tmp_path):
    session, project = isolated_session(tmp_path, changed=False)

    async def script(pilot, app, agent):
        await pilot.press("ctrl+d")
        await until(pilot, lambda: not app.is_running)
        return app.is_running

    assert drive(tmp_path, script, session=session, project=project) is False


def test_chat_surface_hides_code_only_commands_and_hints(tmp_path):
    async def script(pilot, app, agent):
        await type_text(pilot, "/")
        shown = {o.prompt.plain.split()[0] for o in app.popup._options}
        return shown, bool(app.query("#chip-mode")), bool(app.query("#side-changes"))

    shown, has_mode, has_changes = drive(tmp_path, script, mode="chat")
    assert "/model" in shown and not shown & {"/diff", "/accept", "/rewind", "/export", "/init", "/ide"}
    assert not has_mode and not has_changes


def test_theme_switch_applies_live(tmp_path):
    async def script(pilot, app, agent):
        await type_text(pilot, "/theme light")
        await pilot.press("enter")
        assert await until(pilot, lambda: app.theme == "hal-light")
        return app.theme

    assert drive(tmp_path, script) == "hal-light"


def test_unsandboxed_banner_and_status_on_every_redraw(tmp_path):
    async def script(pilot, app, agent):
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: "hi" in transcript_text(app) and not app.busy)
        return app.query_one("#unsandboxed").display, str(app.query_one("#status").render()), transcript_text(app)

    banner, status, text = drive(tmp_path, script, session=FakeSession(backend="none"))
    assert banner and "UNSANDBOXED" in status and "UNSANDBOXED" in text.strip().splitlines()[-1]


def test_message_click_offers_rewind_to_its_checkpoint(tmp_path):
    session = FakeSession()

    async def script(pilot, app, agent):
        await type_text(pilot, "change things")
        await pilot.press("enter")
        assert await until(pilot, lambda: not app.busy and "hi" in transcript_text(app))
        message = app.transcript.query_one(UserMessage)
        await pilot.click(message)
        assert await until(pilot, lambda: isinstance(app.screen, screens.ChoiceModal))
        await pilot.click("#choice-rewind")
        assert await until(pilot, lambda: session.undone)
        return message.checkpoint

    checkpoint = drive(tmp_path, script, session=session)
    assert checkpoint == 1 and session.undone == [1]


def test_approval_request_opens_the_modal_and_ctrl_c_denies(tmp_path):
    from test_tui_approval_modal import REQUEST, FakeBridge

    bridge = FakeBridge({"hal-1": REQUEST})

    async def script(pilot, app, agent):
        send = app.ui.approval_sender(bridge, lambda note: None)
        await send({"id": "hal-1"})
        assert await until(pilot, lambda: isinstance(app.screen, ApprovalModal))
        app.refresh_status()
        waiting = "approval waiting" in str(app.query_one("#status").render())
        await pilot.press("ctrl+c")
        assert await until(pilot, lambda: bridge.answers)
        return waiting

    assert drive(tmp_path, script) is True
    assert bridge.answers == [("hal-1", "deny")]


def test_layout_fits_80_by_24_without_horizontal_scroll(tmp_path):
    async def script(pilot, app, agent):
        composer = app.query_one(Composer)
        return app.transcript.region.height, composer.region.right, app.query_one("#side").display

    height, right, side = drive(tmp_path, script, size=(80, 24))
    assert height >= 12 and right <= 80 and not side


def test_page_down_in_an_approval_scrolls_its_arguments_not_the_transcript(tmp_path):
    # regression: the app's PgUp/PgDn and Shift+Tab bindings took the keys away from open dialogs
    from test_tui_approval_modal import REQUEST, FakeBridge

    long = {**REQUEST, "display": "\n".join(f"arg{i}: value" for i in range(80))}
    bridge = FakeBridge({"hal-1": long})

    async def script(pilot, app, agent):
        send = app.ui.approval_sender(bridge, lambda note: None)
        await send({"id": "hal-1"})
        assert await until(pilot, lambda: isinstance(app.screen, ApprovalModal))
        modal = app.screen
        mode = app.state.mode
        await pilot.press("shift+tab")
        await settle(pilot)
        for _ in range(12):
            await pilot.press("pagedown")
        await settle(pilot)
        return modal.seen, app.state.mode == mode

    seen, mode_kept = drive(tmp_path, script)
    assert seen and mode_kept


def test_header_chips_stay_on_screen_with_a_long_model_label(tmp_path):
    # regression: at 80x24 a real model label pushed the effort and lockdown chips off screen
    def setup(controller):
        controller.state.status.model = "ollama/qwen3-coder:30b-a3b-instruct (q4_K_M)"
        controller.state.status.lockdown, controller.state.status.paranoia = "strict", "high"

    for size in ((140, 36), (80, 24), (60, 20)):

        async def script(pilot, app, agent):
            app.refresh_status()
            await settle(pilot)
            chips = [b for b in app.query("#header Button") if b.display]
            return [(b.id, b.region.right) for b in chips], app.query_one("#title").region.width

        chips, title = drive(tmp_path, script, size=size, setup=setup)
        assert all(right <= size[0] for _, right in chips), (size, chips)
        assert {"chip-model", "chip-mode", "chip-effort"} <= {c for c, _ in chips} and title >= 10, (size, chips)
        if size[0] >= 140:
            assert "chip-lockdown" in {c for c, _ in chips}


def test_esc_or_ctrl_c_on_the_leave_question_stays_in_the_session(tmp_path):
    # regression: Esc answered "Keep for later" and Ctrl+C fell through, so both quit hal code
    session, project = isolated_session(tmp_path)

    async def script(pilot, app, agent):
        results = []
        for key in ("escape", "ctrl+c"):
            await pilot.press("ctrl+d")
            assert await until(pilot, lambda: isinstance(app.screen, screens.ChoiceModal))
            await pilot.press(key)
            await settle(pilot)
            results.append(app.is_running and not isinstance(app.screen, screens.ChoiceModal))
        return results

    assert drive(tmp_path, script, session=session, project=project) == [True, True]


def test_a_recalled_message_with_a_paste_chip_sends_the_pasted_text_again(tmp_path):
    # regression: Up brought back the chip without its text, so the agent got "[pasted 3 lines #1]"
    async def script(pilot, app, agent):
        composer = app.query_one(Composer)
        composer.post_message(events.Paste("1\n2\n3"))
        await settle(pilot)
        await pilot.press("enter")
        assert await until(pilot, lambda: len(agent.prompts) == 1 and not app.busy)
        await settle(pilot)
        await pilot.press("up")
        recalled = composer.text
        await pilot.press("enter")
        assert await until(pilot, lambda: len(agent.prompts) == 2)
        stored = list(app.history_store.load_history_strings())
        return recalled, agent.prompts[1][0]["text"], stored[0]

    recalled, sent, stored = drive(tmp_path, script)
    assert recalled.startswith("[pasted 3 lines") and sent == "1\n2\n3" and stored == recalled


def test_ctrl_o_expands_everything_when_one_card_is_open_then_folds_everything(tmp_path):
    # regression: Ctrl+O flipped each card, so an opened card folded while the others opened
    from rich.text import Text

    async def script(pilot, app, agent):
        cards = [ToolCard(f"c{i}", Text(f"tool {i}")) for i in range(2)]
        for card in cards:
            app.transcript.add(card)
        await settle(pilot)
        for card in cards:
            card.finish(Text("done"), [Text("one")], [Text("one"), Text("two")])
        await settle(pilot)
        cards[0].action_toggle()
        states = []
        for _ in range(2):
            await pilot.press("ctrl+o")
            await settle(pilot)
            states.append([c.body.expanded for c in cards])
        return states

    assert drive(tmp_path, script) == [[True, True], [False, False]]


def test_typing_after_clicking_a_chip_or_a_card_goes_into_the_message_box(tmp_path):
    # regression: a click left the focus on the chip or card, so the next keys typed went nowhere
    from rich.text import Text

    async def script(pilot, app, agent):
        composer = app.query_one(Composer)
        await pilot.click("#chip-lockdown")
        await settle(pilot)
        await type_text(pilot, "hi")
        after_chip = composer.text
        composer.clear()
        card = ToolCard("c1", Text("tool"))
        app.transcript.add(card)
        await settle(pilot)
        card.focus()
        await settle(pilot)
        await type_text(pilot, "yo")
        return after_chip, composer.text, composer.has_focus

    assert drive(tmp_path, script) == ("hi", "yo", True)


def test_accept_buttons_and_high_risk_box_stay_on_screen_at_80_by_24_and_60_by_20(tmp_path):
    # regression: a long patch pushed Apply, Cancel and the high-risk checkbox below the screen
    from halcore.tui.agentapp import AcceptModal

    patch = "\n".join(f"+line {i}" for i in range(80))
    for size in ((80, 24), (60, 20)):

        async def script(pilot, app, agent, size=size):
            app.push_screen(AcceptModal("report\n" * 20, patch, True))
            await settle(pilot)
            widgets = list(app.screen.query("Button, Checkbox"))
            return [w.id for w in widgets if w.region.bottom > size[1] or w.region.right > size[0] or not w.region.height]

        assert drive(tmp_path, script, size=size) == [], size


def test_side_panel_closes_when_the_terminal_narrows_until_ctrl_b_sets_it(tmp_path):
    # regression: a panel opened on a wide terminal stayed open after a resize to 60 columns
    async def script(pilot, app, agent):
        side = app.query_one("#side")
        states = [side.display]
        await pilot.resize_terminal(60, 20)
        await settle(pilot)
        states.append(side.display)
        await pilot.press("ctrl+b")
        await pilot.resize_terminal(130, 36)
        await settle(pilot)
        await pilot.resize_terminal(70, 24)
        await settle(pilot)
        states.append(side.display)
        return states

    assert drive(tmp_path, script, size=(130, 36)) == [True, False, True]


def test_notify_setting_picks_osc9_bell_or_nothing():
    for mode, wanted in (("osc9", [("write", agentapp.OSC9)]), ("bell", [("bell",)]), ("off", [])):
        calls = []
        fake = types.SimpleNamespace(
            look=types.SimpleNamespace(notify=mode), is_headless=False,
            _driver=types.SimpleNamespace(write=lambda text, calls=calls: calls.append(("write", text))),
            bell=lambda calls=calls: calls.append(("bell",)),
        )  # fmt: skip
        AgentApp.notify_turn(fake)
        assert calls == wanted, mode


def test_a_very_long_turn_notifies_even_when_the_terminal_never_reports_focus():
    # regression: without focus reports the full screen never signalled the end of a long turn
    from halcore.tui.render import TurnStats

    signalled = []
    app = types.SimpleNamespace(
        look=LOOK, window_focused=True, notify_turn=lambda: signalled.append(True), set_busy=lambda busy: None,
        controller=types.SimpleNamespace(session=types.SimpleNamespace(backend="docker"),
                                         renderer=types.SimpleNamespace(sink=types.SimpleNamespace(write=lambda line: None))),
    )  # fmt: skip
    ui = agentapp.TextualUi(app)
    for elapsed in (30.0, agentapp.NOTIFY_LONG_S):
        stats = TurnStats(started=time.monotonic() - elapsed)
        ui.turn_ended(stats, "end_turn")
    assert signalled == [True]


def test_enter_on_a_command_that_is_also_a_prefix_runs_it(tmp_path):
    # regression: "/mode" listed /model first, so Enter completed it to "/model " instead of sending it
    async def script(pilot, app, agent):
        await type_text(pilot, "/mode")
        await settle(pilot)
        first = app.popup._options[0].prompt.plain.split()[0]
        await pilot.press("enter")
        opened = await until(pilot, lambda: isinstance(app.screen, screens.PickerModal))
        return first, opened, app.screen.title_text if opened else app.query_one(Composer).text

    first, opened, shown = drive(tmp_path, script)
    assert first == "/mode" and opened, shown


def test_scrolled_up_transcript_counts_new_output_and_a_click_follows_it_again(tmp_path):
    async def script(pilot, app, agent):
        for i in range(60):
            app.transcript.add(SafeStatic(f"line {i}"))
        await settle(pilot)
        app.transcript.scroll_home(animate=False)
        await settle(pilot)
        app.transcript.add(SafeStatic("late 1"))
        app.transcript.add(SafeStatic("late 2"))
        await settle(pilot)
        below = app.query_one("#below")
        shown = below.display and "2 new below" in plain(below)
        still_up = app.transcript.scroll_y == 0
        await pilot.click("#below")
        await settle(pilot)
        return shown, still_up, app.transcript.scroll_y >= app.transcript.max_scroll_y - 1, below.display

    assert drive(tmp_path, script) == (True, True, True, False)


def test_ctrl_q_asks_about_unaccepted_changes_like_ctrl_d(tmp_path):
    # regression: Textual's built-in Ctrl+Q quit at once, skipping the leave question
    session, project = isolated_session(tmp_path)

    async def script(pilot, app, agent):
        await pilot.press("ctrl+q")
        asked = await until(pilot, lambda: isinstance(app.screen, screens.ChoiceModal))
        await pilot.press("escape")
        await settle(pilot)
        return asked, app.is_running

    assert drive(tmp_path, script, session=session, project=project) == (True, True)


def test_a_one_line_paste_is_inserted_once(tmp_path):
    # regression: the composer called `TextArea._on_paste` and Textual called it again, so it pasted twice
    async def script(pilot, app, agent):
        app.post_message(events.Paste("https://example.invalid/a"))  # the way the driver delivers a paste
        await settle(pilot)
        return app.query_one(Composer).text

    assert drive(tmp_path, script) == "https://example.invalid/a"


@pytest.mark.parametrize(("mode", "title"), [("code", "Hal code"), ("chat", "Hal chat")])
def test_window_title_names_the_surface(tmp_path, mode, title):
    """Regression: the terminal title read "AgentApp", the class name."""

    async def script(pilot, app, agent):
        return app.title

    assert drive(tmp_path, script, mode=mode) == title


def test_no_timer_runs_while_idle(tmp_path):
    running = {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Run sleep 1", "status": "in_progress"}
    done = {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"}

    async def script(pilot, app, agent):
        idle_before = (app.motion.running, app.motion.active)
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: app.motion.active >= 1)
        during = app.motion.running
        assert await until(pilot, lambda: not app.busy and "hi" in transcript_text(app))
        await settle(pilot)
        return idle_before, during, (app.motion.running, app.motion.active)

    before, during, after = drive(tmp_path, script, turns=[[running, 0.3, done, chunk("hi")]])
    assert before == (False, 0) and during and after == (False, 0)


def test_eye_follows_turns_and_approvals(tmp_path):
    from test_tui_approval_modal import REQUEST, FakeBridge

    from halcore.tui.chrome import HalEye

    async def script(pilot, app, agent):
        eye = app.query_one(HalEye)
        seen = [eye.state]
        app.set_busy(True)
        seen.append(eye.state)
        send = app.ui.approval_sender(FakeBridge({"hal-1": REQUEST}), lambda note: None)
        await send({"id": "hal-1"})
        assert await until(pilot, lambda: isinstance(app.screen, ApprovalModal))
        seen.append(eye.state)
        await pilot.press("escape")
        assert await until(pilot, lambda: not isinstance(app.screen, ApprovalModal))
        await settle(pilot)
        seen.append(eye.state)
        app.set_busy(False)
        seen.append(eye.state)
        return seen

    assert drive(tmp_path, script) == ["idle", "thinking", "approval", "thinking", "idle"]
