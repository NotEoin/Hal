"""The `read`, `edit` and `write` tools.

`edit` tries four matching tiers so small models that mangle whitespace or paste line numbers
still land the edit; every tier needs exactly one match. A syntax guard refuses an edit that
breaks a file that parsed before. Writes are atomic (temp file, `os.replace`).
"""

from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

from halagent.tools.paths import ToolError, Workspace

READ_LINES = 200
READ_CHARS = 12_000
READ_LINE_CHARS = 500
WRITE_MAX_CHARS = 200_000
DIFF_LINES = 40
DIFF_CHARS = 3_000
CLOSEST_LINES = 12
AMBIGUOUS_SHOWN = 5
BINARY_PROBE = 8192
LINE_PREFIX = re.compile(r"^\s*\d+(?:\t|[:|] ?)")
ESCAPES = (("\\n", "\n"), ("\\t", "\t"), ('\\"', '"'), ("\\\\", "\\"))


def _text(path: Path, shown: str) -> str:
    if not path.is_file():
        raise ToolError("not_found", f"{shown} is not a file")
    data = path.read_bytes()
    if b"\0" in data[:BINARY_PROBE]:
        raise ToolError("binary", f"{shown} is a binary file")
    return data.decode("utf-8", "replace")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o7777 if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".hal-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _int(args: dict, key: str, default: int) -> int:
    value = args.get(key)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError("invalid_args", f"{key} must be an integer") from exc


def read(ws: Workspace, args: dict) -> str:
    """Numbered lines of a file, at most 200 lines and 12000 characters."""
    shown = str(args.get("path"))
    lines = _text(ws.resolve(args.get("path")), shown).splitlines()
    start = max(_int(args, "start_line", 1), 1)
    end = min(_int(args, "end_line", start + READ_LINES - 1), start + READ_LINES - 1)
    if start > len(lines) and lines:
        raise ToolError("not_found", f"The file has {len(lines)} lines.")
    out, size = [], 0
    for n, line in enumerate(lines[start - 1 : end], start):
        if len(line) > READ_LINE_CHARS:
            line = line[:READ_LINE_CHARS] + "[... line cut ...]"
        row = f"{n:>6}\t{line}"
        if size + len(row) > READ_CHARS:
            break
        out.append(row)
        size += len(row) + 1
    last = start + len(out) - 1
    if not out:
        return "[empty file]"
    if last < len(lines):
        out.append(f"[hal] lines {start}-{last} of {len(lines)}. Continue with start_line={last + 1}.")
    return "\n".join(out)


def _syntax_error(path: Path, text: str) -> str | None:
    try:
        if path.suffix == ".py":
            compile(text, str(path), "exec")
        elif path.suffix == ".json":
            json.loads(text)
        elif path.suffix == ".toml":
            tomllib.loads(text)
    except (SyntaxError, ValueError) as exc:  # tomllib.TOMLDecodeError and JSONDecodeError are ValueErrors
        line = getattr(exc, "lineno", None)
        return f"line {line}: {exc}" if line else str(exc)
    return None


def _windows(lines: list[str], old: list[str], strip: bool) -> list[int]:
    n = len(old)
    norm = (lambda s: s.strip()) if strip else (lambda s: s)
    want = [norm(s) for s in old]
    return [i for i in range(len(lines) - n + 1) if [norm(s) for s in lines[i : i + n]] == want]


def _find(text: str, old: str, new: str) -> tuple[str, str]:
    """Returns (new file text, tier name); raises `ToolError` when no tier finds one match."""
    for tier, o, n in _candidates(old, new):
        if tier == "exact":
            count = text.count(o)
            if count == 1:
                return text.replace(o, n, 1), tier
            if count > 1:
                at = [text.count("\n", 0, m.start()) + 1 for m in re.finditer(re.escape(o), text)]
                raise _ambiguous(at)
            continue
        lines = text.split("\n")
        olines = o.split("\n")
        hits = _windows(lines, olines, strip=True)
        if len(hits) > 1:
            raise _ambiguous([h + 1 for h in hits])
        if hits:
            i = hits[0]
            shift = _indent(lines[i]) - _indent(olines[0])
            nlines = [_reindent(s, shift) for s in n.split("\n")]
            return "\n".join(lines[:i] + nlines + lines[i + len(olines) :]), tier
    raise _not_found(text, old)


