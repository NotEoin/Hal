"""Tests for the `git` tool, on a throwaway repository."""

import subprocess

import pytest

from halagent.tools import ToolError, Toolset, Workspace
from halagent.tools.gittool import git


@pytest.fixture
def ws(tmp_path):
    def sh(*argv):
        subprocess.run(["git", *argv], cwd=tmp_path, check=True, capture_output=True, timeout=30)

    sh("init", "-q")
    sh("config", "user.email", "t@example.invalid")
    sh("config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n")
    sh("add", "a.txt")
    sh("commit", "-qm", "First")
    return Workspace(tmp_path)


def test_status_and_diff_show_a_working_copy_change(ws, tmp_path):
    (tmp_path / "a.txt").write_text("two\n")
    assert "a.txt" in git(ws, {"action": "status"})
    assert "+two" in git(ws, {"action": "diff"})


def test_commit_stages_and_records_the_change_and_log_lists_it(ws, tmp_path):
    (tmp_path / "b.txt").write_text("new\n")
    git(ws, {"action": "commit", "message": "Add b"})
    assert "Add b" in git(ws, {"action": "log"})
    assert git(ws, {"action": "status"}).count("b.txt") == 0


def test_a_planted_hook_does_not_run_on_commit(ws, tmp_path):
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path}/pwned\nexit 1\n")
    hook.chmod(0o755)
    (tmp_path / "c.txt").write_text("c\n")
    git(ws, {"action": "commit", "message": "Add c"})
    assert not (tmp_path / "pwned").exists()


def test_bad_action_and_multiline_message_are_refused(ws):
    with pytest.raises(ToolError, match="action"):
        git(ws, {"action": "push"})
    with pytest.raises(ToolError, match="one-line"):
        git(ws, {"action": "commit", "message": "a\nb"})


def test_git_is_left_out_when_the_toolset_has_no_run(tmp_path):
    assert "git" in Toolset(tmp_path).tools and "git" not in Toolset(tmp_path, with_run=False).tools
