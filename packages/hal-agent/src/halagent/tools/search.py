"""The `grep` and `glob` tools: ripgrep when installed, a Python walk otherwise.

A search with no match is a success, because an error invites retries (design 03-agent, 3.7.4).
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from halagent.tools.paths import ToolError, Workspace

MAX_MATCHES = 100
MAX_GLOB = 200
MAX_OUTPUT_CHARS = 8_000
MAX_FILE_BYTES = 1_000_000
MAX_LINE_CHARS = 300
RG_TIMEOUT_S = 20
RG_BAD_REGEX = 2
BAD_REGEX_NOTE = "\n[hal] the pattern is not a valid regular expression; searched for the exact text instead."
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__"}


def _walk(base: Path):
    if base.is_file():
        yield base
        return
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            p = Path(dirpath) / name
            try:
                if p.stat().st_size <= MAX_FILE_BYTES:
                    yield p
            except OSError:
                # a file that vanished while walking is skipped
                continue


def _rg(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=RG_TIMEOUT_S,
            stdin=subprocess.DEVNULL, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        raise ToolError(
            "timeout", f"the search took more than {RG_TIMEOUT_S} s; narrow path or pattern"
        ) from exc


def _format(hits: list[str], pattern: str, where: str, note: str = "") -> str:
    if not hits:
        return f"No matches for {pattern!r} in {where}. Try a shorter or different identifier.{note}"
    hits = hits[:MAX_MATCHES]
    files = len({h.split(":", 1)[0] for h in hits})
    body = "\n".join(hits)
    if len(body) > MAX_OUTPUT_CHARS:
        body = body[:MAX_OUTPUT_CHARS] + "\n[... output cut ...]"
    return f"{len(hits)} matches in {files} files\n{body}{note}"


def _py_grep(ws: Workspace, pattern: str, base: Path, glob: str) -> list[str]:
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    hits: list[str] = []
    for p in _walk(base):
        if glob and not fnmatch.fnmatch(p.name, glob):
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            # binary or unreadable files cannot match a text search
            continue
        for n, line in enumerate(lines, 1):
            if rx.search(line):
                hits.append(f"{ws.display(p)}:{n}: {line[:MAX_LINE_CHARS]}")
                if len(hits) >= MAX_MATCHES:
                    return hits
    return hits


def grep(ws: Workspace, args: dict) -> str:
    """`path:line: text` matches, at most 100."""
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("invalid_args", "pattern must be non-empty text")
    base = ws.resolve(args.get("path") or ".")
    glob = str(args.get("glob") or "")
    where = ws.display(base) if base != ws.root else "the project"
    note = ""
    if shutil.which("rg"):
        argv = ["rg", "--line-number", "--no-heading", "--color", "never"]
        argv += ["--max-columns", str(MAX_LINE_CHARS), "--max-columns-preview"]
        argv += ["--max-count", "20", "--max-filesize", "1M"]
        if glob:
            argv += ["--glob", glob]
        done = _rg([*argv, "--regexp", pattern, "--", str(base)], ws.root)
        if done.returncode == RG_BAD_REGEX:
            done = _rg([*argv, "--fixed-strings", "--regexp", pattern, "--", str(base)], ws.root)
            note = (
                "\n[hal] the pattern is not a valid regular expression; searched for the exact text instead."
            )
        hits = []
        for row in done.stdout.splitlines():
            path, _, rest = row.partition(":")
            hits.append(f"{ws.display(Path(path))}:{rest.replace(':', ': ', 1)}" if rest else row)
    else:
        try:
            re.compile(pattern)
        except re.error:
            note = (
                "\n[hal] the pattern is not a valid regular expression; searched for the exact text instead."
            )
        hits = _py_grep(ws, pattern, base, glob)
    return _format(hits, pattern, where, note)


def _glob_match(path: Path, base: Path, pattern: str) -> bool:
    if fnmatch.fnmatch(path.relative_to(base).as_posix(), pattern):
        return True
    return "/" not in pattern and fnmatch.fnmatch(path.name, pattern)


def glob(ws: Workspace, args: dict) -> str:
    """Sorted paths matching a glob, at most 200."""
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("invalid_args", "pattern must be non-empty text")
    base = ws.resolve(args.get("path") or ".")
    if shutil.which("rg"):
        done = _rg(["rg", "--files", "--glob", pattern, "--", str(base)], ws.root)
        found = sorted(ws.display(Path(p)) for p in done.stdout.splitlines())
    else:
        found = sorted(ws.display(p) for p in _walk(base) if _glob_match(p, base, pattern))
    if not found:
        return f"No files match {pattern!r}."
    more = f"\n[hal] {len(found) - MAX_GLOB} more not shown." if len(found) > MAX_GLOB else ""
    return f"{len(found)} files\n" + "\n".join(found[:MAX_GLOB]) + more