def _candidates(old: str, new: str):
    yield "exact", old, new
    olines = old.split("\n")
    if all(LINE_PREFIX.match(s) for s in olines if s.strip()):
        o = "\n".join(LINE_PREFIX.sub("", s, 1) for s in olines)
        n = "\n".join(LINE_PREFIX.sub("", s, 1) for s in new.split("\n"))
        yield "exact", o, n
    yield "whitespace", old, new
    if "\n" not in old and "\\n" in old:
        o, n = old, new
        for esc, real in ESCAPES:
            o, n = o.replace(esc, real), n.replace(esc, real)
        yield "exact", o, n
        yield "whitespace", o, n


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _reindent(line: str, shift: int) -> str:
    if not line.strip():
        return line
    return " " * (_indent(line) + shift) + line.lstrip() if shift >= 0 else line[min(-shift, _indent(line)) :]


def _ambiguous(lines: list[int]) -> ToolError:
    shown = ", ".join(str(n) for n in lines[:AMBIGUOUS_SHOWN])
    return ToolError(
        "ambiguous",
        f"old_string matches {len(lines)} places (lines {shown}); add more surrounding lines to old_string",
    )


def _not_found(text: str, old: str) -> ToolError:
    lines = text.split("\n")
    size = max(len(old.split("\n")), 1)
    best, ratio = 0, -1.0
    for i in range(max(len(lines) - size + 1, 1)):
        r = difflib.SequenceMatcher(None, "\n".join(lines[i : i + size]), old).ratio()
        if r > ratio:
            best, ratio = i, r
    near = "\n".join(
        f"{best + k + 1:>6}\t{s}" for k, s in enumerate(lines[best : best + size][:CLOSEST_LINES])
    )
    return ToolError(
        "not_found",
        f"old_string was not found. Closest lines:\n{near}\nRead the lines again and copy them exactly.",
    )


def _diff(shown: str, old: str, new: str) -> str:
    diff = list(
        difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{shown}", f"b/{shown}", lineterm="", n=3)
    )
    out = "\n".join(diff[:DIFF_LINES])
    if len(diff) > DIFF_LINES or len(out) > DIFF_CHARS:
        out = out[:DIFF_CHARS] + "\n[... diff cut ...]"
    return out


def edit(ws: Workspace, args: dict, *, syntax_guard: bool = True) -> str:
    """Replaces one exact piece of text; returns a short diff."""
    old, new = args.get("old_string"), args.get("new_string")
    if not isinstance(old, str) or not old or not isinstance(new, str) or old == new:
        raise ToolError("invalid_args", "old_string must be non-empty text that differs from new_string")
    path = ws.resolve(args.get("path"), write=True)
    shown = ws.display(path)
    text = _text(path, shown)
    updated, tier = _find(text, old, new)
    if syntax_guard and _syntax_error(path, text) is None:
        bad = _syntax_error(path, updated)
        if bad:
            raise ToolError("syntax", f"the edit would break {shown} ({bad}); the file was not changed")
    _atomic_write(path, updated)
    suffix = "" if tier == "exact" else f" (tier: {tier})"
    return f"Edited {shown}{suffix}.\n{_diff(shown, text, updated)}"


def write(ws: Workspace, args: dict) -> str:
    """Creates or replaces a whole file."""
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("invalid_args", "content must be a string")
    path = ws.resolve(args.get("path"), write=True)
    shown = ws.display(path)
    if path.is_dir():
        raise ToolError("invalid_args", f"{shown} is a directory")
    if len(content) > WRITE_MAX_CHARS:
        raise ToolError(
            "too_large", "write the file in parts: write the first part, then add the rest with edit"
        )
    was = len(path.read_text("utf-8", "replace").splitlines()) if path.is_file() else None
    _atomic_write(path, content)
    n = len(content.splitlines())
    msg = f"Created {shown} ({n} lines)." if was is None else f"Replaced {shown} ({n} lines, was {was})."
    bad = _syntax_error(path, content) if path.suffix == ".py" else None
    return msg + (f"\n[hal] warning: {shown} has a syntax error ({bad})." if bad else "")
