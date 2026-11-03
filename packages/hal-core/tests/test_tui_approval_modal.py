"""Security tests for the full-screen approval modal: focus, arm delay, scroll gate, two-step allow, vanish.

The modal is the human's gate for calls that leave the box, so every rule of its module header has
a test here. The bridge is a fake that records what `consume` received.
"""

from __future__ import annotations

import asyncio
import types

from textual.app import App
from textual.widgets import Button

from halcore.tui import approvalmodal
from halcore.tui.approvalmodal import GONE, ApprovalModal, ModalAsker

RLO = chr(0x202E)
REQUEST = {
    "id": "apr_1", "tool": "shell.run", "tier": "exec", "pack": "hal-tools", "reason": "exec tier asks",
    "display": '{"command": "rm -rf build/", "cwd": "/work"}', "scopes": ["once", "session"],
    "key": "sha256:3f9a0c12d4e8" + "0" * 52, "risk": "high", "risk_reason": "writes outside the workspace",
}  # fmt: skip
QUICK = 0.05  # arm delay for tests that are not about it


class Host(App):
    pass


def run_modal(request, script, *, arm_s=QUICK, size=(110, 40)):
    """Pushes an `ApprovalModal`, runs `script(pilot, modal)` and returns its dismiss value."""
    results = []

    async def go():
        app = Host()
        async with app.run_test(size=size) as pilot:
            modal = ApprovalModal(request, arm_s=arm_s)
            app.push_screen(modal, results.append)
            await pilot.pause()
            await script(pilot, modal)
            for _ in range(3):
                await pilot.pause()

    asyncio.run(go())
    return results[0] if results else None


async def armed(pilot, modal):
    await asyncio.sleep(modal.arm_s + 0.05)
    await pilot.pause()


def test_approval_modal_focuses_deny_and_enter_denies():
    async def script(pilot, modal):
        assert modal.focused.id == "deny"
        await armed(pilot, modal)
        await pilot.press("enter")

    assert run_modal(REQUEST, script) == ("n", "")


def test_allow_is_inert_during_the_arm_delay():
    async def script(pilot, modal):
        await pilot.press("y")
        assert modal.focused.id == "deny" and modal.query_one("#allow-once", Button).disabled
        await pilot.click("#allow-once")
        await pilot.press("enter")

    assert run_modal(REQUEST, script, arm_s=5.0) == ("n", "")


def test_typeahead_before_the_modal_is_dropped():
    from textual import events

    async def script(pilot, modal):
        await armed(pilot, modal)
        early = events.Key("y", "y")
        early._time = modal.opened_at - 1  # sent before the modal opened
        modal.post_message(early)
        await pilot.pause()
        assert modal.focused.id == "deny"
        await pilot.press("enter")

    assert run_modal(REQUEST, script) == ("n", "")


def test_allow_disabled_until_arguments_scrolled_to_end():
    long = {**REQUEST, "display": "\n".join(f"arg{i}: value" for i in range(60))}

    async def script(pilot, modal):
        await armed(pilot, modal)
        assert modal.query_one("#allow-once", Button).disabled and modal.query_one("#gate").display
        await pilot.press("y")
        assert modal.focused.id == "deny"
        modal.query_one("#args").scroll_end(animate=False)
        await pilot.pause()
        modal.check_seen()
        await pilot.pause()
        assert not modal.query_one("#allow-once", Button).disabled
        await asyncio.sleep(approvalmodal.TYPING_GAP_S)
        await pilot.press("y")
        assert modal.focused.id == "allow-once"
        await pilot.press("enter")

    assert run_modal(long, script) == ("y", "")


def test_y_moves_focus_and_needs_enter_to_allow_once():
    async def script(pilot, modal):
        await armed(pilot, modal)
        await pilot.press("y")
        await pilot.pause()
        assert modal.is_attached and modal.focused.id == "allow-once"  # not allowed yet
        await pilot.press("enter")

    assert run_modal(REQUEST, script) == ("y", "")


def test_a_full_click_on_allow_once_allows():
    async def script(pilot, modal):
        await armed(pilot, modal)
        await pilot.click("#allow-once")

    assert run_modal(REQUEST, script) == ("y", "")


def test_session_button_absent_when_scope_not_offered_and_a_denies():
    once_only = {**REQUEST, "scopes": ["once"]}

    async def script(pilot, modal):
        await armed(pilot, modal)
        assert not modal.query("#allow-session")
        await pilot.press("a")
        assert modal.focused.id == "deny"
        await pilot.press("enter")

    assert run_modal(once_only, script) == ("n", "")
    assert approvalmodal.approval.answer_to_option("a", once_only) == "deny"  # a forged `a` is a deny


