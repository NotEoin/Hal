"""Pilot tests for the shared modals: picker, confirm, choice and help."""

from __future__ import annotations

import asyncio

from textual.app import App
from textual.widgets import Button, OptionList

from halcore.tui import screens, slash
from halcore.tui.slash import PickItem

ITEMS = [PickItem("local/qwen3:8b", "qwen3:8b", "5.0 GiB"), PickItem("local/gemma3:12b", "gemma3:12b"),
         PickItem("disk/big.gguf", "", "not served yet", disabled=True), PickItem("local/llama3:8b", "llama3:8b")]


class Host(App):
    pass


def run(screen, script, size=(100, 30)):
    """Pushes `screen` on a bare app, runs `script(pilot, screen)` and returns the modal's result."""
    results = []

    async def go():
        app = Host()
        async with app.run_test(size=size) as pilot:
            app.push_screen(screen, results.append)
            await pilot.pause()
            await script(pilot, screen)
            await pilot.pause()
        return results

    out = asyncio.run(go())
    return out[0] if out else "no result"


def test_picker_filters_marks_current_and_returns_clicked_row():
    async def script(pilot, screen):
        rows = [o.prompt.plain for o in screen.query_one(OptionList)._options]
        assert rows[1].startswith("* ")  # the current model is marked
        await pilot.press(*"llama")
        assert [i.value for i in screen.shown] == ["llama3:8b"]
        await pilot.click(OptionList, offset=(4, 0))

    assert run(screens.PickerModal("Model", ITEMS, "gemma3:12b"), script) == "llama3:8b"


def test_picker_ranks_the_best_fuzzy_match_first_and_bolds_its_letters():
    items = [PickItem("disk/gxexm.gguf", "x"), PickItem("local/qwen3:8b", "q"), PickItem("local/gemma3:12b", "g")]
    seen = {}

    async def script(pilot, screen):
        await pilot.press(*"gem")
        seen["shown"] = [i.value for i in screen.shown]
        first = screen.query_one(OptionList)._options[0].prompt
        seen["bold"] = "".join(first.plain[s.start : s.end] for s in first.spans if "bold" in str(s.style))
        await pilot.press("escape")

    run(screens.PickerModal("Model", items), script)
    assert seen["shown"] == ["g", "x"] and seen["bold"] == "gem"


def test_picker_enter_chooses_the_current_row_by_default():
    async def script(pilot, screen):
        await pilot.press("enter")

    assert run(screens.PickerModal("Model", ITEMS, "gemma3:12b"), script) == "gemma3:12b"


def test_picker_disabled_row_shows_reason_and_is_not_returned():
    async def script(pilot, screen):
        rows = [o.prompt.plain for o in screen.query_one(OptionList)._options]
        assert "not served yet" in rows[2]
        screen.choose(2)
        await pilot.pause()
        assert screen.is_attached  # still open
        await pilot.press("escape")

    assert run(screens.PickerModal("Model", ITEMS), script) is None


def test_picker_shows_loading_row_until_items_arrive():
    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        return ITEMS[:1]

    async def script(pilot, screen):
        assert screen.query("#loading")
        gate.set()
        for _ in range(20):
            await pilot.pause()
        assert not screen.query("#loading") and [i.value for i in screen.shown] == ["qwen3:8b"]
        await pilot.press("enter")

    assert run(screens.PickerModal("Model", slow), script) == "qwen3:8b"


def test_confirm_enter_means_no_by_default_and_esc_too():
    async def enter(pilot, screen):
        assert screen.focused.id == "choice-no"
        await pilot.press("enter")

    async def esc(pilot, screen):
        await pilot.press("escape")

    async def yes(pilot, screen):
        await pilot.click("#choice-yes")

    assert run(screens.ConfirmModal("Write these changes?"), enter) == "no"
    assert run(screens.ConfirmModal("Write these changes?"), esc) == "no"
    assert run(screens.ConfirmModal("Write these changes?"), yes) == "yes"


