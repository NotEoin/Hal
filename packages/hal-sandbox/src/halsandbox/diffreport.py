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
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .gitops import GitRunner

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
    path: str = ""


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


DEFAULT_MAX_PATCH_BYTES = 2 * 2**20  # a patch past 2 MiB is cut; the stat and flags still cover everything
ZERO_MODE = "000000"
_STATUS_KEEP = ("A", "M", "D", "T")


@dataclass(frozen=True)
class FileChange:
    """One changed path; `T` is a type change such as file to symlink."""

    path: str
    status: str
    old_mode: str | None
    new_mode: str | None


@dataclass(frozen=True)
class DiffReport:
    """What `/diff` and `accept` show: patch, stat, changed files and risk flags (REQ-BOX-50)."""

    base: str
    head_tree: str
    files: tuple[FileChange, ...]
    flags: tuple[Flag, ...]
    stat: str
    patch: str
    truncated: bool

    @property
    def high(self) -> tuple[Flag, ...]:
        """The flags `accept` refuses unless told otherwise."""
        return tuple(f for f in self.flags if f.severity == HIGH)


PRIVATE_KEY_RE = re.compile(rb"^\+.*-----BEGIN [A-Z ]*PRIVATE KEY-----", re.M)
_PATCH_FILE_RE = re.compile(rb"^diff --git a/.* b/(.*)$", re.M)


def _secret_paths(patch: bytes) -> list[str]:
    """Paths whose added lines hold a private key header (the patch is scanned in full, never cut)."""
    heads = list(_PATCH_FILE_RE.finditer(patch))
    found: list[str] = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(patch)
        if PRIVATE_KEY_RE.search(patch, head.end(), end):
            found.append(head.group(1).decode(errors="replace"))
    return found


def _parse_raw(raw: bytes) -> list[tuple[FileChange, str]]:
    """Parses `diff-tree --raw -z` output into changes with the new blob id."""
    parts = raw.decode(errors="surrogateescape").split("\0")
    out: list[tuple[FileChange, str]] = []
    i = 0
    while i + 1 < len(parts) and parts[i].startswith(":"):
        old_mode, new_mode, _old_sha, new_sha, status = parts[i][1:].split(" ")
        path = parts[i + 1]
        i += 2
        if status[0] in _STATUS_KEEP:
            out.append(
                (
                    FileChange(
                        path,
                        status[0],
                        None if old_mode == ZERO_MODE else old_mode,
                        None if new_mode == ZERO_MODE else new_mode,
                    ),
                    new_sha,
                )
            )
    return out


def build_report(
    git: GitRunner,
    base_commit: str,
    base_tree: str,
    head_tree: str,
    skipped: Sequence[dict[str, object]] = (),
    *,
    max_patch_bytes: int = DEFAULT_MAX_PATCH_BYTES,
) -> DiffReport:
    """Diffs `base_tree` against `head_tree` and rates every changed path.

    `skipped` is the checkpoint skip list (large files and special files left out of the tree).
    The symlink target is read from the committed blob, so a flag describes what would be applied.
    """
    raw = git.run("diff-tree", "-r", "-z", "--raw", "--no-renames", base_tree, head_tree).stdout
    files: list[FileChange] = []
    flags: list[Flag] = []
    for change, sha in _parse_raw(raw):
        files.append(change)
        target = None
        if change.new_mode == MODE_SYMLINK:
            target = git.run("cat-file", "blob", sha).stdout.decode(errors="replace")
        for f in flags_for(
            change.path, change.old_mode or ZERO_MODE, change.new_mode or ZERO_MODE, target=target
        ):
            flags.append(Flag(f.kind, f.severity, f.reason, change.path))
    for s in skipped:
        kind = "special_file" if s.get("special") else "skipped_large"
        why = (
            "not a regular file, left out of the checkpoint"
            if s.get("special")
            else f"{s['bytes']} bytes, over the size limit"
        )
        flags.append(Flag(kind, MEDIUM, why, str(s["path"])))
    stat = git.out("diff", "--stat", base_tree, head_tree)
    patch = git.run("diff", "--binary", base_tree, head_tree).stdout
    flags.extend(Flag("secret", HIGH, "private key in added lines", path) for path in _secret_paths(patch))
    truncated = len(patch) > max_patch_bytes
    text = patch[:max_patch_bytes].decode(errors="replace")
    return DiffReport(base_commit, head_tree, tuple(files), tuple(flags), stat, text, truncated)
