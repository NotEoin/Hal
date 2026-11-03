"""Tests for the full-screen welcome (`halcore.tui.welcome`): header, tip and recent sessions."""

from __future__ import annotations

import types
from datetime import UTC, datetime

from test_tui_agentapp import LOOK, drive, settle, until

from halcore import sessionscmd
from halcore.tui import welcome
from halcore.tui.slash import TuiState


def fake_state():
    return TuiState(client=None, sid="0123456789ab", cwd="/home/someone/proj", renderer=None)


def test_tip_is_the_same_all_day_and_changes_the_next():
    morning, evening = datetime(2026, 3, 4, 1, tzinfo=UTC), datetime(2026, 3, 4, 23, tzinfo=UTC)
    assert welcome.tip(morning) == welcome.tip(evening) != welcome.tip(datetime(2026, 3, 5, tzinfo=UTC))


def test_short_path_writes_the_home_folder_as_a_tilde():
    assert welcome.short_path("/home/someone/proj", "/home/someone") == "~/proj"
    assert welcome.short_path("/home/someoneelse", "/home/someone") == "/home/someoneelse"


def test_short_screen_uses_the_one_row_eye_and_tall_screen_the_art():
    state, session = fake_state(), types.SimpleNamespace(runtime="runsc", iso=None, project=None)
    short = welcome.header(state, session, LOOK, welcome.WELCOME_ART_MIN_ROWS - 1).plain.splitlines()
    tall = welcome.header(state, session, LOOK, welcome.WELCOME_ART_MIN_ROWS).plain.splitlines()
    assert len(short) == 3 and short[0].strip().startswith("o  hal code")
    assert len(tall) == len(welcome.ART) and "hal code" in tall[1]


def test_hostile_model_name_cannot_reach_the_welcome_as_control_text():
    state = fake_state()
    state.status.model = "evil\x1b]0;pwned\x07\u202e"
    text = welcome.header(state, types.SimpleNamespace(), LOOK, 40).plain
    assert "\x1b" not in text and "\x07" not in text and "\u202e" not in text


def add_sessions(controller):
    sessionscmd.write_meta(controller.store, "aaaaaaaaaaaa", title="Fix the flaky add test", mode="code")


def test_welcome_lists_recent_sessions_and_resumes_on_click(tmp_path):
    async def script(pilot, app, agent):
        sent = []
        app.send = sent.append
        assert await until(pilot, lambda: app.query(welcome.RecentRow))
        await pilot.click(welcome.RecentRow)
        await settle(pilot)
        return [r.item.label for r in app.query(welcome.RecentRow)], sent

    assert drive(tmp_path, script, setup=add_sessions) == (["Fix the flaky add test"], ["/resume aaaaaaaaaaaa"])


def test_welcome_without_sessions_has_no_recent_heading(tmp_path):
    async def script(pilot, app, agent):
        assert await until(pilot, lambda: app.query(welcome.WelcomeView))
        await settle(pilot)
        return len(app.query(".welcome-heading")), len(app.query(welcome.RecentRow))

    assert drive(tmp_path, script) == (0, 0)
