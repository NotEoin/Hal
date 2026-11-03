"""When the full-screen UI runs and how it falls back: terminal checks, start failures, light headless imports."""

from __future__ import annotations

import io
import subprocess
import sys

import pytest

from halcore.tui import fallback

IMPORT_TIMEOUT_S = 60


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_no_tty_uses_line_ui():
    ok, reason = fallback.can_run_textual(io.StringIO(), Tty(), {"TERM": "xterm-256color"})
    assert not ok and "not a terminal" in reason


def test_term_dumb_or_unset_uses_line_ui():
    assert fallback.can_run_textual(Tty(), Tty(), {"TERM": "dumb"}) == (False, "TERM is dumb")
    assert fallback.can_run_textual(Tty(), Tty(), {}) == (False, "TERM is unset")


def test_interface_line_setting_forces_line_ui():
    ok, reason = fallback.can_run_textual(Tty(), Tty(), {"TERM": "xterm-256color"}, "line")
    assert not ok and "line" in reason


def test_a_capable_terminal_runs_the_full_screen():
    assert fallback.can_run_textual(Tty(), Tty(), {"TERM": "xterm-256color"}) == (True, "")


def test_hal_tui_environment_variable_sets_the_interface(monkeypatch):
    from halkit import config as kitconfig

    from halcore.tui.theme import UI_TABLE

    monkeypatch.setenv("HAL_TUI", "line")
    assert kitconfig.load(UI_TABLE, environ={"HAL_TUI": "line"})["interface"] == "line"


def test_tune_screen_falls_back_when_the_app_fails_before_its_first_draw(monkeypatch, capsys):
    from test_tune import CARD_24GB, SHOW, TAGS

    from halcore import tune, tuneapp, tuneui
    from halkit import hardware
    from halkit.testing import TempHalHome
    from halkit.testing.ollama import FakeOllama

    opened = []
    monkeypatch.setattr(fallback, "can_run_textual", lambda *a, **k: (True, ""))
    monkeypatch.setattr(tuneapp.TuneApp, "run", lambda self, **kw: (_ for _ in ()).throw(RuntimeError("no driver")))
    monkeypatch.setattr(tuneui, "run_line_screen", lambda state, look: opened.append(state) or 0)
    with TempHalHome() as home, FakeOllama(TAGS, SHOW) as server:
        home.config.write_text(f'[models.sources.local]\nkind = "ollama"\nurl = "{server.url}"\n')
        assert tune.run_screen(hardware.from_dict(CARD_24GB)) == 0
    assert len(opened) == 1 and opened[0].sources
    assert "full-screen UI unavailable: no driver; using the line UI" in capsys.readouterr().err


def test_a_failure_after_the_first_draw_is_not_hidden(monkeypatch):
    from test_tune import CARD_24GB, SHOW, TAGS

    from halcore import tune, tuneapp
    from halkit import hardware
    from halkit.testing import TempHalHome
    from halkit.testing.ollama import FakeOllama

    def crash(self, **kw):
        self.mounted = True
        raise RuntimeError("bug after mount")

    monkeypatch.setattr(fallback, "can_run_textual", lambda *a, **k: (True, ""))
    monkeypatch.setattr(tuneapp.TuneApp, "run", crash)
    with TempHalHome() as home, FakeOllama(TAGS, SHOW) as server:
        home.config.write_text(f'[models.sources.local]\nkind = "ollama"\nurl = "{server.url}"\n')
        with pytest.raises(RuntimeError, match="bug after mount"):
            tune.run_screen(hardware.from_dict(CARD_24GB))


@pytest.mark.parametrize("module", ["halcore.headless", "halcore.acpcmd", "halcore.chat", "halcore.tune", "halcore.code"])
def test_headless_acp_and_one_shot_paths_do_not_import_textual(module):
    code = f"import sys, {module}; print('textual' in sys.modules)"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=IMPORT_TIMEOUT_S, check=True)
    assert done.stdout.strip() == "False"
