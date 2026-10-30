"""Session transcripts: ids, the JSONL writer, the store (list, load, resume) and export.

A transcript is one JSON object per line (`session` header, `message`, `compaction`, `notice`,
`turn_end`), ASCII-escaped, with UTC `Z` timestamps. Argument values whose names look like
secrets are masked on write, so a transcript or export never carries them. A last line that does
not parse is a crash mid-write and is skipped; any other bad line fails loudly with its number.
In the box the host writes the transcript; `SessionWriter` is for standalone runs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("halagent.sessions")

SCHEMA_VERSION = 1
ID_RE = re.compile(r"[0-9a-f]{12}")
SECRET_ARG = re.compile(r"pass(word)?|token|secret|api_?key|credential", re.I)
MASK = "***"
REASONING_CAP = 20_000
EXPORT_RESULT_CAP = 2_000
LIST_LIMIT = 50
RECORD_TYPES = ("session", "message", "compaction", "notice", "turn_end", "checkpoint")


class SessionError(Exception):
    """A transcript is unreadable, or a session id is not valid."""


def new_session_id() -> str:
    """A fresh 12-hex-character session id."""
    return secrets.token_hex(6)


def check_session_id(value: str) -> str:
    """Returns `value` if it is a valid session id; raises `SessionError` otherwise."""
    if not ID_RE.fullmatch(value):
        raise SessionError(f"invalid session id {value!r}: expected 12 lowercase hex characters")
    return value


def utc_now() -> str:
    """The current time as UTC ISO 8601 with a `Z`."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def mask_secrets(value: Any) -> Any:
    """Copies `value` with the values of secret-looking keys replaced by `***`."""
    if isinstance(value, dict):
        return {k: MASK if SECRET_ARG.search(str(k)) else mask_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value


def _mask_message(msg: dict) -> dict:
    """A copy of a chat message with secret tool arguments masked and reasoning capped."""
    out = json.loads(json.dumps(msg))
    for call in out.get("tool_calls") or []:
        fn = call.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            continue
        fn["arguments"] = json.dumps(mask_secrets(args))
    if isinstance(out.get("reasoning"), str):
        out["reasoning"] = out["reasoning"][:REASONING_CAP]
    return out


class SessionWriter:
    """Appends records to `path`, one line each; `turn_end` records are fsync'd."""

    def __init__(self, path: Path, header: dict[str, Any]) -> None:
        check_session_id(str(header.get("id", "")))
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("a", encoding="ascii")
        if path.stat().st_size == 0:
            self.write({"type": "session", "v": SCHEMA_VERSION, "created": utc_now(), **header})

    def write(self, record: dict[str, Any]) -> None:
        """Appends one record; `message` records get secret masking."""
        if record.get("type") not in RECORD_TYPES:
            raise SessionError(f"unknown record type {record.get('type')!r}")
        rec = dict(record)
        rec.setdefault("ts", utc_now())
        if rec["type"] == "message":
            rec["msg"] = _mask_message(rec["msg"])
        self._fh.write(json.dumps(rec, ensure_ascii=True) + "\n")
        self._fh.flush()
        if rec["type"] == "turn_end":
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        """Closes the file."""
        self._fh.close()


@dataclass(frozen=True)
class SessionInfo:
    """The summary of one stored session, taken from its `session` record."""

    id: str
    created: str
    cwd: str
    title: str
    model: str
    path: Path


def read_records(path: Path) -> list[dict[str, Any]]:
    """Reads a transcript; skips an unparsable last line, fails on any other bad line."""
    lines = path.read_text(encoding="ascii", errors="replace").splitlines()
    out: list[dict[str, Any]] = []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            if not isinstance(rec, dict) or rec.get("type") not in RECORD_TYPES:
                raise ValueError("not a record")
        except ValueError as exc:
            if n == len(lines):
                log.warning("%s: skipping unreadable last line %d (crash while writing?)", path, n)
                break
            raise SessionError(f"{path}:{n}: invalid record: {exc}") from exc
        out.append(rec)
    return out


def replay_indexed(records: Iterable[dict[str, Any]]) -> list[tuple[int, dict]]:
    """Rebuilds (index, chat message) pairs: applies mask and summary compactions in place.

    A summary message is written after the messages it replaces but sorts where they were.
    """
    msgs: dict[int, dict] = {}
    dropped: set[int] = set()
    order: dict[int, float] = {}
    for rec in records:
        if rec["type"] == "message":
            msgs[rec["i"]] = dict(rec["msg"])
            order.setdefault(rec["i"], float(rec["i"]))
        elif rec["type"] == "compaction":
            for idx, stub in (rec.get("masked") or {}).items():
                if int(idx) in msgs:
                    msgs[int(idx)]["content"] = stub
            if rec.get("replaces"):
                first, last = rec["replaces"]
                dropped.update(range(first, last + 1))
                if rec.get("summary_i") is not None:
                    order[rec["summary_i"]] = first - 0.5
    return sorted(((i, m) for i, m in msgs.items() if i not in dropped), key=lambda im: order[im[0]])


def replay(records: Iterable[dict[str, Any]]) -> list[dict]:
    """Rebuilds the chat messages (see `replay_indexed`)."""
    return [m for _, m in replay_indexed(records)]


class SessionStore:
    """Reads `<root>/<sid>/transcript.jsonl`; lists sessions by their `session` record."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, session_id: str) -> Path:
        """The transcript path of a valid session id."""
        return self.root / check_session_id(session_id) / "transcript.jsonl"

    def load(self, session_id: str) -> list[dict[str, Any]]:
        """All records of a session; raises `SessionError` if it does not exist."""
        path = self.path(session_id)
        if not path.is_file():
            raise SessionError(f"no session {session_id}")
        return read_records(path)

    def list(self, *, cwd: str | None = None, limit: int = LIST_LIMIT) -> list[SessionInfo]:
        """Sessions, newest first, optionally only those started in `cwd`."""
        found = []
        for path in self.root.glob("*/transcript.jsonl") if self.root.is_dir() else []:
            try:
                first = next(iter(read_records(path)), None)
            except SessionError as exc:
                log.warning("skipping %s: %s", path, exc)
                continue
            if not first or first["type"] != "session" or (cwd is not None and first.get("cwd") != cwd):
                continue
            found.append(
                SessionInfo(first["id"], first.get("created", ""), first.get("cwd", ""),
                            first.get("title", ""), first.get("model", ""), path)
            )  # fmt: skip
        found.sort(key=lambda s: (s.created, s.id), reverse=True)
        return found[:limit]

    def latest(self, *, cwd: str) -> SessionInfo | None:
        """The newest session started in `cwd`."""
        listed = self.list(cwd=cwd, limit=1)
        return listed[0] if listed else None

    def export(self, session_id: str, fmt: Literal["md", "jsonl"], *, reasoning: bool = False) -> str:
        """The session as Markdown or JSONL, secrets masked; reasoning only when asked."""
        records = self.load(session_id)
        if fmt == "jsonl":
            return "".join(json.dumps(_export_record(r, reasoning)) + "\n" for r in records)
        return _markdown(records, reasoning)


def _export_record(rec: dict, reasoning: bool) -> dict:
    rec = dict(rec)
    if isinstance(rec.get("msg"), dict):
        rec["msg"] = _mask_message(rec["msg"])
        if not reasoning:
            rec["msg"].pop("reasoning", None)
    return rec


def _markdown(records: list[dict], reasoning: bool) -> str:
    head = records[0] if records and records[0]["type"] == "session" else {}
    out = [f"# Session {head.get('id', '')}", "", f"Model: {head.get('model', '')}  ",
           f"Started: {head.get('created', '')}", ""]  # fmt: skip
    for rec in records:
        if rec["type"] != "message":
            continue
        msg = _export_record(rec, reasoning)["msg"]
        role = msg.get("role")
        if role == "user":
            out += ["## User", "", str(msg.get("content") or ""), ""]
        elif role == "assistant":
            if reasoning and msg.get("reasoning"):
                out += ["_Reasoning:_", "", str(msg["reasoning"]), ""]
            if msg.get("content"):
                out += ["## Assistant", "", str(msg["content"]), ""]
            for call in msg.get("tool_calls") or []:
                fn = call.get("function", {})
                out += [f"Tool call `{fn.get('name')}`:", "```json", str(fn.get("arguments")), "```", ""]
        elif role == "tool":
            text = str(msg.get("content") or "")
            if len(text) > EXPORT_RESULT_CAP:
                text = text[:EXPORT_RESULT_CAP] + "\n[... cut ...]"
            out += ["Result:", "```text", text, "```", ""]
    return "\n".join(out).rstrip() + "\n"