def test_esc_and_ctrl_c_deny():
    async def esc(pilot, modal):
        await pilot.press("escape")

    async def ctrl_c(pilot, modal):
        await armed(pilot, modal)
        await pilot.press("y", "ctrl+c")

    assert run_modal(REQUEST, esc) == ("n", "")
    assert run_modal(REQUEST, ctrl_c) == ("n", "")


def test_note_is_returned_with_a_deny_and_an_empty_note_is_a_plain_deny():
    async def note(pilot, modal):
        await pilot.press("e")
        await pilot.press(*"use pytest -x", "enter")

    async def empty(pilot, modal):
        await pilot.press("e", "enter")

    assert run_modal(REQUEST, note) == ("e", "use pytest -x")
    assert run_modal(REQUEST, empty) == ("n", "")


def test_hostile_display_text_is_shown_escaped_and_cannot_add_buttons():
    hostile = {**REQUEST, "tool": "[@click=app.quit]x[/]", "display": "[b]Allow once  y[/b] \x1b[2J" + RLO,
               "reason": "[link=https://x.example]ok[/link]"}

    async def script(pilot, modal):
        texts = " ".join(str(w.render()) for w in modal.query("SafeStatic"))
        assert "[@click=app.quit]" in texts and "\x1b" not in texts and RLO not in texts and "\\u202e" in texts
        assert len(modal.query(Button)) == 4
        assert {b.id for b in modal.query(Button)} == {"deny", "deny-note", "allow-once", "allow-session"}
        await pilot.press("escape")

    assert run_modal(hostile, script) == ("n", "")


def test_modal_shows_key_prefix_and_leaves_the_box_label():
    async def script(pilot, modal):
        texts = " ".join(str(w.render()) for w in modal.query("SafeStatic"))
        assert "leaves the box" in texts and "key 3f9a0c12d4e8" in texts and "classifier: high" in texts
        await pilot.press("escape")

    run_modal(REQUEST, script)


class FakeBridge:
    """A bridge with scripted pending requests; `consume` records the answers."""

    def __init__(self, requests):
        self.requests = dict(requests)
        self.answers = []

    def request_for(self, rid):
        return self.requests.get(rid)

    def is_pending(self, rid):
        return rid in self.requests

    def consume(self, response):
        self.requests.pop(response["id"], None)
        self.answers.append((response["id"], response["result"]["outcome"]["optionId"]))
        return True


def run_asker(bridge, script, notes=None):
    async def go():
        app = Host()
        async with app.run_test(size=(110, 40)) as pilot:
            asker = ModalAsker(app, bridge, (notes if notes is not None else []).append, arm_s=QUICK, poll_s=0.02, ring=False)
            await script(pilot, app, asker)
            for _ in range(3):
                await pilot.pause()

    asyncio.run(go())


def test_modal_closes_without_answer_when_request_vanishes():
    bridge = FakeBridge({"hal-1": REQUEST})

    async def script(pilot, app, asker):
        await asker.send({"id": "hal-1"})
        await asyncio.sleep(0.05)
        assert isinstance(app.screen, ApprovalModal)
        bridge.requests.clear()  # answered with `hal approve` in another terminal
        await asyncio.sleep(0.1)
        await pilot.pause()
        assert not isinstance(app.screen, ApprovalModal)

    run_asker(bridge, script)
    assert bridge.answers == []


def test_note_is_queued_and_call_denied():
    bridge, notes = FakeBridge({"hal-1": REQUEST}), []

    async def script(pilot, app, asker):
        await asker.send({"id": "hal-1"})
        await asyncio.sleep(0.05)
        await pilot.press("e", *"try again", "enter")
        await asyncio.sleep(0.05)

    run_asker(bridge, script, notes)
    assert bridge.answers == [("hal-1", "deny")] and notes == ["try again"]


def test_two_pending_requests_are_answered_one_at_a_time():
    bridge = FakeBridge({"hal-1": REQUEST, "hal-2": {**REQUEST, "id": "apr_2", "scopes": ["once"]}})

    async def script(pilot, app, asker):
        await asker.send({"id": "hal-1"})
        await asker.send({"id": "hal-2"})
        await asyncio.sleep(0.1)
        await pilot.pause()
        first = app.screen
        assert "1 of 2" in str(first.query_one(".modal-title").render())
        await pilot.press("y", "enter")
        await asyncio.sleep(0.1)
        await pilot.pause()
        assert app.screen is not first and isinstance(app.screen, ApprovalModal)
        await pilot.press("enter")
        await asyncio.sleep(0.05)

    run_asker(bridge, script)
    assert bridge.answers == [("hal-1", "once"), ("hal-2", "deny")]


