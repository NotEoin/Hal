"""Workspace path confinement: every file tool resolves its paths here.

`realpath` runs before the containment check so a symlink out of the root is refused. Writes into
protected globs or read-only directories are refused too. The box mounts `.git` read-only; the
`protect` globs make the refusal explicit for the model and cover a run without a box.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PROTECT = (".git", ".git/**")


class ToolError(Exception):
    """A tool call failed in a way the model should see and can fix; `error_type` is its class."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.message = message


@dataclass(frozen=True)
class Workspace:
    """Where file tools may read and write: a root, a scratch dir and the refused paths."""

    root: Path
    scratch: Path | None = None
    readonly: tuple[Path, ...] = ()
    protect: tuple[str, ...] = DEFAULT_PROTECT
    _real: Path = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        real = Path(self.root).resolve()
        if not real.is_dir():
            raise ValueError(f"workspace root is not a directory: {self.root}")
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "root", real)

    def _inside(self, real: Path, base: Path) -> bool:
        return real == base or base in real.parents

    def resolve(self, path: object, *, write: bool = False) -> Path:
        """Returns the real path; raises `ToolError` (`outside_workspace` or `protected`)."""
        if not isinstance(path, str) or not path:
            raise ToolError("invalid_args", "path must be a non-empty string")
        real = Path(os.path.realpath(self.root / path))
        allowed = [self.root] + ([self.scratch.resolve()] if self.scratch else [])
        allowed += [p.resolve() for p in self.readonly]
        if not any(self._inside(real, base) for base in allowed):
            raise ToolError("outside_workspace", f"{path} is outside the project directory")
        if write:
            if any(self._inside(real, p.resolve()) for p in self.readonly):
                raise ToolError("protected", f"{path} is read-only")
            if self._inside(real, self.root):
                rel = real.relative_to(self.root).as_posix()
                if any(fnmatch.fnmatch(rel, g) for g in self.protect):
                    raise ToolError("protected", f"{path} is protected and cannot be changed")
        return real

    def display(self, path: Path) -> str:
        """The path relative to the root when inside it, else absolute."""
        return path.relative_to(self.root).as_posix() if self._inside(path, self.root) else str(path)
