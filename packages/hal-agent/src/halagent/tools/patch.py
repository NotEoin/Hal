"""The `patch` tool: SEARCH/REPLACE blocks and unified diffs applied with the `edit` matcher.

Small local models write whichever patch format they were trained on, so both are accepted.
Each block or hunk is matched with the same four tiers as `edit` (exact, line-number stripped,
whitespace-insensitive, escaped newlines), and the whole patch is all-or-nothing: if one block
fails, the file is not touched and the message names the block to fix.
"""

from __future__ import annotations

import re

from halagent.tools import files
from halagent.tools.paths import ToolError, Workspace

BLOCK = re.compile(
    r"^<{5,9} ?SEARCH[ \t]*\n(?P<old>.*?)\n?={5,9}[ \t]*\n(?P<new>.*?)\n?>{5,9} ?REPLACE[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
HUNK_HEADER = re.compile(r"^@@ .*@@.*$")
FILE_HEADERS = ("--- ", "+++ ", "diff ", "index ")
RETRY_HINT = "Read the file again, copy the lines exactly, and send only the blocks that still apply."


def parse_blocks(patch: str) -> list[tuple[str, str]]:
    """The `(old, new)` pairs of a patch, from SEARCH/REPLACE blocks or unified-diff hunks."""
    blocks = [(m["old"], m["new"]) for m in BLOCK.finditer(patch)]
    if blocks:
        return blocks
    return _hunks(patch)


def _hunks(patch: str) -> list[tuple[str, str]]:
    pairs: list[tuple[list[str], list[str]]] = []
    current: tuple[list[str], list[str]] | None = None
    for line in patch.split("\n"):
        if HUNK_HEADER.match(line):
            current = ([], [])
            pairs.append(current)
        elif current is None or line.startswith(FILE_HEADERS) and not line.startswith(("--- ", "+++ ")):
            continue
        elif line.startswith("+") and not line.startswith("+++ "):
            current[1].append(line[1:])
        elif line.startswith("-") and not line.startswith("--- "):
            current[0].append(line[1:])
        elif line.startswith(" ") or line == "":
            current[0].append(line[1:])
            current[1].append(line[1:])
    return [("\n".join(o).strip("\n"), "\n".join(n).strip("\n")) for o, n in pairs if o or n]


def patch(ws: Workspace, args: dict, *, syntax_guard: bool = True) -> str:
    """Applies every block or hunk of `patch` to `path`; returns a short diff."""
    text_in = args.get("patch")
    if not isinstance(text_in, str) or not text_in.strip():
        raise ToolError("invalid_args", "patch must be SEARCH/REPLACE blocks or a unified diff")
    blocks = parse_blocks(text_in)
    if not blocks:
        raise ToolError(
            "invalid_args",
            "no SEARCH/REPLACE block or diff hunk found. Use:\n"
            "<<<<<<< SEARCH\nold lines\n=======\nnew lines\n>>>>>>> REPLACE",
        )
    path = ws.resolve(args.get("path"), write=True)
    shown = ws.display(path)
    original = files._text(path, shown)
    updated = original
    for n, (old, new) in enumerate(blocks, 1):
        if not old.strip():
            raise ToolError("invalid_args", f"block {n} has an empty SEARCH part; use write for new files")
        try:
            updated, _tier = files._find(updated, old, new)
        except ToolError as exc:
            raise ToolError(
                exc.error_type, f"block {n} of {len(blocks)}: {exc.message}\n{RETRY_HINT}"
            ) from exc
    if updated == original:
        raise ToolError("invalid_args", "the patch changes nothing")
    if syntax_guard and files._syntax_error(path, original) is None:
        bad = files._syntax_error(path, updated)
        if bad:
            raise ToolError("syntax", f"the patch would break {shown} ({bad}); the file was not changed")
    files._atomic_write(path, updated)
    plural = "s" if len(blocks) != 1 else ""
    return f"Patched {shown} ({len(blocks)} block{plural}).\n{files._diff(shown, original, updated)}"
