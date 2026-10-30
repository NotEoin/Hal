"""Launch target resolution: decides whether a bare `hal` opens code or chat, and in which folder.

A project root is never `/`, `$HOME`, an ancestor of `$HOME`, a Hal directory or anything on the
hard-deny list of the policy (REQ-CLI-09): mounting one would break the box rule that nothing
else from the host is visible. Git detection runs `git rev-parse` with a timeout, so a hung
repository cannot hang the launcher.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from halhub.policy.profiles import hard_deny_paths
from halkit import paths, proc

GIT_TIMEOUT_S = 5.0  # REQ-CLI-07: every subprocess has a timeout
PROJECT_MARKERS = (
    "AGENTS.md",
    "CLAUDE.md",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "CMakeLists.txt",
    ".agents",
)

Mode = Literal["code", "chat"]


class ForbiddenRoot(Exception):
    """A folder that may not be a workspace; carries the path and the rule that forbids it."""

    def __init__(self, path: Path, rule: str) -> None:
        super().__init__(f"{path} cannot be a workspace: {rule}")
        self.path = path
        self.rule = rule


@dataclass(frozen=True)
class LaunchTarget:
    """What to open: the mode, the project root (None for chat), and why."""

    mode: Mode
    root: Path | None
    is_git: bool
    reason: str


def _beneath(base: Path, path: Path) -> bool:
    return path == base or base in path.parents


def forbidden_rule(path: Path, *, home: Path) -> str | None:
    """The name of the rule that forbids `path` as a workspace, or None when it is allowed."""
    path = path.resolve()
    home = home.resolve()
    if path == Path(path.anchor):
        return "the filesystem root is never a workspace"
    if path == home or path in home.parents:
        return "your home directory is never a workspace"
    hal_dirs = (paths.config_dir(), paths.state_dir(), paths.data_dir(), home / ".config", home / ".local")
    for base in hal_dirs:
        if _beneath(base.resolve(), path):
            return f"{base} holds Hal and desktop state"
    for base in hard_deny_paths(home=home):
        base = base.resolve()
        if _beneath(base, path) or _beneath(path, base):
            return f"{base} is on the hard-deny list"
    return None


def git_toplevel(cwd: Path) -> Path | None:
    """The top level of the git work tree holding `cwd`, or None outside one."""
    try:
        out = proc.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""
    return Path(text) if text else None


def _marker(cwd: Path) -> str | None:
    return next((m for m in PROJECT_MARKERS if (cwd / m).exists()), None)


def resolve_target(cwd: Path, requested: Literal["auto", "code", "chat"] = "auto", *, home: Path) -> LaunchTarget:
    """Picks the launch target for `cwd` following the table in docs/design/06-core-cli.md.

    `requested="chat"` ignores the folder. `requested="code"` raises `ForbiddenRoot` where `auto`
    would fall back to chat.
    """
    cwd = cwd.resolve()
    if requested == "chat":
        return LaunchTarget("chat", None, False, "chat requested")
    cwd_rule = forbidden_rule(cwd, home=home)
    top = git_toplevel(cwd)
    marker = _marker(cwd)
    if cwd_rule is None and top is not None and forbidden_rule(top, home=home) is None:
        return LaunchTarget("code", top, True, "git repository")
    if cwd_rule is None and top is not None:  # the git root is forbidden, the folder is not
        if marker:
            return LaunchTarget("code", cwd, True, f"project marker {marker}; git root is your home")
        rule = forbidden_rule(top, home=home) or ""
        return _fallback(requested, top, rule)
    if cwd_rule is None and marker:
        return LaunchTarget("code", cwd, False, f"project marker {marker}")
    if cwd_rule is None:
        if requested == "code":
            return LaunchTarget("code", cwd, False, "no project here")
        return LaunchTarget("chat", None, False, "no project here")
    return _fallback(requested, cwd, cwd_rule)


def _fallback(requested: str, path: Path, rule: str) -> LaunchTarget:
    if requested == "code":
        raise ForbiddenRoot(path, rule)
    return LaunchTarget("chat", None, False, rule)
