"""`@path` mentions: index the workspace for completion and attach mentioned files to a prompt.

Files are read below the workspace root without following symlinks (`halkit.fsafe.open_regular_beneath`),
so a link a model planted in the copy cannot pull a host file into the context. A file over the
byte cap is skipped with a note rather than cut, because half a file misleads a small model.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from halkit import fsafe

from .fuzzy import rank, score

SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".ruff_cache", "dist", "build"})
MAX_INDEX = 5000  # enough for a mid-sized repo, small enough to walk in a few milliseconds
INDEX_TTL_S = 10.0
MENTION_RE = re.compile(r"(?<!\S)@([^\s@]+)")
Block = dict[str, object]
_cache: dict[Path, tuple[float, list[str]]] = {}


def index_files(root: Path, *, limit: int = MAX_INDEX) -> list[str]:
    """Relative paths of regular files below `root` (cached for a few seconds), skipping tool and build folders."""
    now = time.monotonic()
    hit = _cache.get(root)
    if hit and now - hit[0] < INDEX_TTL_S:
        return hit[1]
    found: list[str] = []
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(base, d)))
        for name in sorted(names):
            full = os.path.join(base, name)
            if not os.path.islink(full):
                found.append(os.path.relpath(full, root))
            if len(found) >= limit:
                break
        if len(found) >= limit:
            break
    _cache[root] = (now, found)
    return found


def fuzzy(query: str, text: str) -> bool:
    """True when every character of `query` appears in `text` in order, ignoring case."""
    return score(query, text) is not None


def match_files(root: Path, query: str, *, count: int = 40) -> list[str]:
    """Paths that fuzzy-match `query`, best first (basename and word-start hits rank higher)."""
    return rank(query, index_files(root), limit=count)


def attach_mentions(text: str, root: Path | None, max_bytes: int) -> tuple[list[Block], list[str]]:
    """Text blocks for each `@path` in `text` that names a readable file, and notes for the ones skipped."""
    blocks: list[Block] = []
    notes: list[str] = []
    if root is None:
        return blocks, notes
    seen: set[str] = set()
    for rel in MENTION_RE.findall(text):
        if rel in seen:
            continue
        seen.add(rel)
        try:
            fd = fsafe.open_regular_beneath(root, rel)
        except (fsafe.UnsafePathError, OSError):
            continue  # not a file in the workspace: it was just an @ in the text
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            notes.append(f"@{rel} skipped: over {max_bytes} bytes")
            continue
        try:
            body = data.decode("utf-8")
        except UnicodeDecodeError:
            notes.append(f"@{rel} skipped: not UTF-8 text")
            continue
        blocks.append({"type": "text", "text": f'<file path="{rel}">\n{body}\n</file>'})
        notes.append(f"attached @{rel} ({len(data)} bytes)")
    return blocks, notes
