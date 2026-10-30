"""Tests for isolated coding sessions: snapshot, diff report, accept, export and reject."""

import subprocess

import pytest

from halsandbox import isolated
from halsandbox.errors import SandboxError

CAP = 10**7


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "app.py").write_text("print('hi')\n")
    (r / ".gitignore").write_text("ignored.txt\n")
    (r / "ignored.txt").write_text("ignored\n")
    (r / ".env").write_text("TOKEN=synthetic\n")
    _git(r, "add", "app.py", ".gitignore")
    _git(r, "commit", "-qm", "init")
    (r / "untracked.txt").write_text("new\n")
    (r / "sub").mkdir()
    (r / "sub" / "extra_key.pem").write_text("synthetic key\n")
    return r


def test_snapshot_has_untracked_files_but_not_ignored_or_masked(repo):
    iso = isolated.create(repo, CAP)
    names = {p.name for p in iso.work.rglob("*") if p.is_file()}
    assert {"app.py", "untracked.txt", ".gitignore"} <= names
    assert not names & {"ignored.txt", ".env", "extra_key.pem"}
    assert not (iso.work / ".git").exists()


def test_symlinks_are_not_copied_into_the_snapshot(repo, tmp_path):
    (repo / "link").symlink_to(tmp_path)
    assert not (isolated.create(repo, CAP).work / "link").exists()


def test_copy_cap_is_enforced(repo):
    with pytest.raises(SandboxError, match="copy cap"):
        isolated.create(repo, 5)
    assert isolated.sessions() == []


def test_accept_applies_edits_adds_and_deletes_and_checkpoints(repo):
    iso = isolated.create(repo, CAP)
    (iso.work / "app.py").write_text("print('changed')\n")
    (iso.work / "new.txt").write_text("n\n")
    (iso.work / "untracked.txt").unlink()
    report = isolated.accept(iso)
    assert {(c.status, c.path) for c in report} == {("M", "app.py"), ("A", "new.txt"), ("D", "untracked.txt")}
    assert (repo / "app.py").read_text() == "print('changed')\n" and (repo / "new.txt").exists()
    assert not (repo / "untracked.txt").exists() and (repo / ".env").exists()
    assert not iso.root.exists()
    cp = next((isolated.paths.state_dir() / "checkpoints").iterdir())
    assert "checkpoint before accept" in _git(cp, "log", "--all", "--format=%s")


def test_high_risk_changes_block_accept_unless_allowed(repo):
    iso = isolated.create(repo, CAP)
    (iso.work / ".github" / "workflows").mkdir(parents=True)
    (iso.work / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    with pytest.raises(isolated.AcceptRefused, match="ci.yml"):
        isolated.accept(iso)
    assert not (repo / ".github").exists() and iso.root.exists()
    isolated.accept(iso, allow_flags=True)
    assert (repo / ".github" / "workflows" / "ci.yml").exists()


def test_a_symlink_made_by_the_agent_is_flagged_and_never_applied(repo, tmp_path):
    iso = isolated.create(repo, CAP)
    (iso.work / "leak").symlink_to("/etc")
    assert [c.flag for c in isolated.changes(iso)] == ["symlink"]
    with pytest.raises(isolated.AcceptRefused):
        isolated.accept(iso)
    assert not (repo / "leak").exists()


def test_a_nested_git_dir_made_by_the_agent_is_removed_not_recorded(repo):
    iso = isolated.create(repo, CAP)
    (iso.work / "vendored" / ".git").mkdir(parents=True)
    (iso.work / "vendored" / ".git" / "config").write_text("[core]\n\tfsmonitor = touch /tmp/pwn\n")
    (iso.work / "vendored" / "a.txt").write_text("a\n")
    assert [c.path for c in isolated.changes(iso)] == ["vendored/a.txt"]


def test_failed_apply_keeps_the_patch_and_leaves_the_project_alone(repo):
    iso = isolated.create(repo, CAP)
    (iso.work / "app.py").write_text("print('agent')\n")
    (repo / "app.py").write_text("print('user edited meanwhile')\n")
    with pytest.raises(isolated.AcceptRefused, match="unchanged"):
        isolated.accept(iso)
    assert (repo / "app.py").read_text() == "print('user edited meanwhile')\n"
    assert (isolated.paths.state_dir() / "patches" / f"{iso.sid}.patch").is_file()


def test_nongit_directory_is_copied_and_accepted(tmp_path):
    d = tmp_path / "plain"
    (d / "node_modules").mkdir(parents=True)
    d.joinpath("a.txt").write_text("a\n")
    d.joinpath("node_modules", "x.js").write_text("x\n")
    iso = isolated.create(d, CAP)
    assert not (iso.work / "node_modules").exists()
    (iso.work / "a.txt").write_text("b\n")
    isolated.accept(iso)
    assert (d / "a.txt").read_text() == "b\n"


def test_export_writes_a_patch_and_a_bundle_without_touching_the_project(repo, tmp_path):
    iso = isolated.create(repo, CAP)
    (iso.work / "app.py").write_text("print('x')\n")
    patch = isolated.export(iso, tmp_path / "out.patch")
    bundle = isolated.export(iso, tmp_path / "out.bundle", "bundle")
    assert b"print('x')" in patch.read_bytes() and bundle.stat().st_size > 0
    assert (repo / "app.py").read_text() == "print('hi')\n"


def test_reject_removes_the_session(repo):
    iso = isolated.create(repo, CAP)
    isolated.reject(iso)
    assert isolated.sessions() == []
    with pytest.raises(SandboxError):
        isolated.load(iso.sid)


def test_session_ids_with_path_parts_are_refused():
    with pytest.raises(SandboxError):
        isolated.load("../../etc")
