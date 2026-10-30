"""Project instructions: `AGENTS.md` (or `CLAUDE.md`) from the workspace root down to the cwd.

Instruction files are repository content and may be hostile. That is acceptable because no
policy depends on prompt text; the box and the broker are the boundaries (design 03-agent, 3.12.1).
A nearer file comes later, and the rendered text says the later one wins on a conflict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_CHARS = 16_000
IMPORT_DEPTH = 2
NAMES = ("AGENTS.md", "CLAUDE.md")
IMPORT_RE = re.compile(r"^@(\S+)\s*$")
INTRO = "When these files disagree, the later one wins."


@dataclass(frozen=True)
class Instructions:
    """The rendered "Project instructions" body and the files it came from."""

    text: str
    files: tuple[str, ...]
    truncated: bool


def _expand(path: Path, root: Path, depth: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if depth >= IMPORT_DEPTH:
        return text
    out = []
    for line in text.splitlines():
        m = IMPORT_RE.match(line.strip())
        target = (path.parent / m[1]).resolve() if m else None
        if target and target.is_file() and (target == root or root in target.parents):
            out.append(_expand(target, root, depth + 1))
        else:
            out.append(line)
    return "\n".join(out)


def _only_imports(path: Path, sibling: Path) -> bool:
    lines = [
        ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()
    ]
    return (
        len(lines) == 1
        and (m := IMPORT_RE.match(lines[0])) is not None
        and (path.parent / m[1]).resolve() == sibling.resolve()
    )


def load_instructions(
    workspace_root: Path, cwd: Path, user_file: Path | None = None, max_chars: int = DEFAULT_MAX_CHARS
) -> Instructions:
    """Collects the instruction files, root first; cuts from the root end past `max_chars`."""
    root = workspace_root.resolve()
    cwd = cwd.resolve()
    if cwd != root and root not in cwd.parents:
        raise ValueError(f"cwd {cwd} is outside the workspace {root}")
    dirs = sorted(
        {d for d in [cwd, *cwd.parents] if d == root or root in d.parents}, key=lambda d: len(d.parts)
    )
    found: list[tuple[str, str]] = []
    if user_file and user_file.is_file():
        found.append((str(user_file), _expand(user_file, user_file.parent, IMPORT_DEPTH)))
    for d in dirs:
        agents, claude = d / NAMES[0], d / NAMES[1]
        path = agents if agents.is_file() else claude if claude.is_file() else None
        if path is None or (path == claude and agents.is_file()):
            continue
        if path == claude and _only_imports(claude, agents):
            continue
        found.append((path.relative_to(root).as_posix(), _expand(path, root, 0)))
    if not found:
        return Instructions("", (), False)
    kept = list(found)
    cut: list[str] = []
    while len(kept) > 1 and _size(kept) > max_chars:
        cut.append(kept.pop(0)[0])
    body = _render(kept)
    truncated = bool(cut)
    if len(kept) == 1 and len(body) > max_chars:
        body, truncated = body[:max_chars], True
    if cut:
        body += f"\n[hal] instructions cut: {', '.join(cut)}"
    return Instructions(body, tuple(n for n, _ in found), truncated)


def _size(items: list[tuple[str, str]]) -> int:
    return len(_render(items))


def _render(items: list[tuple[str, str]]) -> str:
    parts = [INTRO] if len(items) > 1 else []
    parts += [f"### From {name}\n\n{text.strip()}" for name, text in items]
    return "\n\n".join(parts)
