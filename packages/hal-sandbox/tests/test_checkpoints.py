"""Tests for git checkpoints: contents, masking, skipping, dedupe, safety of the user's repo."""

import os
import subprocess
import time

import pytest

from halsandbox.checkpoints import CheckpointConfig, CheckpointError, Checkpoints

SID = "0123456789ab"


def _git(cwd, *a):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", *a], cwd=cwd, check=True, capture_output=True
    ).stdout.decode()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / ".gitignore").write_text("cache/\n*.log\n")
    (r / "app.py").write_text("x = 1\n")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "init")
    return r


def _files(repo, ref):
    return set(_git(repo, "ls-tree", "-r", "--name-only", ref).split())


def test_checkpoint_contents(repo):
    (repo / "new.py").write_text("n\n")
    (repo / "app.py").write_text("x = 2\n")
    (repo / "cache").mkdir()
    (repo / "cache" / "c.bin").write_text("c")
    (repo / "run.log").write_text("l")
    cp = Checkpoints.for_work(repo, SID).take("turn 0")
    assert _files(repo, cp.commit) == {".gitignore", "app.py", "new.py"}
    assert _git(repo, "show", f"{cp.commit}:app.py") == "x = 2\n"
    assert cp.ref == f"refs/hal/checkpoints/{SID}/0" and cp.label == "turn 0"
    assert cp.created.endswith("Z") and set(cp.ignored) == {"cache/", "run.log"}


def test_unchanged_tree_reuses_checkpoint(repo):
    c = Checkpoints.for_work(repo, SID)
    first = c.take("a")
    again = c.take("b")
    assert again.commit == first.commit and len(c.list()) == 1
    (repo / "app.py").write_text("y\n")
    assert c.take("c").n == 1


def test_users_index_and_refs_are_untouched(repo):
    (repo / "staged.py").write_text("s")
    _git(repo, "add", "staged.py")
    (repo / "loose.py").write_text("l")
    before = (repo / ".git" / "index").read_bytes()
    heads = _git(repo, "for-each-ref", "refs/heads")
    Checkpoints.for_work(repo, SID).take("t")
    assert (repo / ".git" / "index").read_bytes() == before
    assert _git(repo, "for-each-ref", "refs/heads") == heads
    assert _git(repo, "status", "--porcelain").splitlines() == ["A  staged.py", "?? loose.py"]


def test_masked_paths_left_out_and_listed(repo):
    (repo / "deploy").mkdir()
    (repo / "deploy" / "server.key").write_text("SECRET-KEY")
    (repo / ".env").write_text("TOKEN=abc")
    cp = Checkpoints.for_work(repo, SID).take("t")
    assert cp.masked == (".env", "deploy/server.key")
    assert _files(repo, cp.commit) == {".gitignore", "app.py"}
    objects = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch"], cwd=repo, capture_output=True
    ).stdout
    assert b"SECRET-KEY" not in objects and b"TOKEN=abc" not in objects


def test_unmasked_project_checkpoints_secrets(repo):
    (repo / ".env").write_text("TOKEN=abc")
    cp = Checkpoints.for_work(repo, SID, CheckpointConfig(unmasked=True)).take("t")
    assert ".env" in _files(repo, cp.commit) and cp.masked == ()


def test_tracked_masked_file_is_removed_from_the_checkpoint_only(repo):
    (repo / ".env").write_text("A=1")
    _git(repo, "add", "-f", ".env")
    _git(repo, "commit", "-qm", "oops")
    cp = Checkpoints.for_work(repo, SID).take("t")
    assert ".env" not in _files(repo, cp.commit) and cp.masked == (".env",)
    assert ".env" in _files(repo, "HEAD")


def test_large_files_are_skipped_and_fifos_never_enter_the_tree(repo):
    (repo / "big.bin").write_bytes(b"x" * 2048)
    os.mkfifo(repo / "pipe")
    cp = Checkpoints.for_work(repo, SID, CheckpointConfig(max_file_bytes=1024)).take("t")
    assert cp.skipped == ("big.bin",)
    assert _files(repo, cp.commit).isdisjoint({"big.bin", "pipe"})
    msg = _git(repo, "log", "-1", "--format=%B", cp.commit)
    assert "skipped: big.bin (2048 bytes)" in msg


def test_list_returns_stored_metadata(repo):
    c = Checkpoints.for_work(repo, SID)
    c.take("one")
    (repo / "f").write_text("1")
    (repo / ".env").write_text("A")
    c.take("two")
    got = c.list()
    assert [(x.n, x.label) for x in got] == [(0, "one"), (1, "two")]
    assert got[1].masked == (".env",)


def test_path_with_glob_characters_is_stored_literally(repo):
    (repo / "a[1].txt").write_text("g")
    (repo / "b*.txt").write_text("g")
    cp = Checkpoints.for_work(repo, SID).take("t")
    assert {"a[1].txt", "b*.txt"} <= _files(repo, cp.commit)


def test_non_git_directory_is_refused(tmp_path):
    with pytest.raises(CheckpointError):
        Checkpoints.for_work(tmp_path, SID)


