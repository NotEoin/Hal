"""Tests for box path translation and safe reads."""

import os
import socket

import pytest

from halsandbox.paths import WorkspaceEscape, box_to_host, read_beneath


def test_work_and_children_map_below_the_host_dir(tmp_path):
    assert box_to_host("/work", tmp_path) == tmp_path
    assert box_to_host("/work/a/b.py", tmp_path) == tmp_path / "a" / "b.py"
    assert box_to_host("/work/a/../b", tmp_path) == tmp_path / "b"


@pytest.mark.parametrize(
    "p", ["/work/../etc/passwd", "/workx/a", "/etc/passwd", "work/a", "/work/a\0b", "/", "//etc", "/work/.."]
)
def test_box_to_host_rejects_escapes(tmp_path, p):
    with pytest.raises(WorkspaceEscape):
        box_to_host(p, tmp_path)


def test_read_beneath_reads_a_regular_file_up_to_the_cap(tmp_path):
    (tmp_path / "f").write_text("abcdef")
    assert read_beneath(tmp_path, "f", max_bytes=3) == b"abc"


def test_read_beneath_refuses_symlinks_and_dotdot(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "secret").write_text("x")
    os.symlink(tmp_path / "secret", tmp_path / "work" / "link")
    with pytest.raises(WorkspaceEscape):
        read_beneath(tmp_path / "work", "link")
    with pytest.raises(WorkspaceEscape):
        read_beneath(tmp_path / "work", "../secret")


def test_read_beneath_refuses_fifo_without_blocking(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(WorkspaceEscape, match="regular"):
        read_beneath(tmp_path, "pipe")


def test_read_beneath_refuses_a_socket(tmp_path):
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(tmp_path / "s"))
    try:
        with pytest.raises((WorkspaceEscape, OSError)):
            read_beneath(tmp_path, "s")
    finally:
        s.close()
