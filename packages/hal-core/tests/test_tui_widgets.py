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
        header = card.query_one(".header")
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


def test_toast_shows_markup_and_escapes_literally():
    app = Host()

    async def script(pilot):
        widgets.toast(app, "[@click=app.quit]x[/] \x1b[2J", title="[b]t[/b]")
        await pilot.pause()
        note = list(app._notifications)[-1]
        return note.message, note.title, note.markup

    message, title, markup = run(app, script)
    assert message.startswith("[@click=app.quit]") and "\x1b" not in message and title == "[b]t[/b]" and markup is False


def test_wheel_moves_a_slider_only_while_it_has_focus():
    # regression: wheeling down a page of sliders changed whichever value the pointer passed over
    from textual import events

    slider = Slider(5, 0, 10, 1, id="s")
    app = Host(slider)

    async def script(pilot):
        def wheel(cls):
            r = slider.region
            x, y = r.x + 2, r.y
            app.post_message(cls(None, x, y, 0, 1, 0, False, False, False, screen_x=x, screen_y=y))

        slider.blur()
        await pilot.pause()
        wheel(events.MouseScrollUp)
        await pilot.pause()
        unfocused = slider.value
        slider.focus()
        await pilot.pause()
        wheel(events.MouseScrollUp)
        await pilot.pause()
        return unfocused, slider.value

    assert run(app, script) == (5, 6)


def test_a_click_on_the_value_opens_typed_entry_instead_of_jumping_to_the_top():
    # regression: a click on the number right of the track set the slider to its highest value
    slider = Slider(5, 0, 10, 1, title="Steps", id="s")
    app = Host(slider)

    async def script(pilot):
        await pilot.click("#s", offset=(slider.size.width - 2, 0))
        await pilot.pause()
        opened = isinstance(app.screen, widgets.ValueEntry)
        await pilot.press(*"7", "enter")
        await pilot.pause()
        return opened, slider.value, app.seen[-1]

    assert run(app, script) == (True, 7, ("slider", 7, True, False))


def test_typed_value_outside_the_range_or_not_a_number_changes_nothing():
    slider = Slider(5, 0, 10, 1, id="s")
    app = Host(slider)

    async def script(pilot):
        slider.focus()
        for typed in ("42", "lots"):
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press(*typed, "enter")
            await pilot.pause()
        return slider.value, app.seen, len(app._notifications)

    value, seen, notes = run(app, script)
    assert value == 5 and seen == [] and notes == 2


def test_tool_row_is_one_line_with_the_body_closed_until_clicked():
    from halcore.tui.render import ToolRow

    card = ToolCard("c1", Text("Read src/calc.py"), id="card")
    app = Host(card)

    async def script(pilot):
        card.finish(Text("unused"), [Text("a")], [Text("a"), Text("b")], ToolRow("Read", "src/calc.py", True, 0.3))
        await pilot.pause()
        closed = (card.region.height, str(card.query_one(".header").render()))
        await pilot.click("#card", offset=(2, 0))
        await pilot.pause()
        return closed, card.region.height, card.expanded

    (height, row), opened, expanded = run(app, script)
    assert height == 1 and "Read" in row and "src/calc.py" in row and "0.3s" in row
    assert opened == 3 and expanded


def test_failed_tool_opens_its_body_by_itself():
    from halcore.tui.render import ToolRow

    card = ToolCard("c1", Text("Run pytest"), id="card")
    app = Host(card)

    async def script(pilot):
        card.finish(Text("unused"), [Text("E boom")], [Text("E boom")], ToolRow("Run", "pytest", False, 1.0))
        await pilot.pause()
        return card.open, card.region.height, str(card.query_one(".header").render())

    opened, height, row = run(app, script)
    assert opened and height == 2 and "FAIL" in row


def test_long_tool_target_is_clipped_not_wrapped():
    from halcore.tui.render import ToolRow

    target = "src/" + "very_long_directory_name/" * 8 + "file.py"
    card = ToolCard("c1", Text("Read " + target), id="card")
    app = Host(card)

    async def script(pilot):
        card.finish(Text("unused"), [], [], ToolRow("Read", target, True, 0.1))
        await pilot.pause()
        return card.region.height, str(card.query_one(".header").render())

    height, row = run(app, script)
    assert height == 1 and "..." in row and "0.1s" in row


def test_tool_rows_line_up_their_time_column_with_or_without_a_body_or_a_failure():
    # regression: a row without a body, or with FAIL instead of ok, drew its time further right
    from halcore.tui.render import ToolRow

    cards = [ToolCard(f"c{i}", Text("Read a.py"), id=f"card{i}") for i in range(3)]
    app = Host(*cards)

    async def script(pilot):
        cards[0].finish(Text(""), [Text("a")], [Text("a")], ToolRow("Read", "a.py", True, 0.3))
        cards[1].finish(Text(""), [], [], ToolRow("Read", "b.py", True, 0.3))
        cards[2].finish(Text(""), [], [], ToolRow("Run", "pytest", False, 0.3))
        await pilot.pause()
        return [str(card.query_one(".header").render()) for card in cards]

    rows = run(app, script)
    assert len({row.index("0.3s") for row in rows}) == 1
    assert len({len(row.rstrip("\n")) for row in rows}) == 1


def test_row_tail_keeps_empty_result_and_fold_slots_at_their_width():
    roles = {}
    running = widgets.row_tail(None, roles, "3s", None, None).plain
    done = widgets.row_tail(None, roles, "3s", True, False).plain
    assert running.index("3s") == done.index("3s") and len(running) == len(done) == len("3s  FAIL  >")
