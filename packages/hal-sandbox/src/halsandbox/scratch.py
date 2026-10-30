"""The per-project scratchpad mounted at `/scratch` (RESOLUTIONS I3).

It is the one place a box may keep files across sessions that is not the project. It lives under
`state_dir()/scratch/<project-key>/`, never inside the project, and has a size cap that is checked
before each session because a bind mount cannot enforce one itself.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from halkit import paths

from .errors import SandboxError

KEY_HEX_CHARS = 12  # enough to avoid clashes between one person's projects


def project_key(project: Path) -> str:
    """A stable directory name for `project`: its folder name plus a short hash of the full path."""
    resolved = project.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:KEY_HEX_CHARS]
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in resolved.name) or "root"
    return f"{safe}-{digest}"


def scratch_root() -> Path:
    """The directory holding every project's scratchpad."""
    return paths.state_dir() / "scratch"


def dir_bytes(path: Path) -> int:
    """Total size of regular files under `path`, not following symlinks."""
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def prepare(project: Path, max_bytes: int) -> Path:
    """Creates the project's scratchpad (private) and returns it.

    Raises `SandboxError` when it already holds more than `max_bytes`; `hal scratch clean` empties it.
    """
    root = paths.ensure_private_dir(scratch_root())
    path = root / project_key(project)
    path.mkdir(mode=0o700, exist_ok=True)
    used = dir_bytes(path)
    if used > max_bytes:
        raise SandboxError(
            f"scratchpad {path} holds {used} bytes, over the {max_bytes} cap; run `hal scratch clean`"
        )
    return path


def clean(project: Path | None = None) -> list[Path]:
    """Deletes one project's scratchpad, or all of them; returns what was removed."""
    root = scratch_root()
    if not root.is_dir():
        return []
    targets = [root / project_key(project)] if project else sorted(p for p in root.iterdir() if p.is_dir())
    removed = [t for t in targets if t.is_dir()]
    for t in removed:
        shutil.rmtree(t)
    return removed
