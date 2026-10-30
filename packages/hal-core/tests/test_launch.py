"""Launch target resolution: one case per row of the table in docs/design/06-core-cli.md."""

import subprocess
from pathlib import Path

import pytest

import halcore_env  # noqa: F401 - sets HAL_* paths before halcore imports
from halcore import launch


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def _git(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True, timeout=20)


def test_git_work_tree_opens_code_at_its_top_level(home: Path) -> None:
    repo = home / "proj"
    (repo / "sub").mkdir(parents=True)
    _git(repo)
    target = launch.resolve_target(repo / "sub", "auto", home=home)
    assert (target.mode, target.root, target.is_git, target.reason) == ("code", repo.resolve(), True, "git repository")


def test_dotfiles_repo_at_home_with_a_marker_opens_code_in_the_folder(home: Path) -> None:
    _git(home)
    folder = home / "app"
    folder.mkdir()
    (folder / "Makefile").write_text("")
    target = launch.resolve_target(folder, "auto", home=home)
    assert target.mode == "code" and target.root == folder.resolve()
    assert target.reason == "project marker Makefile; git root is your home"


def test_dotfiles_repo_at_home_without_marker_is_chat_and_code_is_refused(home: Path) -> None:
    _git(home)
    folder = home / "notes"
    folder.mkdir()
    assert launch.resolve_target(folder, "auto", home=home).mode == "chat"
    with pytest.raises(launch.ForbiddenRoot):
        launch.resolve_target(folder, "code", home=home)


def test_non_git_folder_with_a_marker_opens_code(home: Path) -> None:
    folder = home / "tool"
    folder.mkdir()
    (folder / "go.mod").write_text("")
    target = launch.resolve_target(folder, "auto", home=home)
    assert (target.mode, target.is_git, target.reason) == ("code", False, "project marker go.mod")


def test_folder_without_a_project_is_chat_unless_code_is_requested(home: Path) -> None:
    folder = home / "misc"
    folder.mkdir()
    assert launch.resolve_target(folder, "auto", home=home).mode == "chat"
    assert launch.resolve_target(folder, "code", home=home).root == folder.resolve()


@pytest.mark.parametrize("name", [".ssh", ".aws", ".config/hal", ".local/share"])
def test_forbidden_folders_are_chat_and_refuse_code(home: Path, name: str) -> None:
    folder = home / name
    folder.mkdir(parents=True)
    assert launch.resolve_target(folder, "auto", home=home).mode == "chat"
    with pytest.raises(launch.ForbiddenRoot):
        launch.resolve_target(folder, "code", home=home)


def test_home_and_its_ancestors_are_forbidden(home: Path) -> None:
    assert launch.forbidden_rule(home, home=home)
    assert launch.forbidden_rule(home.parent, home=home)
    assert launch.forbidden_rule(Path("/"), home=home)


def test_folder_containing_a_hard_deny_directory_is_forbidden(home: Path) -> None:
    (home / "stuff" / ".aws").mkdir(parents=True)
    assert launch.forbidden_rule(home / "stuff", home=home) is None  # only home itself holds ~/.aws
    (home / ".aws").mkdir()
    assert launch.forbidden_rule(home, home=home)


def test_symlink_into_home_is_resolved_before_the_check(home: Path, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(home)
    assert launch.resolve_target(link, "auto", home=home).mode == "chat"


def test_chat_request_ignores_the_folder(home: Path) -> None:
    repo = home / "r"
    repo.mkdir()
    _git(repo)
    assert launch.resolve_target(repo, "chat", home=home).root is None
