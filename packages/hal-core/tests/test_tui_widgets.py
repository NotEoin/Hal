"""Pilot tests for the shared full-screen widgets: slider, segments, gauge, safe text, tool cards and diffs."""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual.app import App, ComposeResult

from halcore.tui import widgets
from halcore.tui.widgets import FitGauge, SafeStatic, Segment, Segmented, Slider, ToolCard

SIZE = (60, 8)


class Host(App):
    """A bare app around the widgets under test; it records every message they post."""

    def __init__(self, *children):
        super().__init__()
        self.children_under_test = children
        self.seen = []

    def compose(self) -> ComposeResult:
        yield from self.children_under_test

    def on_slider_changed(self, event):
        self.seen.append(("slider", event.value, event.final, event.auto))

    def on_segmented_changed(self, event):
        self.seen.append(("segmented", event.value))


def run(app, script):
    async def go():
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            return await script(pilot)

    return asyncio.run(go())


def test_value_at_and_position_of_are_inverse_on_linear_and_log_tracks():
    assert widgets.value_at(0, 21, 0, 2, 0.05) == 0 and widgets.value_at(20, 21, 0, 2, 0.05) == 2
    assert widgets.value_at(10, 21, 0, 2, 0.05) == 1
    assert widgets.value_at(99, 21, 0, 2, 0.05) == 2  # clamped past the end
    mid = widgets.value_at(10, 21, 8192, 131072, 1024, log=True)
    assert mid == 32768  # halfway on a log track from 8K to 128K is 32K
    assert widgets.position_of(32768, 21, 8192, 131072, log=True) == 10


def test_slider_click_jumps_to_the_clicked_value():
    slider = Slider(0.7, 0, 2, 0.05, id="s")
    app = Host(slider)

    async def script(pilot):
        await pilot.click("#s", offset=(0, 0))
        await pilot.pause()
        return slider.value

    assert run(app, script) == 0
    assert app.seen[-1] == ("slider", 0, True, False)


def test_slider_drag_moves_and_reports_final_on_release():
    slider = Slider(0, 0, 2, 0.05, id="s")
    app = Host(slider)

    async def script(pilot):
        await pilot.mouse_down("#s", offset=(1, 0))
        await pilot.hover("#s", offset=(10, 0))
        await pilot.hover(None, offset=(59, 3))  # below and right of the widget: the knob still follows
        await pilot.mouse_up(None, offset=(59, 3))
        await pilot.pause()
        return slider.value

    assert run(app, script) == 2
    finals = [e for e in app.seen if e[2]]
    assert len(finals) == 1 and finals[0][1] == 2
    assert any(not e[2] for e in app.seen)  # moves before the release were not final


def test_slider_keys_step_page_home_end_and_clamp():
    slider = Slider(1.0, 0, 2, 0.05, id="s")
    app = Host(slider)

    async def script(pilot):
        slider.focus()
        values = []
        for key in ("right", "left", "left", "pagedown", "end", "right", "home", "left"):
            await pilot.press(key)
            values.append(slider.value)
        return values

    assert run(app, script) == [1.05, 1.0, 0.95, 1.45, 2, 2, 0, 0]


def test_slider_auto_toggle_reports_auto_state_only_when_offered():
    plain, auto = Slider(1, 0, 2, 0.5, id="plain"), Slider(1, 0, 2, 0.5, id="auto", allow_auto=True)
    app = Host(plain, auto)

    async def script(pilot):
        plain.focus()
        await pilot.press("a")
        auto.focus()
        await pilot.press("a")
        return plain.auto, auto.auto

    assert run(app, script) == (False, True)
    assert app.seen[-1][3] is True


def test_segmented_disabled_option_cannot_be_chosen_by_click_or_key():
    seg = Segmented([Segment("auto", "auto"), Segment("f16", "f16", reason="needs more VRAM"), Segment("q8_0", "q8_0")], "auto", id="g")
    app = Host(seg)

    async def script(pilot):
        start, end, _ = seg.spans()[1]
        await pilot.click("#g", offset=(start + 1, 0))
        clicked = seg.selected
        seg.focus()
        await pilot.press("right")  # skips the disabled f16
        return clicked, seg.selected

    assert run(app, script) == ("auto", "q8_0")
    assert app.seen == [("segmented", "q8_0")]


def test_fit_gauge_colour_follows_fit_status_not_text():
    gauge = FitGauge(id="g")
    app = Host(gauge)

    async def script(pilot):
        gauge.show(9, 22, "gpu", "too big")  # the caption cannot change the colour
        ok = gauge.colour()
        gauge.show(30, 22, "does-not-fit", "fits")
        bad = gauge.colour()
        variables = app.theme_variables
        return ok == variables["success"], bad == variables["error"]

    assert run(app, script) == (True, True)


def test_safe_static_renders_markup_and_click_actions_literally():
    hostile = "[@click=app.quit]press[/] [b]bold[/b] [link=https://x.example]l[/link] \x1b[2J" + chr(0x202E)
    static = SafeStatic(hostile, id="s")
    app = Host(static)

    async def script(pilot):
        await pilot.click("#s", offset=(2, 0))
        await pilot.pause()
        return app.is_running, static.render()

    running, shown = run(app, script)
    plain = shown.plain if isinstance(shown, Text) else str(shown)
    assert running  # the click did not reach app.quit
    assert "[@click=app.quit]" in plain and "[b]bold[/b]" in plain and "\x1b" not in plain and chr(0x202E) not in plain


def test_tool_card_shows_running_timer_then_result_and_toggles_on_click():
    card = ToolCard("c1", Text("Run pytest"), id="card")
    app = Host(card)

    async def script(pilot):
        header = card.query_one(".header", SafeStatic)
        running = str(header.render())
        card.finish(Text("* Run pytest  ok  3.2s"), [Text("a"), Text("... 9 more lines")], [Text(str(i)) for i in range(10)])
        await pilot.pause()
        folded = len(card.body.children)
        await pilot.click("#card", offset=(2, 0))
        await pilot.pause()
        return running, folded, len(card.body.children), card.has_class("running")

    running, folded, expanded, still_running = run(app, script)
    assert "running" in running and folded == 2 and expanded == 10 and not still_running


def test_diff_view_cuts_at_limit_and_enter_expands():
    card = ToolCard("c1", Text("Edit"), id="card")
    app = Host(card)

    async def script(pilot):
        card.finish(Text("* Edit ok"), [Text("+a"), Text("... 2 more lines")], [Text("+a"), Text("+b"), Text("+c")])
        await pilot.pause()
        card.focus()
        await pilot.press("enter")
        await pilot.pause()
        return len(card.body.children)

    assert run(app, script) == 3
