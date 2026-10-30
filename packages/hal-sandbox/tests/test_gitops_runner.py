"""Tests for `GitRunner`: hardening, environment scrubbing, errors (REQ-BOX-48)."""

import subprocess

import pytest

from halkit import proc
from halsandbox.gitops import GitError, GitRunner


def _git(cwd, *a):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", *a], cwd=cwd, check=True, capture_output=True
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "a.txt").write_text("1\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-qm", "i")
    return r


def test_nested_repo_fsmonitor_is_not_run_by_a_parent_status(repo, tmp_path):
    nested = repo / "vendor"
    nested.mkdir()
    _git(nested, "init", "-q")
    marker = tmp_path / "ran.marker"
    hook = tmp_path / "mon.sh"
    hook.write_text(f"#!/bin/sh\necho ran > {marker}\n")
    hook.chmod(0o755)
    (nested / "f").write_text("x")
    _git(nested, "add", "f")
    _git(nested, "commit", "-qm", "n")
    _git(nested, "config", "core.fsmonitor", str(hook))
    g = GitRunner(repo)
    g.run("add", "-A")
    g.run("status", "--porcelain")
    g.run("diff", "--cached", "--stat")
    assert not marker.exists()


def test_inherited_git_env_is_removed(repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "bogus"))
    assert GitRunner(repo).out("rev-parse", "--git-dir") == str(repo / ".git")


def test_failure_raises_git_error_with_stderr(repo):
    with pytest.raises(GitError) as err:
        GitRunner(repo).run("cat-file", "-p", "deadbeef")
    assert err.value.returncode != 0 and err.value.argv[0] == "cat-file"


def test_check_false_returns_the_failure(repo):
    assert GitRunner(repo).run("cat-file", "-e", "deadbeef", check=False).returncode != 0


def test_timeout_is_a_git_error(repo, monkeypatch):
    def slow(argv, **kw):
        raise proc.ProcTimeout(argv, 1.0)

    monkeypatch.setattr(proc, "run", slow)
    with pytest.raises(GitError, match="timed out"):
        GitRunner(repo, timeout_s=1).run("status")


def test_diff_commands_disable_external_tools(repo):
    argv = GitRunner(repo).argv("diff", "--stat")
    assert "--no-ext-diff" in argv and "--no-textconv" in argv


def test_runs_outside_the_work_tree(repo):
    g = GitRunner(repo)
    assert not str(g._cwd).startswith(str(repo))


def test_init_creates_a_bare_repo_at_the_given_path(tmp_path):
    target = tmp_path / "bare"
    GitRunner(tmp_path, git_dir=target).run("init", "-q", "--bare", str(target))
    assert (target / "HEAD").is_file()
