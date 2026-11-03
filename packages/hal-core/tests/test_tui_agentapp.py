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
from halcore.tui.chrome import ChangesList
from halcore.tui.controller import Controller
from halcore.tui.render import Renderer
from halcore.tui.theme import load_look
from halcore.tui.widgets import SafeStatic, Segmented, ToolCard

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
    assert running and "Run    pytest -q" in done and "ok" in done


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


async def click_top(pilot, app, key):
    from halcore.tui.chrome import TopBar

    bar = app.query_one(TopBar)
    start, _ = bar.spans[key]
    await pilot.click(TopBar, offset=(start, 0))
    await settle(pilot)


def top_text(app):
    from halcore.tui.chrome import TopBar

    return str(app.query_one(TopBar).render())


def test_model_click_in_the_top_bar_opens_picker_and_switches_model(tmp_path):
    def setup(controller):
        controller.state.list_models = lambda: [{"id": "qwen3:8b"}, {"id": "gemma3:12b", "context_length": 32768}]

    async def script(pilot, app, agent):
        await click_top(pilot, app, "model")
        assert await until(pilot, lambda: isinstance(app.screen, screens.PickerModal))
        await type_text(pilot, "gemma")
        await pilot.press("enter")
        assert await until(pilot, lambda: ("session/set_model", {"sessionId": SID, "modelId": "gemma3:12b"}) in agent.calls)
        await settle(pilot)
        return top_text(app)

    label = drive(tmp_path, script, setup=setup)
    assert "gemma3:12b" in label


def test_shift_tab_cycles_mode_and_status_updates(tmp_path):
    async def script(pilot, app, agent):
        await pilot.press("shift+tab")
        await settle(pilot)
        return app.state.mode, str(app.query_one("#status").render()), top_text(app)

    mode, status, top = drive(tmp_path, script)
    assert mode == "auto-edit" and "auto-edit" in top and "mode" not in status  # the top bar owns the mode


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
        from halcore.tui.chrome import TopBar

        return shown, "mode" in app.query_one(TopBar).spans, bool(app.query("#side-changes"))

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


def test_top_bar_items_stay_on_screen_with_a_long_model_label(tmp_path):
    # regression: at 80x24 a real model label pushed the effort and lockdown chips off screen
    from halcore.tui.chrome import TopBar

    def setup(controller):
        controller.state.status.model = "ollama/qwen3-coder:30b-a3b-instruct (q4_K_M)"
        controller.state.status.lockdown, controller.state.status.paranoia = "strict", "high"

    for size in ((140, 36), (80, 24), (60, 20)):

        async def script(pilot, app, agent):
            app.refresh_status()
            await settle(pilot)
            bar = app.query_one(TopBar)
            return dict(bar.spans), bar.region, str(app.query_one("#status").render())

        spans, region, status = drive(tmp_path, script, size=size, setup=setup)
        assert {"model", "mode", "effort", "gauge"} <= set(spans), (size, spans)
        assert all(region.x + end <= size[0] for _, end in spans.values()) and region.height == 1, (size, spans)
        if size[0] >= 140:
            assert "lockdown strict/high" in status


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
        await click_top(pilot, app, "effort")
        if isinstance(app.screen, screens.PickerModal | screens.EffortPopover):
            await pilot.press("escape")
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


@pytest.mark.parametrize("size", [(80, 24), (130, 36), (200, 50)])
def test_side_panel_is_hidden_at_every_width_until_ctrl_b(tmp_path, size):
    async def script(pilot, app, agent):
        side = app.query_one("#side")
        states = [side.display]
        await pilot.resize_terminal(size[0] + 20, size[1])
        await settle(pilot)
        states.append(side.display)
        await pilot.press("ctrl+b")
        await settle(pilot)
        states.append(side.display)
        await pilot.resize_terminal(*size)
        await settle(pilot)
        states.append(side.display)
        await pilot.press("ctrl+b")
        await settle(pilot)
        return [*states, side.display]

    assert drive(tmp_path, script, size=size) == [False, False, True, True, False]