def test_failure_blocks_turn(repo, monkeypatch):
    c = Checkpoints.for_work(repo, SID)
    monkeypatch.setattr(c, "_commit", lambda *a, **k: (_ for _ in ()).throw(CheckpointError("disk full")))
    (repo / "z").write_text("z")
    with pytest.raises(CheckpointError, match="disk full"):
        c.take("t")


def test_deleted_file_is_absent_from_the_next_checkpoint(repo):
    c = Checkpoints.for_work(repo, SID)
    c.take("a")
    (repo / "app.py").unlink()
    cp = c.take("b")
    assert "app.py" not in _files(repo, cp.commit)


def test_restore_round_trip(repo):
    c = Checkpoints.for_work(repo, SID)
    (repo / "keep.py").write_text("k1\n")
    (repo / "gone.py").write_text("g\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "x.py").write_text("x\n")
    cp = c.take("start")
    index_before = (repo / ".git" / "index").read_bytes()
    (repo / "keep.py").write_text("changed\n")
    (repo / "gone.py").unlink()
    (repo / "added.py").write_text("a\n")
    (repo / "newdir").mkdir()
    (repo / "newdir" / "y.py").write_text("y")
    (repo / "run.log").write_text("ignored stays")
    (repo / ".env").write_text("masked stays")
    result = c.restore(cp)
    assert (repo / "keep.py").read_text() == "k1\n" and (repo / "gone.py").read_text() == "g\n"
    assert not (repo / "added.py").exists() and not (repo / "newdir").exists()
    assert set(result.restored) == {"keep.py", "gone.py"}
    assert set(result.deleted) == {"added.py", "newdir/y.py"}
    assert (repo / "run.log").read_text() == "ignored stays" and (repo / ".env").exists()
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert result.safety.label == "before restore to 0"


def test_restore_is_undoable_through_the_safety_checkpoint(repo):
    c = Checkpoints.for_work(repo, SID)
    first = c.take("a")
    (repo / "app.py").write_text("later\n")
    result = c.restore(first)
    assert (repo / "app.py").read_text() == "x = 1\n"
    c.restore(result.safety)
    assert (repo / "app.py").read_text() == "later\n"


def test_deleted_ignored_file_is_reported_not_restorable(repo):
    (repo / "run.log").write_text("l")
    (repo / ".env").write_text("A")
    (repo / "big.bin").write_bytes(b"x" * 2048)
    c = Checkpoints.for_work(repo, SID, CheckpointConfig(max_file_bytes=1024))
    cp = c.take("a")
    for name in ("run.log", ".env", "big.bin"):
        (repo / name).unlink()
    result = c.restore(cp)
    assert set(result.not_restorable) == {("run.log", "ignored"), (".env", "masked"), ("big.bin", "skipped")}


def test_prune_retention(repo):
    now = time.time()
    sids = ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc")
    for sid in sids:
        (repo / f"{sid}.txt").write_text(sid)
        Checkpoints.for_work(repo, sid).take("t")
    me = Checkpoints.for_work(repo, SID)
    assert me.prune(keep_sessions=1, max_age_days=14, now=now) == 0  # all are recent
    assert me.prune(keep_sessions=1, max_age_days=14, now=now + 30 * 86400) == 2  # old, beyond the newest 1
    left = {
        line.split("/")[3] for line in me.git.out("for-each-ref", "--format=%(refname)", "refs/hal/").split()
    }
    assert len(left) == 1 and left < set(sids)


def test_diff_reports_changes_and_flags_since_first_checkpoint(repo):
    ck = Checkpoints.for_work(repo, SID)
    ck.take("start")
    (repo / "app.py").write_text("x = 2\n")
    (repo / ".envrc").write_text("export A=1\n")
    (repo / "link").symlink_to("/etc/passwd")
    (repo / "run.sh").write_text("echo hi\n")
    (repo / "run.sh").chmod(0o755)
    rep = ck.diff()
    by_path = {f.path: f for f in rep.files}
    assert by_path["app.py"].status == "M" and by_path["link"].new_mode == "120000"
    kinds = {(f.path, f.kind) for f in rep.flags}
    assert (".envrc", "automation") in kinds and ("link", "symlink_outside") in kinds
    assert ("run.sh", "executable_added") in kinds
    assert [f.path for f in rep.high] and "x = 2" in rep.patch and not rep.truncated


def test_diff_cuts_the_patch_at_the_limit_and_says_so(repo):
    ck = Checkpoints.for_work(repo, SID)
    ck.take("start")
    (repo / "app.py").write_text("y = 1\n" * 200)
    rep = ck.diff(max_patch_bytes=100)
    assert rep.truncated and len(rep.patch) <= 100 and "app.py" in rep.stat


def test_diff_flags_a_file_left_out_for_size(repo):
    ck = Checkpoints.for_work(repo, SID, CheckpointConfig(max_file_bytes=10))
    ck.take("start")
    (repo / "big.bin").write_bytes(b"x" * 100)
    assert ("big.bin", "skipped_large") in {(f.path, f.kind) for f in ck.diff().flags}


def test_diff_without_a_checkpoint_fails_loudly(repo):
    with pytest.raises(CheckpointError):
        Checkpoints.for_work(repo, SID).diff()
