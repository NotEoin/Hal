"""File access that cannot be redirected by symlinks: `open_beneath` and a policy-bound `walk`.

Host code reads paths that untrusted parties may have shaped: TUI previews of files a box
wrote into the workspace (REQ-SEC-68) and pack tools that list or search directories under
the user's read roots (REQ-SEC-100). Both must never follow a symlink, because a link planted
to `~/.ssh` would otherwise hand its target to the host.

- `open_beneath` walks a relative path one component at a time with
  `os.open(name, O_NOFOLLOW | O_DIRECTORY | O_CLOEXEC, dir_fd=parent)`. Python 3.11 has no
  `openat2`, so this is the portable equivalent of `RESOLVE_BENEATH`: `..`, absolute paths
  and a symlink in any component are refused with `UnsafePathError`, and a component swapped
  for a link between two calls is caught by the next `open`.
- `open_regular_beneath` also refuses anything but a regular file: a FIFO a box planted in its
  copy would otherwise block the host process that opens it.
- `walk` uses `os.scandir` with `follow_symlinks=False`: it never descends into or yields a
  symlink, skips denied directories without listing them, skips files whose names match a
  deny glob, and stops after `WalkPolicy.max_entries` paths. 01-security "Proposed halkit
  additions" gives the rules; `halhub.policy.paths.walk_policy` builds the policy from
  `policy.toml`, so the walk and the broker's path check agree.

Linux and macOS only (`dir_fd` support); Hal's host side does not run on Windows.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .paths import UnsafePathError

DEFAULT_MAX_ENTRIES = 100_000  # 01-security "Proposed halkit additions": keeps a walk to seconds
CREATED_FILE_MODE = 0o600  # a file `open_beneath` creates is private, like every Hal file

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_NOFOLLOW_DIR_FLAGS = _DIR_FLAGS | os.O_NOFOLLOW


@dataclass(frozen=True)
class WalkPolicy:
    """What a `walk` may visit; every path is absolute and already resolved."""

    allow_roots: tuple[Path, ...]  # resolved read roots for the tool
    deny: tuple[Path, ...]  # hard-deny paths, [paths].deny, Hal's dirs, active workspaces
    deny_globs: tuple[str, ...]  # hard-deny file patterns and [paths].deny globs
    max_entries: int = DEFAULT_MAX_ENTRIES  # bound on yielded paths; the tool reports truncation


def is_beneath(root: Path, path: Path) -> bool:
    """True when `path` is `root` or below it, compared lexically after normalisation.

    Touches no file: callers pass resolved paths (`os.path.realpath`) when links matter.
    """
    root_text = os.path.normpath(os.fspath(root))
    path_text = os.path.normpath(os.fspath(path))
    if root_text == path_text:
        return True
    prefix = root_text if root_text.endswith(os.sep) else root_text + os.sep
    return path_text.startswith(prefix)


def _components(rel: str) -> list[str]:
    if "\x00" in rel:
        raise UnsafePathError(f"{rel!r}: contains a NUL character")
    if os.path.isabs(rel):
        raise UnsafePathError(f"{rel!r}: an absolute path is not beneath any root")
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise UnsafePathError(f"{rel!r}: '..' is not allowed")
    if not parts:
        raise UnsafePathError(f"{rel!r}: names no file below the root")
    return parts


def _refuse_link(name: str, dir_fd: int, rel: str, err: OSError) -> None:
    """Raises `UnsafePathError` when `name` is a symlink, else re-raises `err`."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        raise err from None
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePathError(f"{rel!r}: component {name!r} is a symlink") from err
    raise err


def open_beneath(root: Path, rel: str, flags: int = os.O_RDONLY) -> int:
    """Opens `rel` below `root` without following any symlink and returns the descriptor.

    The caller closes the descriptor. A file created through `os.O_CREAT` gets mode 0600.
    Raises `UnsafePathError` for `..`, an absolute or empty `rel`, a NUL, or a symlink in any
    component, and the usual `OSError` (for example `FileNotFoundError`) otherwise.
    """
    parts = _components(rel)
    fd = os.open(root, _DIR_FLAGS)
    try:
        for name in parts[:-1]:
            try:
                next_fd = os.open(name, _NOFOLLOW_DIR_FLAGS, dir_fd=fd)
            except OSError as err:
                if err.errno in (errno.ELOOP, errno.ENOTDIR):
                    _refuse_link(name, fd, rel, err)
                raise
            os.close(fd)
            fd = next_fd
        last = parts[-1]
        try:
            return os.open(last, flags | os.O_NOFOLLOW | os.O_CLOEXEC, CREATED_FILE_MODE, dir_fd=fd)
        except OSError as err:
            if err.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                _refuse_link(last, fd, rel, err)
            raise
    finally:
        os.close(fd)


def open_regular_beneath(root: Path, rel: str, flags: int = os.O_RDONLY) -> int:
    """Like `open_beneath`, but the target must be a regular file; the descriptor is blocking.

    The open itself is non-blocking, so a FIFO cannot stall it (a FIFO with no reader fails a
    write open with `ENXIO`). Raises `UnsafePathError` for a FIFO, socket, device or directory.
    """
    fd = open_beneath(root, rel, flags | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafePathError(f"{rel!r} is not a regular file")
        os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _denied(path: Path, policy: WalkPolicy) -> bool:
    return any(is_beneath(d, path) for d in policy.deny)


def _glob_denied(path: Path, policy: WalkPolicy) -> bool:
    text = os.fspath(path)
    for pattern in policy.deny_globs:
        target = text if "/" in pattern else path.name
        if fnmatch.fnmatchcase(target, pattern):
            return True
    return False


def walk(root: Path, *, policy: WalkPolicy) -> Iterator[Path]:
    """Yields the files and directories below `root` that `policy` allows, depth first.

    `root` must be absolute, resolved, under one of `policy.allow_roots` and under none of
    `policy.deny`, else `UnsafePathError`. Symlinks are never yielded or followed; denied
    directories are not entered; files matching `deny_globs` are not yielded; the walk stops
    after `policy.max_entries` paths. Directories that cannot be listed are skipped.
    """
    root = Path(root)
    if not root.is_absolute() or os.path.realpath(root) != os.path.normpath(os.fspath(root)):
        raise UnsafePathError(f"{root}: the walk root must be absolute and resolved")
    if not any(is_beneath(r, root) for r in policy.allow_roots):
        raise UnsafePathError(f"{root}: not under any read root")
    if _denied(root, policy) or _glob_denied(root, policy):
        raise UnsafePathError(f"{root}: denied by policy")
    yielded = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                children = sorted(entries, key=lambda e: e.name)
        except OSError:
            continue
        subdirs = []
        for entry in children:
            if entry.is_symlink():
                continue
            path = Path(entry.path)
            if _denied(path, policy) or _glob_denied(path, policy):
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            if not is_dir and not entry.is_file(follow_symlinks=False):
                continue  # sockets, fifos and devices are never tool data
            if yielded >= policy.max_entries:
                return
            yielded += 1
            yield path
            if is_dir:
                subdirs.append(path)
        stack.extend(reversed(subdirs))