def test_click_on_the_side_handle_toggles_the_panel(tmp_path):
    from halcore.tui.chrome import SideHandle

    async def script(pilot, app, agent):
        states = []
        for _ in range(2):
            await pilot.click(SideHandle, offset=(0, 3))
            await settle(pilot)
            states.append((app.query_one("#side").display, app.query_one(SideHandle).open))
        return states

    assert drive(tmp_path, script, size=(120, 36)) == [(True, True), (False, False)]


def test_dragging_the_handle_resizes_the_side_panel_within_limits(tmp_path):
    from halcore.tui import tokens
    from halcore.tui.chrome import SideHandle

    async def drag_to(pilot, app, x):
        handle = app.query_one(SideHandle)
        await pilot.mouse_down(handle, offset=(0, 3))
        await pilot.hover(None, offset=(x, 5))
        await pilot.mouse_up(None, offset=(x, 5))
        await settle(pilot)
        return app.query_one("#side").outer_size.width

    async def script(pilot, app, agent):
        await pilot.press("ctrl+b")
        await settle(pilot)
        widths = [await drag_to(pilot, app, x) for x in (120 - 1 - 45, 5, 118)]
        return widths, app.query_one("#side").display

    widths, shown = drive(tmp_path, script, size=(120, 36))
    assert widths == [45, tokens.SIDE_MAX, tokens.SIDE_MIN] and shown


def test_side_panel_lists_changed_paths_cleaned(tmp_path):
    async def script(pilot, app, agent):
        info = app.controller.live_info()
        info.changes, info.changed = 2, [("M", "src/calc.py"), ("A", "evil\x1b[31m\u202e.py")]
        app.refresh_side(info)
        app.show_side(True)
        await settle(pilot)
        return plain(app.query_one("#side-changes")), plain(app.query_one("#changes")), plain(app.query_one("#side-plan"))

    head, paths, plan = drive(tmp_path, script)
    assert head.strip() == "CHANGES  2" and plan.strip() == "PLAN"
    assert "M src/calc.py" in paths and "\x1b" not in paths and "\u202e" not in paths


class DiffSession(FakeSession):
    def diff(self, since=None):
        return (
            "diff --git a/src/calc.py b/src/calc.py\n@@ -1 +1 @@\n-return a - b\n+return a + b\x1b[31m\n"
            "diff --git a/notes.md b/notes.md\n+other file\n"
        )


def test_changes_path_opens_a_read_only_diff(tmp_path):
    async def script(pilot, app, agent):
        info = app.controller.live_info()
        info.changes, info.changed = 2, [("M", "notes.md"), ("M", "src/calc.py")]
        app.refresh_side(info)
        app.show_side(True)
        await settle(pilot)
        await pilot.click(ChangesList, offset=(4, 1))
        await settle(pilot)
        screen = app.screen
        shown = isinstance(screen, screens.DiffScreen) and plain(screen.query_one("#diff-body"))
        await pilot.press("escape")
        await settle(pilot)
        return shown, isinstance(app.screen, screens.DiffScreen)

    shown, still_open = drive(tmp_path, script, session=DiffSession())
    assert shown and "+return a + b" in shown and "other file" not in shown and "\x1b" not in shown
    assert not still_open


def test_click_below_the_listed_paths_opens_nothing(tmp_path):
    async def script(pilot, app, agent):
        info = app.controller.live_info()
        info.changes, info.changed = 14, [("M", f"f{n}.py") for n in range(14)]
        app.refresh_side(info)
        app.show_side(True)
        await settle(pilot)
        await pilot.click(ChangesList, offset=(4, agentapp.SIDE_PATHS_SHOWN))
        await settle(pilot)
        return plain(app.query_one("#changes")), isinstance(app.screen, screens.DiffScreen)

    paths, opened = drive(tmp_path, script, session=DiffSession())
    assert "+2 more" in paths and not opened


def thinking_model(controller):
    controller.state.model = "qwen3:8b"


def record_sends(app):
    sent = []
    app.send = sent.append
    return sent


def test_effort_popover_lists_only_supported_levels(tmp_path):
    async def script(pilot, app, agent):
        sent = record_sends(app)
        await click_top(pilot, app, "effort")
        await settle(pilot)
        popover = app.screen
        levels = [seg.value for seg in popover.query_one(Segmented).segments]
        await pilot.press("right", "enter")
        await settle(pilot)
        return isinstance(popover, screens.EffortPopover), levels, sent, app.query_one(Composer).has_focus

    shown, levels, sent, focused = drive(tmp_path, script, setup=thinking_model)
    assert shown and levels == ["default", "off", "low", "medium", "high", "max"]
    assert sent == ["/effort off"] and focused


