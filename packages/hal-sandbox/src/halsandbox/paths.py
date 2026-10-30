"""Translation of box paths to host paths and safe reads of files in a work tree.

Host code never walks a model-written tree itself: every open goes through
`halkit.fsafe.open_beneath`, which refuses symlinks and `..` without a check-then-open race
(02-sandbox REQ-BOX-57, RESOLUTIONS C10). There is deliberately no second path walker here.
"""

from __future__ import annotations

import os
import posixpath
import stat
from pathlib import Path

from halkit import fsafe

BOX_WORK = "/work"
DEFAULT_MAX_READ_BYTES = 1024 * 1024  # a source file or lockfile; larger is not read whole


class WorkspaceEscape(Exception):
    """A path that leaves the work tree, or a file the host refuses to read."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path!r}: {reason}")
        self.path = path
        self.reason = reason


def box_to_host(box_path: str, work: Path) -> Path:
    """The host path of `box_path`, which must be `/work` or below it.

    Raises `WorkspaceEscape` for a NUL, a relative path, a `..` that leaves `/work`, or any
    other absolute path (including `/workx`, which only shares a prefix).
    """
    if "\0" in box_path:
        raise WorkspaceEscape(box_path.replace("\0", "\\0"), "NUL byte")
    if not box_path.startswith("/"):
        raise WorkspaceEscape(box_path, "not an absolute box path")
    norm = posixpath.normpath(box_path)
    if norm.startswith("//") and not norm.startswith("///"):
        norm = "/" + norm.lstrip("/")  # POSIX keeps a leading double slash through normpath
    if norm != BOX_WORK and not norm.startswith(BOX_WORK + "/"):
        raise WorkspaceEscape(box_path, f"outside {BOX_WORK}")
    rel = norm[len(BOX_WORK) :].lstrip("/")
    return work / rel if rel else work


def read_beneath(root: Path, rel: str, *, max_bytes: int = DEFAULT_MAX_READ_BYTES) -> bytes:
    """At most `max_bytes` of the regular file `rel` below `root`, opened without following links.

    Raises `WorkspaceEscape` for a symlink, `..`, or a non-regular file (a FIFO would block, a
    device would leak).
    """
    try:
        fd = fsafe.open_beneath(root, rel, os.O_RDONLY | os.O_NONBLOCK)
    except fsafe.UnsafePathError as exc:
        raise WorkspaceEscape(rel, str(exc)) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkspaceEscape(rel, "not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read(max_bytes)
    finally:
        os.close(fd)
