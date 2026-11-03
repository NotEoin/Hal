"""Tests for the Ctrl+P palette sources (`halcore.tui.palette`): pure, no screen."""

from __future__ import annotations

from pathlib import Path

from halcore import sessionscmd
from halcore.acp.records import RecordStore
from halcore.tui import palette
from halcore.tui.choices import Choice
from halcore.tui.slash import TuiState


def state(tmp_path: Path, **kw) -> TuiState:
    return TuiState(client=None, sid="0123456789ab", cwd="/work", renderer=None, records=RecordStore(tmp_path / "s"), **kw)


def test_sessions_are_newest_first_and_leave_out_the_current_one(tmp_path):
    st = state(tmp_path)
    for sid, title in (("0123456789ab", "this one"), ("aaaaaaaaaaaa", "older"), ("bbbbbbbbbbbb", "newer")):
        sessionscmd.write_meta(st.records, sid, title=title, mode="code")
    rows = palette.sessions(st)
    assert [(r.label, r.value) for r in rows] == [("newer", "/resume bbbbbbbbbbbb"), ("older", "/resume aaaaaaaaaaaa")]


def test_hostile_session_title_is_cleaned(tmp_path):
    st = state(tmp_path)
    sessionscmd.write_meta(st.records, "aaaaaaaaaaaa", title="evil\x1b]0;pwned\x07 \u202etitle", mode="code")
    label = palette.sessions(st)[0].label
    assert "\x1b" not in label and "\u202e" not in label and "\x07" not in label


def test_models_offer_a_listing_row_until_the_catalog_is_loaded(tmp_path):
    assert [r.value for r in palette.models(state(tmp_path))] == ["/model"]
    loaded = state(tmp_path, choices=[Choice("local", "ollama", "gemma", "gemma:latest"), Choice("x", "y", "nope", "")])
    assert [(r.label, r.value) for r in palette.models(loaded)] == [("gemma", "/model gemma")]


def test_files_only_count_in_code_with_a_working_copy(tmp_path):
    kinds = {i.kind for i in palette.gather(state(tmp_path, surface="chat"), animations=False, workdir=tmp_path, file_names=["a.py"])}
    assert "file" not in kinds
    code = palette.gather(state(tmp_path), animations=False, workdir=tmp_path, file_names=["a.py"])
    assert ("file", "@a.py ") in {(i.kind, i.value) for i in code}


def test_mode_settings_are_code_only_and_the_image_command_is_composed(tmp_path):
    chat = palette.settings(state(tmp_path, surface="chat"), animations=True)
    assert not [i for i in chat if i.label.startswith("mode:")]
    image = next(i for i in palette.commands(state(tmp_path)) if i.value == "/image")
    assert image.action == "compose"


def test_search_caps_rows_and_an_empty_query_keeps_the_order(tmp_path):
    items = palette.gather(state(tmp_path), animations=False, workdir=None)
    assert palette.search("", items) == items[: palette.PALETTE_ROWS]
    assert palette.search("theme dark", items)[0].value == "/theme dark"


def test_when_shows_minutes_and_rejects_odd_stamps():
    assert palette.when("2026-11-02T18:40:12Z") == "2026-11-02 18:40"
    assert palette.when("yesterday") == ""