def test_effort_popover_without_levels_says_why_and_sends_nothing(tmp_path):
    async def script(pilot, app, agent):
        sent = record_sends(app)
        await click_top(pilot, app, "effort")
        await settle(pilot)
        text = " ".join(plain(w) for w in app.screen.query(SafeStatic))
        await pilot.press("enter")
        await settle(pilot)
        return text, bool(app.screen.query(Segmented)), sent

    text, has_levels, sent = drive(tmp_path, script)
    assert "not available" in text and not has_levels and sent == []


def wheel(app, key, step):
    from halcore.tui.chrome import TopBar

    bar = app.query_one(TopBar)
    bar.post_message(TopBar.Wheeled(bar, key, step))


def test_wheel_on_effort_commits_once_after_settling(tmp_path, monkeypatch):
    monkeypatch.setattr(agentapp, "EFFORT_SETTLE_S", 0.4)

    async def script(pilot, app, agent):
        from halcore.tui.chrome import TopBar

        sent = record_sends(app)
        for _ in range(3):
            wheel(app, "effort", 1)
        await settle(pilot)
        pending = list(sent), app.query_one(TopBar).info.effort
        await asyncio.sleep(0.6)
        await settle(pilot)
        return pending, sent, app.query_one(TopBar).info.effort

    pending, sent, after = drive(tmp_path, script, setup=thinking_model)
    assert pending == ([], "medium") and sent == ["/effort medium"] and after == "default"


def test_wheel_on_effort_is_ignored_during_a_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(agentapp, "EFFORT_SETTLE_S", 0.05)

    async def script(pilot, app, agent):
        sent = record_sends(app)
        app.busy = True
        wheel(app, "effort", 1)
        await asyncio.sleep(0.2)
        await settle(pilot)
        return sent, app.wheel_effort

    assert drive(tmp_path, script, setup=thinking_model) == ([], "")


def test_wheel_on_model_and_mode_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(agentapp, "EFFORT_SETTLE_S", 0.05)

    async def script(pilot, app, agent):
        sent = record_sends(app)
        for key in ("model", "mode", "gauge"):
            wheel(app, key, 1)
            wheel(app, key, -1)
        await asyncio.sleep(0.2)
        await settle(pilot)
        return sent, app.state.status.mode

    sent, mode = drive(tmp_path, script, setup=thinking_model)
    assert sent == [] and mode == "default"


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


def test_status_line_shows_approval_waiting_in_words(tmp_path):
    from test_tui_approval_modal import REQUEST, FakeBridge

    async def script(pilot, app, agent):
        send = app.ui.approval_sender(FakeBridge({"hal-1": REQUEST}), lambda note: None)
        await send({"id": "hal-1"})
        assert await until(pilot, lambda: isinstance(app.screen, ApprovalModal))
        return str(app.query_one("#status").render())

    assert "approval waiting" in drive(tmp_path, script)


def test_key_hints_change_while_a_turn_runs(tmp_path):
    async def script(pilot, app, agent):
        idle = str(app.query_one("#status").render())
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: app.busy)
        await settle(pilot)
        busy = str(app.query_one("#status").render())
        await pilot.press("escape")
        return idle, busy

    idle, busy = drive(tmp_path, script, turns=[[WAIT_CANCEL]])
    assert "^P commands" in idle and "Esc stops" not in idle and "Esc stops" in busy


def test_status_line_draws_a_tok_s_sparkline_during_a_turn(tmp_path):
    async def script(pilot, app, agent):
        app.tok_samples = [10.0, 20.0, 40.0]
        app.refresh_status()
        await settle(pilot)
        return str(app.query_one("#status").render())

    assert "40 tok/s" in drive(tmp_path, script)


