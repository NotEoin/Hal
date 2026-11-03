"""Tests of the chrome widgets of `hal code` and `hal chat`: the HAL eye (in a bare app with a fake clock)."""

from __future__ import annotations

import asyncio
import io

from textual.app import App, ComposeResult

from halcore.tui import chrome, motion, tokens
from halcore.tui.chrome import HalEye
from halcore.tui.theme import load_look


def look_of(animations, theme="dark"):
    return load_look({"unicode": False, "animations": animations, "theme": theme}, stream=io.StringIO(), environ={})


class Host(App):
    def __init__(self, look):
        super().__init__()
        self.look = look
        self.motion = None

    def compose(self) -> ComposeResult:
        yield HalEye()

    def on_mount(self):
        self.motion = motion.Ticker(self.set_interval, enabled=self.look.animations)


def run(look, script):
    async def go():
        app = Host(look)
        async with app.run_test(size=(20, 3)) as pilot:
            await pilot.pause()
            return await script(pilot, app, app.query_one(HalEye))

    return asyncio.run(go())


def test_eye_shows_idle_thinking_and_approval():
    async def script(pilot, app, eye):
        seen = [(eye.state, str(eye.render()), app.motion.active)]
        for state in ("thinking", "approval", "idle"):
            eye.state = state
            await pilot.pause()
            seen.append((state, str(eye.render()), app.motion.active))
        return seen

    seen = run(look_of(True), script)
    assert [(s, g) for s, g, _ in seen] == [("idle", "o"), ("thinking", "O"), ("approval", "!"), ("idle", "o")]
    assert [n for _, _, n in seen] == [0, 1, 1, 0]


def test_eye_is_static_with_animations_off():
    async def script(pilot, app, eye):
        eye.state = "thinking"
        await pilot.pause()
        return app.motion.active, app.motion.running

    assert run(look_of(False), script) == (0, False)
    roles = tokens.ROLES["dark"]
    off = look_of(False)
    assert chrome.eye_style("thinking", 0.3, off) == chrome.eye_style("thinking", 1.7, off) == roles["accent"]
    assert chrome.eye_style("approval", 0.7, off) == f"bold {roles['warn']}"


def test_eye_pulses_between_dim_and_full_red_while_thinking():
    on = look_of(True)
    roles = tokens.ROLES["dark"]
    assert chrome.eye_style("thinking", 0.0, on) == roles["accent_dim"]
    assert chrome.eye_style("thinking", motion.EYE_PULSE_PERIOD_S / 2, on) == roles["accent"]
    assert chrome.eye_style("approval", 0.6, on) == roles["accent_dim"]


INFO = chrome.TopInfo(surface="code", project="/home/me/projects/calculator", copy=True,
                      model="ollama/qwen3-coder:30b-a3b-instruct (q4_K_M)", mode="default", effort="high",
                      used=13100, size=32768)  # fmt: skip


def test_top_bar_keeps_model_mode_effort_and_gauge_at_every_width():
    from halcore.tui.theme import ASCII, UNICODE

    for glyphs in (ASCII, UNICODE):
        for width in range(40, 201):
            layout = chrome.fit_top(INFO, width, glyphs)
            assert {"model", "mode", "effort", "gauge"} <= layout.keys(), width
            assert layout.width < width, (width, layout.width)


def test_top_bar_clips_long_model_names_with_an_ellipsis():
    from halcore.tui.theme import ASCII

    wide = chrome.fit_top(INFO, 200, ASCII)
    narrow = chrome.fit_top(INFO, 80, ASCII)
    model = dict((k, t) for k, t, _ in narrow.left)["model"]
    assert wide.level == 0 and "(q4_K_M)" in dict((k, t) for k, t, _ in wide.left)["model"]
    assert model.endswith("... v") and len(model) <= chrome.MODEL_CHIP_NARROW + 2
    assert "project" not in chrome.fit_top(INFO, 60, ASCII).keys()


def test_chat_top_bar_has_no_mode_item():
    from dataclasses import replace

    from halcore.tui.theme import ASCII

    assert "mode" not in chrome.fit_top(replace(INFO, mode=None), 120, ASCII).keys()


def test_model_label_is_the_same_in_bar_and_status():
    from halcore.tui import status

    info = status.StatusInfo()
    assert status.model_label(info) == "default"
    assert "default" in status.to_rich(status.segments(info, look_of(False)), look_of(False)).plain
    assert "no model" not in status.to_rich(status.segments(info, look_of(False)), look_of(False)).plain
