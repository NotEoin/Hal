"""The `repo_map` tool: files and their top-level symbols, ranked by the conversation.

An Aider-style outline that lets a small model find the right file in one call instead of five
`grep`s. Python files are parsed with `ast`; other languages use a line regex for definitions, so
no dependency is needed. Files are ranked by how many words of the query appear in their path and
symbols, then by size, and the output stops at a token budget (estimated at 4 characters a token).
The result is deterministic for the same tree and query, which keeps the prompt cache valid.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from halagent.tools.paths import Workspace
from halagent.tools.search import SKIP_DIRS

DEFAULT_MAP_TOKENS = 1_000
MAX_MAP_TOKENS = 4_000
CHARS_PER_TOKEN = 4
MAX_FILES_SCANNED = 3_000
MAX_FILE_BYTES = 200_000
MAX_SYMBOLS_PER_FILE = 25
SOURCE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".lua", ".sh",
}  # fmt: skip
DEF_LINE = re.compile(
    r"^(?:export\s+)?(?:pub(?:\([a-z]+\))?\s+)?(?:async\s+)?(?:static\s+)?"
    r"(?:def|fn|func|function|class|struct|enum|trait|interface|type|impl|module|public\s+class)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)
WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")


def python_symbols(source: str) -> list[str]:
    """Top-level classes (with their methods) and functions of Python `source`, or [] on a syntax error."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [
                n.name
                for n in node.body
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and not n.name.startswith("_")
            ]
            out.append(f"class {node.name}" + (f": {', '.join(methods[:8])}" if methods else ""))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            out.append(f"def {node.name}")
    return out


def regex_symbols(source: str) -> list[str]:
    """Top-level definitions found by a line regex, for languages without a parser here."""
    out = []
    for line in source.splitlines():
        m = DEF_LINE.match(line)
        if m:
            out.append(line.strip().split("{")[0].split("(")[0].strip()[:80])
    return out


def file_symbols(path: Path) -> list[str]:
    """The symbols of one source file."""
    try:
        text = path.read_text("utf-8", "replace")
    except OSError:
        return []
    found = python_symbols(text) if path.suffix == ".py" else regex_symbols(text)
    return found[:MAX_SYMBOLS_PER_FILE]


def _score(rel: str, symbols: list[str], words: set[str]) -> int:
    haystack = (rel + " " + " ".join(symbols)).lower()
    return sum(haystack.count(w) for w in words)


def build_map(root: Path, query: str = "", max_tokens: int = DEFAULT_MAP_TOKENS) -> str:
    """The map text for `root`, most relevant files first, cut at `max_tokens`."""
    words = {w.lower() for w in WORD.findall(query)}
    rows: list[tuple[int, str, list[str]]] = []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            p = Path(dirpath) / name
            if p.suffix not in SOURCE_SUFFIXES or len(rows) >= MAX_FILES_SCANNED:
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            rel = p.relative_to(root).as_posix()
            symbols = file_symbols(p)
            rows.append((_score(rel, symbols, words), rel, symbols))
    rows.sort(key=lambda r: (-r[0], r[1]))  # ties stay alphabetical so the output is stable
    budget = max(1, min(max_tokens, MAX_MAP_TOKENS)) * CHARS_PER_TOKEN
    lines: list[str] = []
    used = 0
    for _, rel, symbols in rows:
        block = [rel] + [f"  {s}" for s in symbols]
        size = sum(len(x) + 1 for x in block)
        if used + size > budget:
            if used + len(rel) + 1 <= budget:  # keep listing bare paths while they fit
                lines.append(rel)
                used += len(rel) + 1
            continue
        lines.extend(block)
        used += size
    if not lines:
        return "No source files found."
    return (
        "\n".join(lines) + f"\n[hal] {len(rows)} source files; call repo_map with query words to rank them."
    )


def repo_map(ws: Workspace, args: dict) -> str:
    """Tool entry: `query` ranks files, `max_tokens` caps the size."""
    raw = args.get("max_tokens")
    try:
        cap = int(raw) if raw not in (None, "") else DEFAULT_MAP_TOKENS
    except (TypeError, ValueError):
        cap = DEFAULT_MAP_TOKENS
    return build_map(ws.root, str(args.get("query") or ""), cap)