def test_attachment_chip_x_removes_the_image(tmp_path):
    from halcore.tui.agentapp import AttachmentChips

    async def script(pilot, app, agent):
        app.state.attachments.append({"type": "image", "data": "", "mimeType": "image/png"})
        app.refresh_status()
        await settle(pilot)
        chips = app.query_one(AttachmentChips)
        shown = (chips.display, str(chips.render()))
        start, _, _, _ = chips.spans[0]
        await pilot.click(AttachmentChips, offset=(chips.styles.padding.left + start, 0))
        await settle(pilot)
        return shown, list(app.state.attachments), chips.display

    (shown, text), left, after = drive(tmp_path, script)
    assert shown and "image 1" in text and left == [] and not after


def test_paste_chip_shows_under_the_composer_and_x_drops_the_paste(tmp_path):
    from halcore.tui.agentapp import AttachmentChips

    async def script(pilot, app, agent):
        composer = app.query_one(Composer)
        composer.post_message(events.Paste("a\nb\nc\nd"))
        await settle(pilot)
        chips = app.query_one(AttachmentChips)
        text = str(chips.render())
        app.remove_chip("paste", 0)
        await settle(pilot)
        return text, composer.text, composer.pastes

    text, left, pastes = drive(tmp_path, script)
    assert "paste 4 lines" in text and left == "" and pastes == {}


def test_composer_is_borderless_with_a_prompt_mark(tmp_path):
    from halcore.tui.agentapp import PromptMark

    async def script(pilot, app, agent):
        composer = app.query_one(Composer)
        return composer.region.height, app.query_one(PromptMark).has_class("focused"), str(composer.styles.border_top[0])

    height, focused, border = drive(tmp_path, script)
    assert height == 1 and focused and border in ("", "none")


def test_queued_message_gets_its_own_turn_rail(tmp_path):
    from halcore.tui.transcript import TurnView

    async def script(pilot, app, agent):
        await type_text(pilot, "first")
        await pilot.press("enter")
        assert await until(pilot, lambda: app.busy)
        await type_text(pilot, "second")
        await pilot.press("enter")
        await settle(pilot)
        turns = list(app.query(TurnView))
        during = [(t.message.text, t.has_class("queued"), t.has_class("running"), t.message.queued) for t in turns]
        await pilot.press("escape")
        assert await until(pilot, lambda: len(agent.prompts) == 2 and not app.busy)
        await settle(pilot)
        after = [(t.message.text, t.has_class("queued")) for t in app.query(TurnView)]
        return during, after

    during, after = drive(tmp_path, script, turns=[[WAIT_CANCEL], [chunk("two")]])
    assert during == [("first", False, True, False), ("second", True, False, True)]
    assert after == [("first", False), ("second", False)]


def test_running_turn_rail_uses_the_accent(tmp_path):
    from halcore.tui.transcript import TurnView

    async def script(pilot, app, agent):
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: app.busy)
        turn = app.query_one(TurnView)
        await settle(pilot)
        from textual.color import Color

        running = (turn.has_class("running"), turn.styles.border_left[1] == Color.parse(app.theme_variables["primary"]))
        await pilot.press("escape")
        assert await until(pilot, lambda: not app.busy)
        await settle(pilot)
        return running, turn.has_class("running")

    (running, accent), after = drive(tmp_path, script, turns=[[WAIT_CANCEL]])
    assert running and accent and not after


def test_output_of_a_turn_stays_inside_its_rail(tmp_path):
    from halcore.tui.transcript import TurnView

    async def script(pilot, app, agent):
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: "hi" in transcript_text(app) and not app.busy)
        await settle(pilot)
        turn = app.query_one(TurnView)
        return "\n".join(plain(w) for w in turn.query(SafeStatic))

    text = drive(tmp_path, script)
    assert "go" in text and "hi" in text and "tok" in text


def edit_update(cid="e1", n=10):
    old = "".join(f"line {i}\n" for i in range(n))
    new = "".join(f"line {i} changed\n" for i in range(n))
    return {"sessionUpdate": "tool_call", "toolCallId": cid, "title": "Edit src/many.py", "status": "completed",
            "content": [{"type": "diff", "path": "src/many.py", "oldText": old, "newText": new}]}  # fmt: skip


