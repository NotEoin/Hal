"""Snapshot tests of the full-screen UIs: each screen at 80x24 and 120x36, dark and light, against ASCII goldens.

Goldens live in `data/tui_snapshots` (see `tuisnap.py` for the format). Besides the golden
comparison, every snapshot is checked for things a bad golden could hide: widgets inside the
screen, one-row widgets one row high, and no label broken across two rows (the tune role list
once wrapped model names mid-word and no test noticed). The clock is frozen so timings are
stable; regenerate with `HAL_SNAPSHOT_UPDATE=1`.
"""

from __future__ import annotations

import asyncio
import io
import types
from datetime import UTC, datetime
from unittest import mock

import pytest
import tuisnap
from test_tui_agentapp import FakeSession, ScriptedAgent, chunk, settle, until
from test_tui_approval_modal import REQUEST, FakeBridge

from halcore.acp.records import RecordStore
from halcore import sessionscmd
from halcore.tui import render, welcome
from halcore.tui.agentapp import AgentApp, Composer
from halcore.tui.approvalmodal import ApprovalModal
from halcore.tui.controller import Controller
from halcore.tui.render import Renderer
from halcore.tui.theme import load_look

SIZES = [(80, 24), (120, 36)]
THEMES = ["dark", "light"]
FROZEN = 1000.0
MODEL = "gemma-3-27b-it"
CODE = "Here is the fix:\n\n```python\ndef add(a, b):\n    return a + b\n```\n\nThe tests pass now.\n"
TURN = [
    {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "Let me look at the failing test first. " * 5}},
    {"sessionUpdate": "plan", "entries": [{"content": "Read the failing test", "status": "completed"},
                                          {"content": "Fix add()", "status": "in_progress"},
                                          {"content": "Run pytest", "status": "pending"}]},
    {"sessionUpdate": "tool_call", "toolCallId": "c0", "title": "Read src/calc.py", "status": "completed",
     "content": [{"type": "content", "content": {"type": "text", "text": "def add(a, b):\n    return a - b"}}]},
    {"sessionUpdate": "tool_call", "toolCallId": "c0b", "title": "Read tests/test_calc.py", "status": "completed",
     "content": [{"type": "content", "content": {"type": "text", "text": "def test_add():\n    assert add(2, 2) == 4"}}]},
    {"sessionUpdate": "tool_call", "toolCallId": "c0c", "title": "Grep add(", "status": "completed",
     "content": [{"type": "content", "content": {"type": "text", "text": "src/calc.py:1:def add(a, b):"}}]},
    {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Edit src/calc.py", "status": "completed",
     "content": [{"type": "diff", "path": "src/calc.py", "oldText": "def add(a, b):\n    return a - b\n",
                  "newText": "def add(a, b):\n    return a + b\n"}]},
    {"sessionUpdate": "tool_call", "toolCallId": "c2", "title": "Run pytest -q", "status": "completed",
     "content": [{"type": "content", "content": {"type": "text", "text": "....\n4 passed in 0.12s"}}]},
    {"sessionUpdate": "usage_update", "used": 13100, "size": 32768},
    chunk(CODE),
]  # fmt: skip
RECENT = [
    ("aaaaaaaaaaa1", "Explain the sandbox profile", "2026-10-30T16:05:00Z"),
    ("aaaaaaaaaaa2", "Add CSV export to the report command", "2026-11-01T09:12:00Z"),
    ("aaaaaaaaaaa3", "Fix the flaky add test", "2026-11-02T18:40:00Z"),
]
TIP_DAY = datetime(2026, 1, 1, tzinfo=UTC)  # day 1: the second tip, fixed so the golden does not change daily
LABELS = [MODEL, "src/calc.py", "pytest", "Read the failing test", "Allow these exact arguments this session",
          *(title for _, title, _ in RECENT)]  # fmt: skip


class Utf8(io.StringIO):
    """A stream that says it writes UTF-8, so the look picks the Unicode glyphs."""

    encoding = "utf-8"


def look_for(theme, ascii_look=False):
    return load_look({"unicode": not ascii_look, "animations": False, "notify": "off", "theme": theme},
                     stream=io.StringIO() if ascii_look else Utf8(), environ={})  # fmt: skip


@pytest.fixture(autouse=True)
def frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(render, "clock", lambda: FROZEN)
    monkeypatch.setattr(welcome, "clock", lambda: TIP_DAY)
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    rt = tmp_path / "rt"
    rt.mkdir(mode=0o700)
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(rt))


async def until_idle(pilot, app):
    assert await until(pilot, lambda: not app.busy and app.inbox.empty(), tries=150)
    await settle(pilot)


async def scene_welcome(pilot, app):
    await settle(pilot)


async def scene_turn(pilot, app):
    await pilot.press(*"fix the add test")
    await pilot.press("enter")
    await asyncio.sleep(0.05)
    await until_idle(pilot, app)


async def scene_approval(pilot, app):
    await scene_turn(pilot, app)
    send = app.ui.approval_sender(FakeBridge({"hal-1": REQUEST}), lambda note: None)
    await send({"id": "hal-1"})
    assert await until(pilot, lambda: isinstance(app.screen, ApprovalModal))
    await asyncio.sleep(app.screen.arm_s + 0.1)
    await settle(pilot)


