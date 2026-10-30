"""Risk flags for a diff report: the workspace scan of 01-security (REQ-SEC-70, REQ-BOX-50).

`flags_for` rates one changed path from facts the checkpoint diff already has (path, old and new
git mode, symlink target). It is advice for the person reviewing `/diff`; `accept` refuses only
the `high` flags unless told otherwise. Design decisions:

- Pure function, no I/O: the caller reads the symlink target from the committed blob, so the
  flag describes what would be applied, not what the work tree holds now.
- One pattern list (`AUTOMATION`), matching 01-security's "Workspace scan" table; 02 does not
  restate it. A pattern is `high` when the file runs on the host without anyone reading it
  (git hooks and CI, direnv, harness settings and MCP config), `medium` when it is run by habit.
- Matching is on the path below the project root, by full path or by file name, so a nested
  `sub/Makefile` is found as well as a top-level one.
"""

from __future__ import annotations

import fnmatch
import posixpath
from collections.abc import Iterable
from dataclasses import dataclass

HIGH = "high"
MEDIUM = "medium"
MODE_SYMLINK = "120000"
MODE_GITLINK = "160000"
MODE_EXEC = "100755"
SCRIPT_PATTERNS = ("*.sh", ".bashrc", ".zshrc", ".profile")  # flagged only when executable, except rc files
RC_FILES = (".bashrc", ".zshrc", ".profile")

# (patterns, severity, why); the single list for 01-security's "Workspace scan" table
AUTOMATION: tuple[tuple[tuple[str, ...], str, str], ...] = (
    ((".envrc", ".env", ".env.*"), HIGH, "direnv and dotenv loaders"),
    ((".github/*", ".gitlab-ci.yml"), HIGH, "runs in CI with repo secrets after a push"),
    ((".pre-commit-config.yaml", ".husky/*"), HIGH, "runs at commit"),
    ((".claude/*", ".mcp.json"), HIGH, "host harness hooks and MCP servers"),
    ((".codex/*", ".qwen/*", ".opencode/*", ".cursor/*", ".agents/skills/*"), MEDIUM, "other harness config"),
    ((".vscode/*", ".idea/*", "*.code-workspace"), MEDIUM, "editor tasks and settings"),
    (("Makefile", "*.mk", "justfile", "Taskfile.yml"), MEDIUM, "run by habit"),
    (
        ("package.json", "pyproject.toml", "setup.py", "conftest.py", "noxfile.py", "tox.ini"),
        MEDIUM,
        "run by test and install commands",
    ),
    (("*.desktop", "*.service", "*.timer"), MEDIUM, "launchers and units if copied out"),
    (("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md"), MEDIUM, "instructions later agents load"),
)  # fmt: skip
GIT_METADATA = (".gitattributes", ".gitmodules", ".gitconfig")


@dataclass(frozen=True)
class Flag:
    """One reason to look at a changed path."""

    kind: str  # nested_repo, symlink_outside, git_metadata, executable_added, automation, secret
    severity: str  # HIGH or MEDIUM
    reason: str


def _matches(path: str, patterns: Iterable[str]) -> bool:
    name = posixpath.basename(path)
    return any(
        fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) or fnmatch.fnmatch(path, f"*/{p}")
        for p in patterns
    )


def symlink_escapes(path: str, target: str) -> bool:
    """True when a symlink at `path` (relative to the root) points outside the root."""
    if posixpath.isabs(target):
        return True
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return resolved == ".." or resolved.startswith("../")


def flags_for(path: str, old_mode: str, new_mode: str, *, target: str | None = None) -> list[Flag]:
    """The flags for a path that now has `new_mode` (was `old_mode`, `000000` when new).

    `target` is the symlink target when `new_mode` is a symlink.
    """
    out: list[Flag] = []
    parts = path.split("/")
    if new_mode == MODE_GITLINK or ".git" in parts[:-1] or (parts[-1] == ".git" and new_mode != "000000"):
        out.append(Flag("nested_repo", HIGH, "a nested repository or git file can run commands in git tools"))
    if new_mode == MODE_SYMLINK and (target is None or symlink_escapes(path, target)):
        out.append(
            Flag(
                "symlink_outside", HIGH, f"symlink to {target if target is not None else 'an unknown target'}"
            )
        )
    if parts[-1] in GIT_METADATA:
        out.append(Flag("git_metadata", HIGH, "git filters, diff drivers and submodules"))
    if new_mode == MODE_EXEC and old_mode != MODE_EXEC:
        out.append(Flag("executable_added", MEDIUM, "a file made executable"))
    for patterns, severity, why in AUTOMATION:
        if _matches(path, patterns):
            out.append(Flag("automation", severity, why))
            break
    else:
        if _matches(path, RC_FILES) or (new_mode == MODE_EXEC and _matches(path, SCRIPT_PATTERNS)):
            out.append(Flag("automation", MEDIUM, "shell startup file or executable script"))
    return out


def sensitive_summary(flagged: int) -> str:
    """The one-line count shown in the turn and end-of-session summaries."""
    return "no sensitive files changed" if flagged == 0 else f"{flagged} sensitive files changed"