def test_edit_row_shows_six_diff_lines_and_expands_to_all(tmp_path):
    async def script(pilot, app, agent):
        await type_text(pilot, "edit")
        await pilot.press("enter")
        assert await until(pilot, lambda: app.query(ToolCard) and not app.busy)
        await settle(pilot)
        card = app.query_one(ToolCard)
        preview = plain(card.query_one(".preview"))
        more = plain(card.query_one(".more"))
        card.action_toggle()
        await settle(pilot)
        return preview, more, card.open, len(card.body.children), card.query_one(".preview").display

    preview, more, opened, full_rows, preview_shown = drive(tmp_path, script, turns=[[edit_update(), chunk("done")]])
    assert len([ln for ln in preview.splitlines() if " + " in ln or " - " in ln]) == 6
    assert "more lines" in more and "@@" not in preview
    assert opened and full_rows > 6 and not preview_shown


def test_click_on_an_edit_path_only_expands_that_diff(tmp_path):
    async def script(pilot, app, agent):
        await type_text(pilot, "edit")
        await pilot.press("enter")
        assert await until(pilot, lambda: app.query(ToolCard) and not app.busy)
        await settle(pilot)
        card = app.query_one(ToolCard)
        screen, prompts = app.screen, len(agent.prompts)
        await pilot.click(ToolCard, offset=(10, 0))
        await settle(pilot)
        return card.open, app.screen is screen, len(agent.prompts) == prompts, app.inbox.empty()

    assert drive(tmp_path, script, turns=[[edit_update(), chunk("done")]]) == (True, True, True, True)


def read_update(cid, path, status="completed"):
    return {"sessionUpdate": "tool_call", "toolCallId": cid, "title": f"Read {path}", "status": status,
            "content": [{"type": "content", "content": {"type": "text", "text": "x"}}]}  # fmt: skip


def run_tools(tmp_path, updates):
    from halcore.tui.transcript import ToolGroup

    async def script(pilot, app, agent):
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: "done" in transcript_text(app) and not app.busy)
        await settle(pilot)
        groups = list(app.query(ToolGroup))
        cards = list(app.query(ToolCard))
        return ([str(g.render()) for g in groups], [g.region.height for g in groups],
                [(c.row.verb if c.row else "", c.display, c.open) for c in cards])  # fmt: skip

    return drive(tmp_path, script, turns=[[*updates, chunk("done")]])


def test_three_reads_fold_into_one_ran_tools_row(tmp_path):
    shown, heights, cards = run_tools(tmp_path, [read_update(f"r{i}", f"src/f{i}.py") for i in range(3)])
    assert len(shown) == 1 and "ran 3 tools" in shown[0] and "read f0.py, f1.py, f2.py" in shown[0]
    assert heights == [1] and [d for _, d, _ in cards] == [False, False, False]


def test_two_reads_do_not_fold(tmp_path):
    shown, _, cards = run_tools(tmp_path, [read_update(f"r{i}", f"src/f{i}.py") for i in range(2)])
    assert shown == [] and all(d for _, d, _ in cards)


def test_edit_rows_are_never_folded(tmp_path):
    updates = [read_update("r0", "a.py"), read_update("r1", "b.py"), edit_update("e1", 2), read_update("r2", "c.py")]
    shown, _, cards = run_tools(tmp_path, updates)
    assert shown == [] and all(d for _, d, _ in cards)


def test_a_failed_tool_stays_open_and_ends_the_group(tmp_path):
    updates = [*(read_update(f"r{i}", f"f{i}.py") for i in range(3)), read_update("bad", "x.py", "failed"),
               read_update("r9", "z.py")]  # fmt: skip
    shown, _, cards = run_tools(tmp_path, updates)
    assert len(shown) == 1 and "ran 3 tools" in shown[0]
    failed = [c for c in cards if c[1] and c[2]]
    assert len(failed) == 1 and cards[-1][1]  # the failure is open; the read after it stays a row


def block_gaps(tmp_path, size):
    async def script(pilot, app, agent):
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: "done" in transcript_text(app) and not app.busy)
        await settle(pilot)
        turn = app.transcript.active or app.query("TurnView").last()
        rows = [w for w in turn.children if w.display and w is not turn.message and w is not turn.tail]
        return [(type(w).__name__, w.styles.margin.top) for w in rows], app.transcript.has_class("compact")

    updates = [chunk("Looking.\n\n"), read_update("r0", "a.py"), read_update("r1", "b.py"), chunk("done")]
    return drive(tmp_path, script, turns=[updates], size=size)


