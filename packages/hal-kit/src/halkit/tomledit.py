"""Small text edits of a TOML file that keep comments and unrelated tables.

`halkit.config` only reads TOML. `hal models` and `hal tune --write` must change a few tables of
the user's config without rewriting the whole file, so these functions work on the text:

- `set_table` replaces or appends a whole table (used for tables Hal owns, `[models.sources.X]`).
- `set_keys` changes some keys of a table and leaves its other lines alone (used for `[hal]`).
- `remove_table` deletes a table.

A table runs from its header line to the next header line. Values are strings, ints, floats,
bools and lists of those, written the way TOML expects. The result is parsed with `tomllib`
before it is returned, so a bad edit raises `ValueError` instead of corrupting the file.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping

HEADER = re.compile(r"^\s*\[{1,2}\s*([^\]]+?)\s*\]{1,2}\s*(#.*)?$")


def toml_value(value: object) -> str:
    """The TOML text of a scalar or list; raises `TypeError` for anything else."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)  # a JSON string is a valid TOML basic string, ASCII-escaped
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} as TOML")


def _span(lines: list[str], table: str) -> tuple[int, int] | None:
    """`(start, end)` line indexes of `[table]`, `end` exclusive, or None."""
    start = None
    for i, line in enumerate(lines):
        found = HEADER.match(line)
        if found is None:
            continue
        if start is not None:
            return start, i
        if found.group(1).strip() == table and not line.lstrip().startswith("[["):
            start = i
    return (start, len(lines)) if start is not None else None


def _check(text: str) -> str:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"edit would produce invalid TOML: {exc}") from exc
    return text


def _block(table: str, values: Mapping[str, object]) -> list[str]:
    return [f"[{table}]", *(f"{k} = {toml_value(v)}" for k, v in values.items()), ""]


def set_table(text: str, table: str, values: Mapping[str, object]) -> str:
    """Replaces `[table]` with `values`, or appends it."""
    lines = text.splitlines()
    span = _span(lines, table)
    block = _block(table, values)
    if span is None:
        body = lines + ([""] if lines and lines[-1].strip() else [])
        return _check("\n".join(body + block))
    start, end = span
    return _check("\n".join(lines[:start] + block + lines[end:]).rstrip("\n") + "\n")


def remove_table(text: str, table: str) -> str:
    """Deletes `[table]`; a missing table is not an error."""
    lines = text.splitlines()
    span = _span(lines, table)
    if span is None:
        return text
    start, end = span
    return _check("\n".join(lines[:start] + lines[end:]).rstrip("\n") + "\n")


def set_keys(text: str, table: str, values: Mapping[str, object]) -> str:
    """Sets `values` inside `[table]`, replacing existing keys in place and adding the rest."""
    lines = text.splitlines()
    span = _span(lines, table)
    if span is None:
        return set_table(text, table, values)
    start, end = span
    pending = dict(values)
    for i in range(start + 1, end):
        found = re.match(r"^(\s*)([A-Za-z0-9_-]+)\s*=", lines[i])
        if found and found.group(2) in pending:
            key = found.group(2)
            comment = re.search(r"\s+#[^\"']*$", lines[i])
            lines[i] = f"{found.group(1)}{key} = {toml_value(pending.pop(key))}" + (
                comment.group(0) if comment else ""
            )
    insert = start + 1
    new = [f"{k} = {toml_value(v)}" for k, v in pending.items()]
    return _check("\n".join(lines[:insert] + new + lines[insert:]).rstrip("\n") + "\n")
