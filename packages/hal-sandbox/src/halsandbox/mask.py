"""Secret masking: find files in a work tree that the box must see as empty (REQ-BOX-69).

Host hard-deny rules do not apply inside the box, so a `.env` or private key in the project would
be readable by the model. The walk descends into git-ignored directories on purpose (a `.env` or
`secrets/prod.pem` is usually ignored, which is exactly what masking is for) and never follows a
symlink out of the tree. A tree that is too big or too slow fails loudly instead of leaving files
unchecked. `MASK_DEFAULT` lives here until 01-security exports a shared list; it is the hub's
hard-deny globs plus common token files.
"""

from __future__ import annotations

import fnmatch
import os
import time
from collections.abc import Sequence
from pathlib import Path

from halhub.policy.profiles import HARD_DENY_GLOBS
from halkit import fsafe

from .errors import SandboxError

MASK_DEFAULT: tuple[str, ...] = (
    *HARD_DENY_GLOBS,
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    "*.p12",
    "*.pfx",
    "*.tfstate",
)
MASK_SKIP_DIRS = (".git", ".hal", "node_modules", ".venv", "venv")  # large, never hold user secrets
MASK_MAX_FILES = 256  # more matches than this means a data folder, not secrets
MASK_WALK_TIMEOUT_S = 10  # a tree that takes longer to walk fails loudly


class MaskError(SandboxError):
    """The work tree cannot be masked within the limits; the message names the fix."""


def resolve_patterns(setting: object) -> tuple[str, ...]:
    """The pattern list for a `[sandbox].mask` value: `"default"`, or the user's own list."""
    if setting == "default":
        return MASK_DEFAULT
    if isinstance(setting, list | tuple):
        return tuple(str(p) for p in setting)
    raise ValueError("[sandbox].mask must be 'default' or a list of patterns")


def find_masked(
    work: Path,
    patterns: Sequence[str],
    *,
    timeout_s: float = MASK_WALK_TIMEOUT_S,
    max_files: int = MASK_MAX_FILES,
) -> tuple[str, ...]:
    """Sorted work-relative paths of regular files whose name matches a pattern.

    A matching symlink is masked at its target when the target is a file inside `work`; a link
    pointing outside is skipped because the box cannot see its target anyway. Raises
    `MaskError` past `max_files` matches or `timeout_s`.
    """
    if not patterns:
        return ()
    root = work.resolve()
    deadline = time.monotonic() + timeout_s
    found: set[str] = set()
    stack = [root]
    while stack:
        if time.monotonic() > deadline:
            raise MaskError(
                f"masking walk of {work} took over {timeout_s:g}s; see --unmask and [sandbox].mask"
            )
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue  # unreadable directory: the box cannot read it either
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in MASK_SKIP_DIRS:
                    stack.append(Path(entry.path))
                continue
            if not any(fnmatch.fnmatchcase(entry.name, p) for p in patterns):
                continue
            target = Path(entry.path)
            if entry.is_symlink():
                target = Path(os.path.realpath(entry.path))
                if not fsafe.is_beneath(root, target) or not target.is_file():
                    continue
            elif not entry.is_file(follow_symlinks=False):
                continue
            found.add(target.relative_to(root).as_posix())
            if len(found) > max_files:
                raise MaskError(f"more than {max_files} files match [sandbox].mask in {work}; see --unmask")
    return tuple(sorted(found))
