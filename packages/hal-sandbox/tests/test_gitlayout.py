"""Tests for `halsandbox.gitlayout` discovery of read-only git mounts."""

from pathlib import Path

import pytest

from halsandbox import gitlayout
from halsandbox.errors import SandboxError


def test_plain_git_dir_is_mounted_at_work_git(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert gitlayout.discover(tmp_path) == (gitlayout.GitMount(tmp_path.resolve() / ".git", "/work/.git"),)


def test_folder_without_git_has_no_mounts(tmp_path: Path) -> None:
    assert gitlayout.discover(tmp_path) == ()


def test_gitfile_layout_resolves_mounts(tmp_path: Path) -> None:
    main = tmp_path / "main" / ".git"
    wt = main / "worktrees" / "w"
    wt.mkdir(parents=True)
    (wt / "commondir").write_text("../..\n")
    work = tmp_path / "w"
    work.mkdir()
    (work / ".git").write_text(f"gitdir: {wt}\n")
    got = gitlayout.discover(work)
    assert got[0] == gitlayout.GitMount(work.resolve() / ".git", "/work/.git")
    assert got[1] == gitlayout.GitMount(wt.resolve(), str(wt.resolve()))
    assert got[2] == gitlayout.GitMount(main.resolve(), str(main.resolve()))


def test_relative_submodule_gitfile_mounts_at_normalised_work_path(tmp_path: Path) -> None:
    work = tmp_path / "super" / "sub"
    mod = tmp_path / "super" / ".git" / "modules" / "sub"
    work.mkdir(parents=True)
    mod.mkdir(parents=True)
    (work / ".git").write_text("gitdir: ../.git/modules/sub\n")
    got = gitlayout.discover(work)
    assert got[1] == gitlayout.GitMount(mod.resolve(), "/.git/modules/sub")
    assert len(got) == 2


def test_gitfile_without_prefix_is_an_error(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("hello\n")
    with pytest.raises(SandboxError, match="not a gitfile"):
        gitlayout.discover(tmp_path)


def test_gitfile_pointing_nowhere_is_an_error(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text(f"gitdir: {tmp_path}/missing\n")
    with pytest.raises(SandboxError, match="does not exist"):
        gitlayout.discover(tmp_path)


def test_symlinked_git_dir_inside_tmp_is_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitlayout, "_allowed_symlink_target", lambda p: True)
    real = tmp_path / "real.git"
    real.mkdir()
    work = tmp_path / "w"
    work.mkdir()
    (work / ".git").symlink_to(real)
    assert gitlayout.discover(work)[0].src == real.resolve()


def test_symlinked_git_dir_outside_allowed_roots_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gitlayout, "_allowed_symlink_target", lambda p: False)
    (tmp_path / "real").mkdir()
    (tmp_path / ".git").symlink_to(tmp_path / "real")
    with pytest.raises(SandboxError, match="outside home"):
        gitlayout.discover(tmp_path)