def test_blocks_of_a_new_kind_get_one_blank_row_and_tool_rows_none(tmp_path):
    rows, compact = block_gaps(tmp_path, (120, 36))
    assert not compact
    assert [m for _, m in rows] == [1, 1, 0, 1, 1]  # prose, tool, tool, prose, end line


def test_a_small_screen_drops_the_blank_rows_between_blocks(tmp_path):
    rows, compact = block_gaps(tmp_path, (80, 24))
    assert compact and all(m == 0 for _, m in rows)


def code_scene(tmp_path, text, extra=None):
    from halcore.tui.transcript import CodeBlock, CodeHeader

    copied = []

    async def script(pilot, app, agent):
        app.copy_to_clipboard = copied.append
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: app.query(CodeBlock) and not app.busy)
        await settle(pilot)
        block = app.query_one(CodeBlock)
        header = str(block.query_one(CodeHeader).render())
        await pilot.click(CodeHeader)
        await settle(pilot)
        return block.label, header, transcript_text(app)

    label, header, text_shown = drive(tmp_path, script, turns=[[chunk(text)]])
    return label, header, text_shown, copied


def test_code_block_copy_copies_the_source_only(tmp_path):
    label, header, shown, copied = code_scene(tmp_path, "Fix:\n\n```python\ndef add(a, b):\n    return a + b\n```\n\nDone.\n")
    assert label == "python" and header.startswith("python") and header.rstrip().endswith("copy")
    assert copied == ["def add(a, b):\n    return a + b"]
    assert "Fix:" in shown and "Done." in shown and "```" not in shown


def test_code_block_language_label_is_sanitised(tmp_path):
    label, _, _, _ = code_scene(tmp_path, "```py\u202ethon<script>[b]x[/b] extra\nprint(1)\n```\n")
    assert label.isascii() and "[" not in label and "<" not in label and len(label) <= 16


def test_hostile_code_language_cannot_add_markup(tmp_path):
    label, header, _, copied = code_scene(tmp_path, "```[@click=app.quit]\nboom\n```\n")
    assert "[" not in header and "=" not in header and copied == ["boom"]


def test_thought_line_shows_seconds_and_opens(tmp_path, monkeypatch):
    from halcore.tui import render
    from halcore.tui.widgets import ThinkingFold

    now = [100.0]
    update = Renderer.update

    def timed_update(self, item):
        if item.get("sessionUpdate") == "agent_thought_chunk":
            now[0] += 4.2  # two thought chunks 4.2s apart
        return update(self, item)

    monkeypatch.setattr(render, "clock", lambda: now[0])
    monkeypatch.setattr(Renderer, "update", timed_update)

    async def script(pilot, app, agent):
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: "done" in transcript_text(app) and not app.busy)
        await settle(pilot)
        fold = app.query_one(ThinkingFold)
        closed = fold.query_one(".summary").render()
        await pilot.click(ThinkingFold)
        await settle(pilot)
        opened = fold.query_one(".summary").render()
        return str(closed), str(opened), fold.open, bool(fold.query(".body"))

    thought = {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "think " * 20}}
    closed, opened, is_open, has_body = drive(tmp_path, script, turns=[[thought, thought, chunk("done")]])
    assert "thought for 4s" in closed and "tokens" not in closed
    assert "thought for 4s" in opened and "~60 tokens" in opened and is_open and has_body


def plan_update(*statuses):
    names = ("Read the failing test", "Fix add()", "Run pytest")
    return {"sessionUpdate": "plan", "entries": [{"content": n, "status": st} for n, st in zip(names, statuses, strict=True)]}


def test_plan_block_updates_in_place_with_progress(tmp_path):
    from halcore.tui.transcript import PlanBlock

    async def script(pilot, app, agent):
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: "done" in transcript_text(app) and not app.busy)
        await settle(pilot)
        return [str(b.render()) for b in app.query(PlanBlock)], "Todo" in transcript_text(app)

    turn = [plan_update("in_progress", "pending", "pending"), chunk("working\n\n"),
            plan_update("completed", "in_progress", "pending"), chunk("done")]  # fmt: skip
    blocks, panel = drive(tmp_path, script, turns=[turn])
    assert len(blocks) == 1 and not panel
    lines = blocks[0].splitlines()
    assert lines[0] == "plan  ###-----  1 of 3"
    assert lines[1:] == ["  [x] Read the failing test", "  [>] Fix add()", "  [ ] Run pytest"]


