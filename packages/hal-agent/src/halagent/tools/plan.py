"""The `todo` and `finish` tools. Both are free: they do not count toward the tool-call budget."""

from __future__ import annotations

import re

from halagent.tools.paths import ToolError

MAX_ITEMS = 20
MAX_ITEM_CHARS = 200
ITEM = re.compile(r"^\s*(?:[-*]|\d+[.)])\s*\[( |x|X|~|>)\]\s*(.+)$")
MARK = {" ": "[ ]", "x": "[x]", "X": "[x]", "~": "[~]", ">": "[~]"}


def parse_plan(plan: object) -> list[tuple[str, str]]:
    """Parses a checklist into (mark, text) pairs; a line without a checkbox is pending."""
    if not isinstance(plan, str) or not plan.strip():
        raise ToolError("invalid_args", "plan must be a checklist, one '- [ ] step' per line")
    items = []
    for line in plan.splitlines():
        if not line.strip():
            continue
        m = ITEM.match(line)
        mark, text = (MARK[m.group(1)], m.group(2).strip()) if m else ("[ ]", line.strip(" -*\t"))
        items.append((mark, text))
    if len(items) > MAX_ITEMS or any(len(t) > MAX_ITEM_CHARS for _, t in items):
        raise ToolError(
            "invalid_args", f"a plan has at most {MAX_ITEMS} steps of {MAX_ITEM_CHARS} characters"
        )
    return items


def todo(args: dict) -> str:
    """Normalises the checklist and returns it."""
    return "Plan recorded:\n" + "\n".join(f"- {m} {t}" for m, t in parse_plan(args.get("plan")))


def finish(args: dict) -> str:
    """Returns the summary that ends the turn."""
    summary = args.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ToolError("invalid_args", "summary must be non-empty text")
    return summary.strip()
