"""Tests for `halhub.policy.taint`: session taint levels and what moves them (01 REQ-SEC-32)."""

from __future__ import annotations

import pytest

from halhub.policy.model import Taint
from halhub.policy.taint import SURFACES, TaintChange, TaintTracker, start_level


@pytest.mark.parametrize("kind", ["code", "box", "remote", "hub"])
def test_sandboxed_remote_and_hub_sessions_start_untrusted(kind):
    assert TaintTracker(kind).level is Taint.UNTRUSTED


@pytest.mark.parametrize("kind", ["chat", "headless", "routine"])
def test_other_sessions_start_clean(kind):
    assert TaintTracker(kind).level is Taint.CLEAN


def test_resumed_session_starts_untrusted_and_resets_to_its_kind():
    tracker = TaintTracker("chat", resumed=True)
    assert tracker.level is Taint.UNTRUSTED
    assert tracker.human_message("tui") == TaintChange(Taint.UNTRUSTED, Taint.CLEAN, "human_message", "tui")
    assert start_level("chat", resumed=True) is Taint.UNTRUSTED


def test_untrusted_output_raises_clean_to_untrusted_once():
    tracker = TaintTracker("chat")
    assert tracker.raise_to(Taint.UNTRUSTED, cause="untrusted_output") == TaintChange(
        Taint.CLEAN, Taint.UNTRUSTED, "untrusted_output"
    )
    assert tracker.raise_to(Taint.UNTRUSTED, cause="untrusted_output") is None


def test_raise_never_lowers_the_level():
    tracker = TaintTracker("chat")
    tracker.raise_to(Taint.FLAGGED, cause="guard_flag")
    assert tracker.raise_to(Taint.UNTRUSTED, cause="untrusted_output") is None
    assert tracker.level is Taint.FLAGGED


@pytest.mark.parametrize("surface", ["tui", "acp", "telegram", "console"])
def test_human_message_resets_flagged_to_the_starting_level(surface):
    tracker = TaintTracker("code")
    tracker.raise_to(Taint.FLAGGED, cause="guard_flag")
    change = tracker.human_message(surface)
    assert change == TaintChange(Taint.FLAGGED, Taint.UNTRUSTED, "human_message", surface)
    assert tracker.level is Taint.UNTRUSTED


def test_voice_message_does_not_reset_flagged():
    tracker = TaintTracker("chat")
    tracker.raise_to(Taint.FLAGGED, cause="guard_flag")
    assert tracker.human_message("voice") is None and tracker.level is Taint.FLAGGED


def test_voice_message_resets_untrusted():
    tracker = TaintTracker("chat")
    tracker.raise_to(Taint.UNTRUSTED, cause="untrusted_output")
    assert tracker.human_message("voice") is not None and tracker.level is Taint.CLEAN


@pytest.mark.parametrize("surface", ["headless", "routine"])
def test_headless_and_routine_text_never_resets(surface):
    tracker = TaintTracker("chat")
    tracker.raise_to(Taint.UNTRUSTED, cause="untrusted_output")
    assert tracker.human_message(surface) is None and tracker.level is Taint.UNTRUSTED


def test_message_at_the_starting_level_is_no_change():
    assert TaintTracker("chat").human_message("tui") is None


def test_unknown_surface_is_refused_and_changes_nothing():
    tracker = TaintTracker("chat")
    tracker.raise_to(Taint.FLAGGED, cause="guard_flag")
    with pytest.raises(ValueError, match="surface"):
        tracker.human_message("tty")
    assert tracker.level is Taint.FLAGGED


def test_surfaces_are_the_broker_hook_surfaces():
    assert sorted(SURFACES) == ["acp", "console", "headless", "routine", "telegram", "tui", "voice"]
