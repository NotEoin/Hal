"""The IDE message schema: the only things an editor may say to `hal code`, and how paths are confined.

The editor is a client of the TUI, not a controller of it (RESOLUTIONS I1, I3). Every message is
one JSON object per line; its `type` must be in `IDE_TO_HOST`, its fields are checked here, and
a path is accepted only when it stays inside the workspace. Nothing in this module runs a command
or touches the file system beyond resolving paths. `ide/vscode/src` may only send types listed in
`IDE_TO_HOST`; a test enforces that.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

MAX_MESSAGE_BYTES = 2**20  # 1 MiB per message, the cap the design sets for IDE frames
MAX_DIAGNOSTICS = 200  # more than a local model can use in one prompt
MAX_TEXT = 500
IDE_TO_HOST = ("hello", "add_selection", "context", "diagnostics", "show_changes", "accept")
HOST_TO_IDE = ("welcome", "request_diagnostics", "changes", "error")
SEVERITIES = ("error", "warning", "info", "hint")


class IdeRefused(ValueError):
    """A message or path the host will not accept."""


@dataclass(frozen=True)
class Message:
    """A validated editor message; `fields` holds only checked values and relative paths."""

    type: str
    fields: dict[str, object]


def confine(workspace: Path, raw: object) -> str:
    """Returns `raw` as a workspace-relative POSIX path; raises `IdeRefused` if it escapes.

    Symlinks are resolved, so a link that points outside the workspace is refused as well.
    """
    if not isinstance(raw, str) or not raw or "\0" in raw or len(raw) > 4096:
        raise IdeRefused("path must be a non-empty string")
    root = Path(os.path.realpath(workspace))
    full = Path(os.path.realpath(raw if os.path.isabs(raw) else root / raw))
    try:
        rel = full.relative_to(root)
    except ValueError:
        raise IdeRefused(f"path {raw!r} is outside the workspace") from None
    return rel.as_posix()


def _line(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 10**7:
        raise IdeRefused(f"{name} must be a line number from 1")
    return value


def _range(raw: dict[str, object]) -> tuple[int, int]:
    start, end = _line(raw.get("start"), "start"), _line(raw.get("end"), "end")
    if end < start:
        raise IdeRefused("end is before start")
    return start, end


def parse(line: bytes, workspace: Path) -> Message:
    """Validates one received line; raises `IdeRefused` with a short reason."""
    if len(line) > MAX_MESSAGE_BYTES:
        raise IdeRefused("message over the size cap")
    try:
        raw = json.loads(line)
    except ValueError:
        raise IdeRefused("not JSON") from None
    if not isinstance(raw, dict):
        raise IdeRefused("message must be an object")
    kind = raw.get("type")
    if kind not in IDE_TO_HOST:
        raise IdeRefused(f"unknown message type {kind!r}")
    fields: dict[str, object] = {}
    if kind == "hello":
        client = raw.get("client", "")
        if not isinstance(client, str) or len(client) > 64:
            raise IdeRefused("client must be a short string")
        fields["client"] = client
    elif kind == "add_selection":
        fields["path"] = confine(workspace, raw.get("path"))
        fields["start"], fields["end"] = _range(raw)
    elif kind == "context":
        path = raw.get("path")
        fields["path"] = None if path is None else confine(workspace, path)
        sel = raw.get("selection")
        fields["selection"] = None
        if sel is not None:
            if not isinstance(sel, dict):
                raise IdeRefused("selection must be an object")
            fields["selection"] = _range(sel)
    elif kind == "diagnostics":
        fields["path"] = confine(workspace, raw.get("path"))
        items = raw.get("items")
        if not isinstance(items, list):
            raise IdeRefused("items must be a list")
        fields["items"] = [_diagnostic(item) for item in items[:MAX_DIAGNOSTICS]]
    return Message(kind, fields)


def _diagnostic(item: object) -> dict[str, object]:
    if not isinstance(item, dict):
        raise IdeRefused("a diagnostic must be an object")
    sev = item.get("severity")
    msg = item.get("message")
    if sev not in SEVERITIES or not isinstance(msg, str):
        raise IdeRefused("a diagnostic needs a severity and a message")
    return {"line": _line(item.get("line"), "line"), "severity": sev, "message": msg[:MAX_TEXT]}


def chip(path: str, start: int, end: int) -> str:
    """The `@path#L10-20` mention for a selection (a single line is `@path#L10`)."""
    return f"@{path}#L{start}" if start == end else f"@{path}#L{start}-{end}"


def encode(message: dict[str, object]) -> bytes:
    """One host-to-editor line; raises `IdeRefused` for a type outside `HOST_TO_IDE`."""
    if message.get("type") not in HOST_TO_IDE:
        raise IdeRefused(f"host may not send {message.get('type')!r}")
    return json.dumps(message, ensure_ascii=True).encode("ascii") + b"\n"