def test_choice_modal_starts_on_the_safe_button():
    choices = [screens.Choice("Keep editing", "keep"), screens.Choice("Discard and quit", "discard", "error")]

    async def script(pilot, screen):
        assert isinstance(screen.focused, Button) and screen.focused.id == "choice-keep"
        await pilot.press("enter")

    assert run(screens.ChoiceModal("Unsaved changes", "2 roles changed", choices, "keep"), script) == "keep"


def test_help_screen_lists_only_the_surface_commands():
    rows = [(usage, text) for usage, text in slash.commands_for("chat").values()]
    help_screen = screens.HelpScreen("Help", [screens.HelpSection("Commands", rows)])

    async def script(pilot, screen):
        text = " ".join(str(w.render()) for w in screen.query("SafeStatic"))
        assert "/model" in text and "/accept" not in text
        await pilot.press("escape")

    assert run(help_screen, script) is None


def test_a_long_picker_list_stays_inside_small_screens():
    # regression: a 20-row list pushed past the bottom of a 24-row screen
    many = [PickItem(f"model {i}", str(i)) for i in range(40)]
    for size in ((80, 24), (60, 20)):
        bottoms = []

        async def script(pilot, screen, bottoms=bottoms):
            bottoms.append(screen.query_one(OptionList).region.bottom)
            await pilot.press("escape")

        assert run(screens.PickerModal("Model", many), script, size=size) is None
        assert bottoms[0] <= size[1], size


def test_dialog_buttons_are_flat_one_row_and_the_focused_one_is_filled():
    choices = [screens.Choice("Keep editing", "keep"), screens.Choice("Discard and quit", "discard", "error")]
    seen = {}

    async def script(pilot, screen):
        buttons = list(screen.query(Button))
        seen["heights"] = [b.region.height for b in buttons]
        seen["focused"] = buttons[0].styles.background != buttons[1].styles.background
        seen["border"] = screen.query_one(".modal-box").styles.border_top[0]
        await pilot.press("escape")

    run(screens.ChoiceModal("Unsaved changes", "2 roles changed", choices, "keep"), script)
    assert seen == {"heights": [1, 1], "focused": True, "border": "round"}


def test_plan_card_keeps_the_heavy_attention_border():
    seen = []

    async def script(pilot, screen):
        seen.append(screen.query_one(".modal-box").styles.border_top[0])
        await pilot.press("escape")

    run(screens.ChoiceModal("Run this plan?", "1. step", [screens.Choice("Keep planning", "keep")], "keep", attention=True), script)
    assert seen == ["heavy"]


def overlay_layout(screen, size=(80, 24)):
    """The focused filter's height and border, and the list's height against its option count."""
    shown = {}

    async def script(pilot, modal):
        for _ in range(5):
            await pilot.pause()
        field = modal.query_one("#filter")
        options = modal.query_one("#options")
        shown.update(focused=field.has_focus, height=field.region.height, border=field.styles.border_top[0])
        shown.update(rows=options.virtual_size.height, count=options.option_count)
        await pilot.press("escape")

    run(screen, script, size=size)
    return shown


def test_palette_rows_are_one_line_and_its_search_line_has_no_box():
    # regression: the focused Input drew Textual's tall border over the one-row search line,
    # hiding the placeholder, and long notes wrapped rows onto two lines
    from halcore.tui import palette
    from halcore.tui.screens import CommandPalette

    note = "start a fresh conversation in the same box with a long description " * 2

    async def load():
        return [palette.PaletteItem("command", f"/c{i}", note, "send", f"/c{i}") for i in range(5)]

    shown = overlay_layout(CommandPalette(load))
    assert shown["focused"] and shown["height"] == 1 and shown["border"] in ("", "none")
    assert shown["rows"] == shown["count"] == 5


def test_history_search_line_has_no_box_and_long_messages_take_one_row():
    from halcore.tui.screens import HistorySearch

    items = [PickItem("fix the add test and then " * 6, str(i)) for i in range(3)]
    shown = overlay_layout(HistorySearch(items))
    assert shown["focused"] and shown["height"] == 1 and shown["border"] in ("", "none")
    assert shown["rows"] == shown["count"] == 3