def test_each_turn_gets_its_own_plan_block(tmp_path):
    from halcore.tui.transcript import PlanBlock

    async def script(pilot, app, agent):
        for word in ("one", "two"):
            await type_text(pilot, word)
            await pilot.press("enter")
            assert await until(pilot, lambda: not app.busy and transcript_text(app).count("ok") >= 1)
            await settle(pilot)
        assert await until(pilot, lambda: len(app.query(PlanBlock)) == 2)
        return [b.parent is not None for b in app.query(PlanBlock)]

    turns = [[plan_update("pending", "pending", "pending"), chunk("ok")], [plan_update("completed", "completed", "completed"), chunk("ok")]]
    assert drive(tmp_path, script, turns=turns) == [True, True]


def test_subagent_tree_updates_in_place(tmp_path):
    from halcore.tui.transcript import SubagentTree

    async def script(pilot, app, agent):
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: "done" in transcript_text(app) and not app.busy)
        await settle(pilot)
        return [(t.done, str(t.render())) for t in app.query(SubagentTree)]

    sub = {"sessionUpdate": "hal_subagent", "subagentId": "s1", "agent": "explorer"}
    tools = [{**sub, "phase": "tool", "tool": f"Read src/f{i}.py"} for i in range(5)]
    turn = [{**sub, "phase": "start", "task": "find the callers of add()"}, *tools, {**sub, "phase": "done"}, chunk("done")]
    trees = drive(tmp_path, script, turns=[turn])
    assert len(trees) == 1
    done, shown = trees[0]
    lines = shown.splitlines()
    assert done and lines[0].startswith("ok explorer  find the callers of add()") and lines[0].rstrip().endswith("s")
    assert lines[1:] == ["  +2 more", "  |- Read   src/f2.py", "  |- Read   src/f3.py", "  `- Read   src/f4.py"]


def row_zero(widget):
    return "".join(seg.text for seg in widget.render_line(0))


async def click_word(pilot, widget, word):
    start = next(a for w, a, _ in widget.hover_spans(widget.content_size.width) if w == word)
    await pilot.hover(widget, offset=(start, 0))
    await pilot.click(widget, offset=(start, 0))


@pytest.mark.parametrize(("mode", "words"), [("code", ["copy", "edit", "again", "rewind"]), ("chat", ["copy", "edit", "again"])])
def test_hover_actions_offer_copy_edit_again_and_rewind_only_with_a_checkpoint(tmp_path, mode, words):
    async def script(pilot, app, agent):
        await type_text(pilot, "change things")
        await pilot.press("enter")
        assert await until(pilot, lambda: not app.busy and "hi" in transcript_text(app))
        message = app.transcript.query_one(UserMessage)
        before = row_zero(message)
        await pilot.hover(message, offset=(0, 0))
        await settle(pilot)
        return before, row_zero(message), message.hover_actions()

    before, hovered, offered = drive(tmp_path, script, mode=mode)
    assert offered == words and "copy" not in before
    assert "  ".join(words) in hovered and "change things" in hovered


def test_hover_actions_hide_rewind_until_the_message_has_a_checkpoint(tmp_path):
    async def script(pilot, app, agent):
        message = UserMessage("x", app.look)
        await app.transcript.mount(message)
        return message.hover_actions()

    assert drive(tmp_path, script) == ["copy", "edit", "again"]


