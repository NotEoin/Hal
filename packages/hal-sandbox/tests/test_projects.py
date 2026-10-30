"""Tests for per-project sandbox choices."""

import json
import stat

import pytest

from halsandbox import projects
from halsandbox.errors import SandboxError


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))


def _set(p, choice="unmask", word=projects.CONFIRM_WORD):
    projects.set_choice(p, choice, is_tty=lambda: True, ask=lambda _: word)


def test_unmask_is_remembered_per_project(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _set(a)
    assert projects.is_set(a, "unmask") and not projects.is_set(b, "unmask")
    assert not projects.is_set(a, "none_consent")


def test_choices_need_a_terminal(tmp_path):
    with pytest.raises(projects.NotATerminal):
        projects.set_choice(tmp_path, "unmask", is_tty=lambda: False, ask=lambda _: projects.CONFIRM_WORD)
    assert not projects.is_set(tmp_path, "unmask")


def test_a_wrong_confirmation_changes_nothing(tmp_path):
    with pytest.raises(SandboxError, match="not confirmed"):
        _set(tmp_path, word="y")
    assert not projects.projects_file().exists()


def test_forget_revokes_every_choice_and_file_is_private(tmp_path):
    _set(tmp_path)
    _set(tmp_path, "none_consent")
    assert stat.S_IMODE(projects.projects_file().stat().st_mode) == 0o600
    assert projects.forget(tmp_path) and not projects.is_set(tmp_path, "none_consent")
    assert not projects.forget(tmp_path)


def test_a_corrupt_file_fails_loudly(tmp_path):
    projects.projects_file().parent.mkdir(parents=True)
    projects.projects_file().write_text("{nope")
    with pytest.raises(SandboxError, match="unreadable"):
        projects.is_set(tmp_path, "unmask")


def test_file_holds_only_booleans_per_project(tmp_path):
    _set(tmp_path)
    data = json.loads(projects.projects_file().read_text())
    assert data == {str(tmp_path.resolve()): {"unmask": True}}


def test_none_asks_once_per_project(tmp_path):
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return projects.CONFIRM_WORD

    projects.require_none_consent(tmp_path, is_tty=lambda: True, ask=ask)
    projects.require_none_consent(
        tmp_path, is_tty=lambda: False, ask=ask
    )  # recorded: no prompt, no tty needed
    assert len(asked) == 1 and projects.is_set(tmp_path, "none_consent")


def test_none_without_terminal_refuses(tmp_path):
    with pytest.raises(projects.NotATerminal):
        projects.require_none_consent(tmp_path, is_tty=lambda: False, ask=lambda _: projects.CONFIRM_WORD)
    assert not projects.is_set(tmp_path, "none_consent")