async def scene_approval_record(pilot, app):
    await scene_turn(pilot, app)
    bridge = FakeBridge({"hal-1": REQUEST, "hal-2": {**REQUEST, "id": "apr_2"}})
    send = app.ui.approval_sender(bridge, lambda note: None)
    await send({"id": "hal-1"})
    await send({"id": "hal-2"})
    for keys in (("enter",), ("y", "enter")):
        assert await until(pilot, lambda: isinstance(app.screen, ApprovalModal))
        await asyncio.sleep(app.screen.arm_s + 0.1)
        await settle(pilot)
        await pilot.press(*keys)
        await settle(pilot)
    assert await until(pilot, lambda: len(bridge.answers) == 2 and not isinstance(app.screen, ApprovalModal))
    await until_idle(pilot, app)


SCENES = {"welcome": scene_welcome, "turn": scene_turn, "approval": scene_approval, "approval-record": scene_approval_record}
ASCII_SCENES = ("welcome", "turn", "approval")


def shoot(tmp_path, name, size, theme, *, ascii_look=False):
    look = look_for(theme, ascii_look)
    agent = ScriptedAgent([TURN])
    box = types.SimpleNamespace(session=FakeSession(), transport=agent)

    async def go():
        renderer = Renderer(io.StringIO(), look)
        controller = Controller(box, renderer, cwd="/work", project=None, store=RecordStore(tmp_path / "sessions"),
                                mode="code", live=False)  # fmt: skip
        await controller.start()
        controller.state.status.model = MODEL
        for sid, title, stamp in RECENT:
            with mock.patch.object(sessionscmd, "_now", return_value=stamp):
                sessionscmd.write_meta(controller.store, sid, title=title, mode="code")
        app = AgentApp(controller, look)
        controller.renderer.sink = app.sink
        async with app.run_test(size=size, notifications=False) as pilot:
            await settle(pilot)
            app.query_one(Composer).cursor_blink = False
            await SCENES[name](pilot, app)
            app.clear_notifications()
            await settle(pilot)
            snap = tuisnap.grid(app)
            tuisnap.assert_inside(app, ["#composer", "#status", "Button"])
            tuisnap.assert_one_row(app, ["#status", "#working"])
            tuisnap.assert_unbroken(snap, LABELS)
            tuisnap.compare(app, snap, name=name, theme=theme, look="ascii" if ascii_look else "unicode", tmp_path=tmp_path)

    asyncio.run(go())


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("name", list(SCENES))
def test_screen_matches_its_golden(tmp_path, name, size, theme):
    shoot(tmp_path, name, size, theme)


@pytest.mark.parametrize("name", ASCII_SCENES)
def test_ascii_look_matches_its_golden(tmp_path, name):
    shoot(tmp_path, name, (80, 24), "dark", ascii_look=True)


def test_broken_label_is_caught():
    rows = [[tuisnap.Cell(ch, "", "", False) for ch in line.ljust(20)] for line in ["roles: gemma-3-2", "7b-it chat"]]
    snap = tuisnap.Snapshot(20, 2, rows)
    with pytest.raises(AssertionError, match="broken across rows"):
        tuisnap.assert_unbroken(snap, [MODEL])


def test_clipped_label_with_an_ellipsis_passes():
    rows = [[tuisnap.Cell(ch, "", "", False) for ch in line.ljust(20)] for line in ["roles: gemma-3...", "next"]]
    tuisnap.assert_unbroken(tuisnap.Snapshot(20, 2, rows), [MODEL])


TUNE_MASKS = ((r"127\.0\.0\.1:\d+", "127.0.0.1:PORT"), (r"localhost:\d+", "localhost:PORT"))


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_tune_screen_matches_its_golden(tmp_path, size):
    from test_tune import CARD_24GB, SHOW, TAGS, arch_doc  # noqa: PLC0415 - tune fixtures only for this test
    from test_tuneui import QWEN_TAG  # noqa: PLC0415

    from halcore import tuneapp, tunestate  # noqa: PLC0415
    from halkit import hardware  # noqa: PLC0415
    from halkit.testing import TempHalHome  # noqa: PLC0415
    from halkit.testing.ollama import FakeOllama  # noqa: PLC0415

    look = look_for("dark")
    with TempHalHome() as home, FakeOllama([*TAGS, QWEN_TAG], {**SHOW, "qwen3:8b": arch_doc(36, 32, 8, 128, 40960)}) as server:
        home.config.write_text(f'[models.sources.local]\nkind = "ollama"\nurl = "{server.url}"\n'
                               '[models.roles.chat]\nsource = "local"\nmodel = "qwen3:8b"\n')  # fmt: skip
        state = tunestate.open_state(hw=hardware.from_dict(CARD_24GB))
        app = tuneapp.TuneApp(state, look)

        async def go():
            async with app.run_test(size=size, notifications=False) as pilot:
                await settle(pilot)
                app.clear_notifications()
                await settle(pilot)
                snap = tuisnap.grid(app)
                tuisnap.assert_unbroken(snap, ["qwen3:8b"])
                tuisnap.compare(app, snap, name="tune", theme="dark", look="unicode", tmp_path=tmp_path, masks=TUNE_MASKS)

        asyncio.run(go())
