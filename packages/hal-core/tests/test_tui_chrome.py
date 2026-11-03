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
