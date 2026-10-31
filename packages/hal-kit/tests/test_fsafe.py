"""Tests for `halkit.fsafe`: `open_beneath` (REQ-SEC-68) and the policy-bound `walk` (REQ-SEC-100)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from halkit.fsafe import WalkPolicy, is_beneath, open_beneath, open_regular_beneath, walk
from halkit.paths import UnsafePathError


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A workspace with a nested file and a secret outside it."""
    root = (tmp_path / "work").resolve()
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "mod.py").write_text("print('hi')\n")
    (tmp_path / "secret.txt").write_text("synthetic secret\n")
    return root


def _read(root: Path, rel: str) -> str:
    fd = open_beneath(root, rel)
    try:
        return os.read(fd, 1024).decode()
    finally:
        os.close(fd)


def test_open_beneath_reads_a_nested_file(tree):
    assert _read(tree, "src/pkg/mod.py") == "print('hi')\n"


@pytest.mark.parametrize("rel", ["../secret.txt", "src/../../secret.txt", "/etc/passwd", "", "a\x00b"])
def test_open_beneath_refuses_traversal_absolute_empty_and_nul(tree, rel):
    with pytest.raises(UnsafePathError):
        open_beneath(tree, rel)


def test_open_beneath_refuses_a_symlink_as_the_final_component(tree):
    (tree / "evil").symlink_to(tree.parent / "secret.txt")
    with pytest.raises(UnsafePathError, match="symlink"):
        open_beneath(tree, "evil")


def test_open_beneath_refuses_a_symlink_in_a_middle_component(tree):
    (tree / "link").symlink_to(tree / "src")
    with pytest.raises(UnsafePathError, match="symlink"):
        open_beneath(tree, "link/pkg/mod.py")


def test_directory_swapped_for_a_link_between_calls_is_caught(tree):
    assert _read(tree, "src/pkg/mod.py")
    (tree / "src").rename(tree / "src.old")
    (tree / "src").symlink_to(tree / "src.old")
    with pytest.raises(UnsafePathError):
        open_beneath(tree, "src/pkg/mod.py")


def test_open_beneath_creates_files_private(tree):
    fd = open_beneath(tree, "src/new.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    os.close(fd)
    assert (tree / "src" / "new.txt").stat().st_mode & 0o777 == 0o600


def test_open_regular_beneath_refuses_a_fifo_without_blocking(tree):
    # regression: a FIFO planted in the box copy hung the host TUI and ACP proxy on open
    os.mkfifo(tree / "pipe")
    for flags in (os.O_RDONLY, os.O_WRONLY | os.O_APPEND | os.O_CREAT):
        with pytest.raises((UnsafePathError, OSError)):
            os.close(open_regular_beneath(tree, "pipe", flags))
    with pytest.raises(UnsafePathError, match="not a regular file"):
        open_regular_beneath(tree, "src")


def test_open_regular_beneath_returns_a_blocking_descriptor_for_a_file(tree):
    fd = open_regular_beneath(tree, "src/pkg/mod.py")
    try:
        assert os.get_blocking(fd) and os.read(fd, 100)
    finally:
        os.close(fd)


def test_missing_file_raises_file_not_found(tree):
    with pytest.raises(FileNotFoundError):
        open_beneath(tree, "src/none.txt")


@pytest.mark.parametrize(
    ("root", "path", "expected"),
    [("/a/b", "/a/b", True), ("/a/b", "/a/b/c", True), ("/a/b", "/a/bc", False), ("/a/b", "/a", False)],
)
def test_is_beneath_compares_whole_components(root, path, expected):
    assert is_beneath(Path(root), Path(path)) is expected


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    """A read root with a denied folder, a workspace, a masked file and a planted link."""
    home = tmp_path.resolve()
    root = home / "Documents"
    for folder in ("notes", "important", "repos/active", ".ssh-copy"):
        (root / folder).mkdir(parents=True)
    (root / "notes" / "a.md").write_text("a")
    (root / "notes" / ".env").write_text("TOKEN=x")
    (root / "important" / "b.md").write_text("b")
    (root / "repos" / "active" / "c.py").write_text("c")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_ed25519").write_text("synthetic key")
    (root / "notes" / "keys").symlink_to(home / ".ssh")
    (root / "notes" / "key-file").symlink_to(home / ".ssh" / "id_ed25519")
    return root


def _policy(docs: Path, **kwargs: object) -> WalkPolicy:
    return WalkPolicy(
        allow_roots=(docs,),
        deny=(docs / "important", docs / "repos" / "active"),
        deny_globs=(".env", "*.pem"),
        **kwargs,
    )


def test_walk_skips_denied_masked_and_workspace_paths(docs):
    found = {p.relative_to(docs).as_posix() for p in walk(docs, policy=_policy(docs))}
    assert found == {"notes", "notes/a.md", "repos", ".ssh-copy"}


def test_walk_never_follows_symlinks(docs):
    found = list(walk(docs, policy=_policy(docs)))
    assert all("keys" not in p.parts and p.name != "key-file" for p in found)
    assert not any(p.name == "id_ed25519" for p in found)


def test_walk_stops_after_max_entries(docs):
    assert len(list(walk(docs, policy=_policy(docs, max_entries=2)))) == 2


@pytest.mark.parametrize("sub", ["important", "repos/active"])
def test_walk_refuses_a_denied_root(docs, sub):
    with pytest.raises(UnsafePathError, match="denied"):
        list(walk(docs / sub, policy=_policy(docs)))


def test_walk_refuses_a_root_outside_the_read_roots(docs):
    with pytest.raises(UnsafePathError, match="read root"):
        list(walk(docs.parent / ".ssh", policy=_policy(docs)))


def test_walk_refuses_an_unresolved_root(docs):
    with pytest.raises(UnsafePathError, match="resolved"):
        list(walk(docs / "notes" / "keys", policy=_policy(docs)))
