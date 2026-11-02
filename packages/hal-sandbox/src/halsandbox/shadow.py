"""Checkpoints for folders that are not git repositories, in a shadow repository (REQ-BOX-52).

The shadow git dir lives at `$STATE/shadow/<project-key>/` and the folder is its work tree, so
the folder itself stays free of `.git`. Objects are stored once (deduplicated), the restore and
diff code is the git one, and a turn costs one `git add`. The user's `.gitignore` files are still
honoured. A folder whose non-ignored content exceeds `nongit_max_total_bytes` is refused: the
caller must use the isolated copy or `--no-checkpoint` (a full copy per turn is not affordable).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from halkit import paths

from .checkpoints import CheckpointConfig, CheckpointError, Checkpoints
from .config import DEFAULT_NONGIT_TOTAL_BYTES
from .gitops import GitRunner
from .scratch import project_key

log = logging.getLogger(__name__)


def shadow_root() -> Path:
    """Where shadow repositories live."""
    return paths.state_dir() / "shadow"


def shadow_dir(work: Path) -> Path:
    """The shadow git dir of `work`."""
    return shadow_root() / project_key(work)


def open_checkpoints(
    work: Path,
    sid: str,
    cfg: CheckpointConfig | None = None,
    *,
    nongit_max_total_bytes: int = DEFAULT_NONGIT_TOTAL_BYTES,
) -> Checkpoints:
    """`Checkpoints` for `work`: its own `.git` when it has one, else a shadow repository.

    Raises `CheckpointError` when a non-git folder holds more than `nongit_max_total_bytes` of
    non-ignored files, naming the two ways out.
    """
    cfg = cfg or CheckpointConfig()
    if (work / ".git").is_dir():
        return Checkpoints.for_work(work, sid, cfg)
    if (work / ".git").exists():
        raise CheckpointError(
            f"{work}/.git is a file (a linked worktree or submodule); checkpoints need a plain repo"
        )
    gitdir = shadow_dir(work)
    git = GitRunner(work, git_dir=gitdir, timeout_s=cfg.git_timeout_s)
    if not gitdir.is_dir():
        paths.ensure_private_dir(gitdir.parent)
        git.run("init", "-q", "--bare", str(gitdir))
    ckpt = Checkpoints(work, sid, git, cfg)
    total = _tree_bytes(ckpt)
    if total > nongit_max_total_bytes:
        raise CheckpointError(
            f"{work} is not a git repository and holds {total} bytes of non-ignored files, over the "
            f"{nongit_max_total_bytes} byte checkpoint limit: start with --isolated or --no-checkpoint"
        )
    return ckpt


def _tree_bytes(ckpt: Checkpoints) -> int:
    raw = ckpt.git.run("ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
    total = 0
    for rel in raw.decode(errors="surrogateescape").split("\0"):
        if not rel:
            continue
        try:
            st = os.lstat(ckpt.work / rel)
        except OSError:
            # a path that vanished since the checkpoint has no state to compare
            continue
        total += st.st_size
    return total
