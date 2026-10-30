"""Tests for `hal code accept|export|reject|ls` and `hal scratch clean`."""

import pytest
from halsandbox import isolated, scratch

from halcore import code, scratch as scratch_cmd


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    p = tmp_path / "proj"
    p.mkdir()
    (p / "a.txt").write_text("one\n")
    return p


def test_accept_applies_the_agents_edit_and_prints_the_report(project, capsys):
    iso = isolated.create(project, 10**6)
    (iso.work / "a.txt").write_text("two\n")
    assert code.main(["accept", iso.sid]) == 0
    assert (project / "a.txt").read_text() == "two\n" and "M a.txt" in capsys.readouterr().out


def test_reject_leaves_the_project_unchanged(project):
    iso = isolated.create(project, 10**6)
    (iso.work / "a.txt").write_text("two\n")
    assert code.main(["reject", iso.sid]) == 0
    assert (project / "a.txt").read_text() == "one\n" and isolated.sessions() == []


def test_an_unknown_session_is_an_error_exit(project, capsys):
    assert code.main(["accept", "20260101T000000Z-abc123"]) == 1
    assert "no isolated session" in capsys.readouterr().err


def test_scratch_clean_removes_the_project_pad(project, capsys):
    scratch.prepare(project, 10)
    assert scratch_cmd.main(["clean", str(project)]) == 0
    assert "removed" in capsys.readouterr().out and scratch.clean() == []