def test_deny_open_denies_the_visible_request():
    bridge = FakeBridge({"hal-1": REQUEST})

    async def script(pilot, app, asker):
        await asker.send({"id": "hal-1"})
        await asyncio.sleep(0.05)
        assert asker.deny_open()
        await asyncio.sleep(0.05)

    run_asker(bridge, script)
    assert bridge.answers == [("hal-1", "deny")]


def on_screen(widget, size):
    region = widget.region
    return region.width > 0 and region.height > 0 and region.right <= size[0] and region.bottom <= size[1]


def test_buttons_and_note_line_stay_on_screen_in_small_terminals():
    # regression: at 80x24 and 60x20 a long argument list pushed the buttons below the screen edge
    long = {**REQUEST, "display": "\n".join(f"arg{i}: value" for i in range(60))}
    for size in ((80, 24), (60, 20)):
        shown = {}

        async def script(pilot, modal, size=size, shown=shown):
            await armed(pilot, modal)
            await pilot.pause()
            shown["buttons"] = all(on_screen(b, size) for b in modal.query(Button))
            await pilot.press("e")
            for _ in range(3):
                await pilot.pause()
            shown["note"] = on_screen(modal.query_one("#note"), size)
            await pilot.press("ctrl+c")

        assert run_modal(long, script, size=size) == ("n", "")
        assert shown == {"buttons": True, "note": True}, size


def test_buttons_do_not_move_when_the_arguments_have_been_read():
    long = {**REQUEST, "display": "\n".join(f"arg{i}: value" for i in range(60))}

    async def script(pilot, modal):
        await armed(pilot, modal)
        before = modal.query_one("#deny", Button).region
        await pilot.press("end")
        for _ in range(3):
            await pilot.pause()
        assert modal.seen and not modal.query_one("#allow-once", Button).disabled
        assert modal.query_one("#deny", Button).region == before
        await pilot.press("n")

    assert run_modal(long, script) == ("n", "")


def test_arguments_that_fit_only_before_the_view_shrinks_still_need_scrolling():
    # the view shrinks to fit a 60x20 screen; eight lines fit the tallest view but not the shrunk one
    eight = {**REQUEST, "display": "\n".join(f"arg{i}: value" for i in range(8))}

    async def script(pilot, modal):
        await armed(pilot, modal)
        for _ in range(3):
            await pilot.pause()
        args = modal.query_one("#args")
        assert args.max_scroll_y > 0
        assert not modal.seen and modal.query_one("#allow-once", Button).disabled
        await pilot.press("n")

    assert run_modal(eight, script, size=(60, 20)) == ("n", "")


def test_typing_a_sentence_with_y_and_enter_into_the_modal_denies():
    # regression: "try it" then Enter typed into the open modal moved focus to Allow once and pressed it
    async def script(pilot, modal):
        await armed(pilot, modal)
        await pilot.press(*"try it", "enter")

    assert run_modal(REQUEST, script) == ("n", "")


def test_any_other_key_after_y_sends_focus_back_to_deny():
    async def script(pilot, modal):
        await armed(pilot, modal)
        await pilot.press("y")
        assert modal.focused.id == "allow-once"
        await asyncio.sleep(approvalmodal.TYPING_GAP_S)
        await pilot.press("o")
        assert modal.focused.id == "deny"
        await pilot.press("enter")

    assert run_modal(REQUEST, script) == ("n", "")


def test_space_does_not_press_an_allow_button():
    async def script(pilot, modal):
        await armed(pilot, modal)
        await pilot.press("y", "space")
        assert modal.focused.id == "deny"
        await asyncio.sleep(approvalmodal.TYPING_GAP_S)
        await pilot.press("n")

    assert run_modal(REQUEST, script) == ("n", "")


def test_y_right_after_scrolling_to_the_end_still_selects_allow():
    long = {**REQUEST, "display": "\n".join(f"arg{i}: value" for i in range(60))}

    async def script(pilot, modal):
        await armed(pilot, modal)
        await pilot.press("end")
        for _ in range(3):
            await pilot.pause()
        await pilot.press("y")
        assert modal.focused.id == "allow-once"
        await pilot.press("enter")

    assert run_modal(long, script) == ("y", "")
