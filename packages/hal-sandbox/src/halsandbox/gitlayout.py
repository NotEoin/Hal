"""Discovery of the git metadata a box must see read-only (02-sandbox REQ-BOX-15).

Reads `<work>/.git` without running git: a hostile repo can set `core.fsmonitor` and the like, and
discovery needs only a directory, a gitfile and `commondir`. Mounting at the resolved absolute path
keeps git inside the box working without rewriting the user's gitfile.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from pathlib import Path

from .errors import SandboxError

BOX_WORK = "/work"
GITFILE_PREFIX = "gitdir:"
MAX_GITFILE_BYTES = 4096  # a gitfile is one line; anything larger is not one


@dataclass(frozen=True)
class GitMount:
    """One read-only bind: host path `src` appears at box path `dst`."""

    src: Path
    dst: str


def _allowed_symlink_target(path: Path) -> bool:
    """True when `path` is under the user's home or `/tmp` (REQ-BOX-15, symlinked `.git`)."""
    roots = (Path.home().resolve(), Path("/tmp").resolve())
    return any(path == r or r in path.parents for r in roots)


def _read_line(path: Path) -> str:
    """The text of a small file; raises `SandboxError` when it is missing or too large."""
    try:
        if path.stat().st_size > MAX_GITFILE_BYTES:
            raise SandboxError(f"{path}: too large to be a git pointer file")
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raise SandboxError(f"cannot read {path}: {exc}") from exc


def _box_path(host: Path, work: Path, rel: str | None) -> str:
    """Where `host` appears in the box: normpath(/work/rel) for a relative gitfile, else absolute."""
    if rel is not None:
        return posixpath.normpath(posixpath.join(BOX_WORK, rel))
    return str(host)


def discover(work: Path) -> tuple[GitMount, ...]:
    """The read-only mounts that give the box `<work>/.git`; empty when there is none.

    Raises `SandboxError` for a malformed gitfile, a missing git dir, or a symlinked `.git` whose
    target is outside the user's home and `/tmp`.
    """
    work = work.resolve()
    dot_git = work / ".git"
    if dot_git.is_symlink():
        target = dot_git.resolve()
        if not _allowed_symlink_target(target):
            raise SandboxError(f"{dot_git} is a symlink to {target}, outside home and /tmp")
        dot_git = target
    elif not os.path.lexists(dot_git):
        return ()
    if dot_git.is_dir():
        return (GitMount(dot_git, f"{BOX_WORK}/.git"),)
    line = _read_line(dot_git)
    if not line.startswith(GITFILE_PREFIX):
        raise SandboxError(f"{dot_git}: not a gitfile (expected '{GITFILE_PREFIX} <path>')")
    raw = line[len(GITFILE_PREFIX) :].strip()
    if not raw or "\0" in raw:
        raise SandboxError(f"{dot_git}: empty or invalid gitdir")
    rel = None if os.path.isabs(raw) else raw
    gitdir = (Path(raw) if rel is None else work / raw).resolve()
    if not gitdir.is_dir():
        raise SandboxError(f"{dot_git}: git dir {gitdir} does not exist")
    mounts = [GitMount(dot_git, f"{BOX_WORK}/.git"), GitMount(gitdir, _box_path(gitdir, work, rel))]
    common = gitdir / "commondir"
    if common.is_file():
        cdir = (gitdir / _read_line(common)).resolve()
        if not cdir.is_dir():
            raise SandboxError(f"{common}: common dir {cdir} does not exist")
        if cdir != gitdir:
            cbox = _box_path(cdir, work, None if rel is None else os.path.relpath(cdir, work))
            mounts.append(GitMount(cdir, cbox))
    return tuple(mounts)