@pytest.mark.parametrize(("mode", "stamped"), [("chat", True), ("code", False)])
def test_chat_shows_time_on_hover(tmp_path, monkeypatch, mode, stamped):
    from halcore.tui import transcript
    from halcore.tui.transcript import AssistantBlock

    monkeypatch.setattr(transcript, "stamp_now", lambda: "18:42")

    async def script(pilot, app, agent):
        await type_text(pilot, "hello")
        await pilot.press("enter")
        assert await until(pilot, lambda: not app.busy and app.query(AssistantBlock))
        await settle(pilot)
        rows = []
        for widget in (app.transcript.query_one(UserMessage), app.query(AssistantBlock).first()):
            await pilot.hover(widget, offset=(0, 0))
            await settle(pilot)
            rows.append(row_zero(widget).rstrip())
        stamp = app.transcript.query_one(UserMessage)
        start = next(a for w, a, _ in stamp.hover_spans(stamp.content_size.width) if w == stamp.hover_words()[-1])
        return rows, stamp.action_at(start, 0)

    rows, clicked = drive(tmp_path, script, mode=mode, turns=[[chunk("Hi there.\n\n")]])
    assert all(r.endswith("18:42") for r in rows) is stamped
    assert rows[0].endswith("again  18:42" if stamped else "again  rewind") and rows[1].endswith("copy  18:42" if stamped else "copy")
    assert clicked is None if stamped else clicked == "rewind"


def test_again_sends_the_same_text_as_a_new_turn(tmp_path):
    async def script(pilot, app, agent):
        await type_text(pilot, "say hi")
        await pilot.press("enter")
        assert await until(pilot, lambda: not app.busy and "hi" in transcript_text(app))
        await click_word(pilot, app.transcript.query_one(UserMessage), "again")
        assert await until(pilot, lambda: len(agent.prompts) == 2 and not app.busy)
        return agent.prompts, len(app.transcript.query(UserMessage))

    prompts, messages = drive(tmp_path, script)
    assert prompts[0] == prompts[1] and messages == 2


def test_edit_loads_the_message_into_the_composer_without_sending(tmp_path):
    async def script(pilot, app, agent):
        await type_text(pilot, "say hi")
        await pilot.press("enter")
        assert await until(pilot, lambda: not app.busy and "hi" in transcript_text(app))
        await click_word(pilot, app.transcript.query_one(UserMessage), "edit")
        await settle(pilot)
        composer = app.query_one(Composer)
        return composer.text, composer.has_focus, len(agent.prompts), isinstance(app.screen, screens.ChoiceModal)

    assert drive(tmp_path, script) == ("say hi", True, 1, False)


def test_enter_on_a_focused_message_offers_the_actions_dialog(tmp_path):
    async def script(pilot, app, agent):
        await type_text(pilot, "say hi")
        await pilot.press("enter")
        assert await until(pilot, lambda: not app.busy and "hi" in transcript_text(app))
        app.transcript.query_one(UserMessage).focus()
        await pilot.press("enter")
        assert await until(pilot, lambda: isinstance(app.screen, screens.ChoiceModal))
        return sorted(b.id for b in app.screen.query(Button))

    assert drive(tmp_path, script, mode="chat") == ["choice-again", "choice-cancel", "choice-copy", "choice-edit"]


def test_assistant_block_copy_copies_its_markdown_source(tmp_path):
    from halcore.tui.transcript import AssistantBlock

    copied = []

    async def script(pilot, app, agent):
        app.copy_to_clipboard = copied.append
        await type_text(pilot, "go")
        await pilot.press("enter")
        assert await until(pilot, lambda: not app.busy and app.query(AssistantBlock))
        await settle(pilot)
        block = app.query(AssistantBlock).first()
        await click_word(pilot, block, "copy")
        await settle(pilot)
        return block.hover_actions()

    words = drive(tmp_path, script, turns=[[chunk("Some **bold** text.\n\n")]])
    assert words == ["copy"] and copied == ["Some **bold** text."]


def test_tool_group_summary_comes_from_the_sink_rows_not_unfinished_cards():
    # regression: a group of calls that never started rendered "ran 3 tools" with no targets
    # when it was drawn before the cards' deferred `finish` set their rows (flaky snapshot)
    from rich.text import Text

    from halcore.tui.render import ToolRow
    from halcore.tui.transcript import ToolGroup, group_summary

    cards = [ToolCard(f"r{i}", Text(""), 0) for i in range(3)]
    rows = [ToolRow(verb="Read", target=f"src/f{i}.py", ok=True, seconds=0.1) for i in range(3)]
    group = ToolGroup(cards, rows)
    assert all(c.row is None for c in cards)
    assert group_summary(group.rows) == ["read f0.py, f1.py, f2.py"]
