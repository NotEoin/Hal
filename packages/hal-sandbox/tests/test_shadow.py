"""Tests for shadow-repository checkpoints of non-git folders (REQ-BOX-52)."""

import pytest

from halsandbox import shadow
from halsandbox.checkpoints import CheckpointConfig, CheckpointError

SID = "0123456789ab"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "notes"
    f.mkdir()
    (f / "a.txt").write_text("one\n")
    (f / ".gitignore").write_text("*.tmp\n")
    return f


def test_non_git_folder_gets_a_shadow_repo_and_no_dot_git(folder):
    c = shadow.open_checkpoints(folder, SID)
    cp = c.take("t")
    assert not (folder / ".git").exists()
    assert shadow.shadow_dir(folder).is_dir() and cp.n == 0
    assert c.git.out("ls-tree", "-r", "--name-only", cp.commit).split() == [".gitignore", "a.txt"]


def test_shadow_restore_round_trip_and_ignore_rules(folder):
    c = shadow.open_checkpoints(folder, SID)
    (folder / "x.tmp").write_text("ignored")
    cp = c.take("t")
    assert "x.tmp" not in c.git.out("ls-tree", "-r", "--name-only", cp.commit)
    (folder / "a.txt").write_text("two\n")
    (folder / "b.txt").write_text("new\n")
    result = c.restore(cp)
    assert (folder / "a.txt").read_text() == "one\n" and not (folder / "b.txt").exists()
    assert result.deleted == ("b.txt",) and (folder / "x.tmp").exists()


def test_checkpoints_continue_across_openings(folder):
    shadow.open_checkpoints(folder, SID).take("a")
    (folder / "a.txt").write_text("two\n")
    again = shadow.open_checkpoints(folder, SID)
    assert again.take("b").n == 1 and len(again.list()) == 2


def test_oversized_folder_is_refused_with_the_way_out(folder):
    (folder / "data.bin").write_bytes(b"x" * 600)
    with pytest.raises(CheckpointError, match="--isolated or --no-checkpoint"):
        shadow.open_checkpoints(folder, SID, nongit_max_total_bytes=500)


def test_git_repo_uses_its_own_dot_git(tmp_path):
    import subprocess

    r = tmp_path / "r"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    c = shadow.open_checkpoints(r, SID, CheckpointConfig())
    assert c.git.git_dir == r / ".git" and not shadow.shadow_root().exists()


def test_secret_files_stay_out_of_the_shadow_objects(folder):
    (folder / ".env").write_text("TOKEN=zzz")
    c = shadow.open_checkpoints(folder, SID)
    cp = c.take("t")
    assert cp.masked == (".env",)
    objs = c.git.run("cat-file", "--batch-all-objects", "--batch").stdout
    assert b"TOKEN=zzz" not in objs
