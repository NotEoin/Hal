"""Tests for `halcore.levels`: `hal lockdown` and `hal paranoia` (RESOLUTIONS I2, I5)."""

import io
import tomllib

import pytest
from halkit.testing import TempHalHome

from halcore import levels


class Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def home(monkeypatch):
    monkeypatch.delenv("HAL_HARNESS", raising=False)
    with TempHalHome() as h:
        yield h


def test_set_key_changes_only_its_line_and_keeps_comments():
    text = '[hal]\nvault = "v"\n\n[sandbox]\nlockdown = "standard"  # default\nruntime = "auto"\n'
    new = levels.set_key(text, "sandbox", "lockdown", "strict")
    assert 'lockdown = "strict"  # default' in new and new.count("\n") == text.count("\n")
    assert tomllib.loads(new)["sandbox"] == {"lockdown": "strict", "runtime": "auto"}


def test_set_key_adds_a_missing_key_and_a_missing_table():
    assert tomllib.loads(levels.set_key("[sandbox]\nruntime = 'runc'\n", "sandbox", "lockdown", "off"))["sandbox"]["lockdown"] == "off"
    assert tomllib.loads(levels.set_key("[hal]\nx = 1", "policy", "paranoia", "max"))["policy"]["paranoia"] == "max"
    assert tomllib.loads(levels.set_key("", "policy", "paranoia", "max"))["policy"]["paranoia"] == "max"


def test_raising_lockdown_needs_no_confirmation_and_is_written(home):
    home.config.write_text('[sandbox]\nlockdown = "relaxed"\n')
    out = io.StringIO()
    assert levels.run(levels.DIALS["lockdown"], ["strict"], stdin=Tty(), out=out) == 0
    assert tomllib.loads(home.config.read_text())["sandbox"]["lockdown"] == "strict"


def test_lowering_lockdown_needs_the_level_typed_back(home):
    dial = levels.DIALS["lockdown"]
    assert levels.run(dial, ["off"], stdin=Tty(), out=io.StringIO(), confirm=lambda _p: "yes") == 3
    assert not home.config.exists() or "off" not in home.config.read_text()
    assert levels.run(dial, ["off"], stdin=Tty(), out=io.StringIO(), confirm=lambda _p: "off") == 0
    assert dial.current() == "off"


def test_a_harness_or_a_pipe_cannot_change_a_level(home, monkeypatch):
    dial = levels.DIALS["paranoia"]
    assert levels.run(dial, ["off"], stdin=io.StringIO(), out=io.StringIO()) == 3
    monkeypatch.setenv("HAL_HARNESS", "claude")
    assert levels.run(dial, ["max"], stdin=Tty(), out=io.StringIO()) == 3
    assert dial.current() == "balanced"


def test_paranoia_is_written_to_the_policy_file(home):
    assert levels.run(levels.DIALS["paranoia"], ["high"], stdin=Tty(), out=io.StringIO()) == 0
    assert tomllib.loads(levels.paths.policy_file().read_text())["policy"]["paranoia"] == "high"


def test_unknown_level_is_a_usage_error(home):
    assert levels.run(levels.DIALS["lockdown"], ["paranoid"], stdin=Tty(), out=io.StringIO()) == 2


def test_showing_a_level_lists_every_level_and_marks_the_current(home):
    out = io.StringIO()
    assert levels.run(levels.DIALS["lockdown"], [], out=out) == 0
    text = out.getvalue()
    assert "lockdown: standard" in text and "* standard" in text and "strict" in text
