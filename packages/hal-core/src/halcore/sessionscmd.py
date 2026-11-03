"""Host-side session index: `meta.json` per session, `hal sessions`, resume and fork.

The agent in the box writes transcripts through the host (`acp.records`); the host adds one
`meta.json` next to each transcript with the project, model, mode and a title, because the
transcript only knows the in-box path `/work`. Design decisions:

- Resume replays the host's own records through ACP `session/load`; nothing from the box's
  disk is trusted, and the project root must still exist (REQ-CLI-73).
- A fork copies the records under a new id, so both sessions go on independently.
- Export redacts with `halkit.redact` because a transcript may hold tool output with secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

from halkit import paths, redact

from .acp.records import PRIVATE_FILE_MODE, RecordStore

META_FILE = "meta.json"
TITLE_CHARS = 60
DEFAULT_LIMIT = 20
EXIT_USAGE = 2


class SessionsError(Exception):
    """A session cannot be listed, resumed, forked or exported."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    """A fresh 12-hex session id."""
    return secrets.token_hex(6)


def _meta_path(store: RecordStore, sid: str) -> Path:
    return store.transcript(sid).parent / META_FILE


def read_meta(store: RecordStore, sid: str) -> dict[str, str] | None:
    """The stored meta of `sid`, or None when there is none or it is unreadable."""
    try:
        data = json.loads(_meta_path(store, sid).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_meta(store: RecordStore, sid: str, **fields: str) -> None:
    """Merges `fields` into the meta of `sid` and stamps `updated`; empty values keep the old one."""
    path = _meta_path(store, sid)
    paths.ensure_private_dir(path.parent)
    meta = {**(read_meta(store, sid) or {"id": sid}), **{k: v for k, v in fields.items() if v}}
    meta["updated"] = _now()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        json.dump(meta, fh, ensure_ascii=True)
    os.replace(tmp, path)


def title_of(text: str) -> str:
    """A one-line title from the first prompt."""
    return " ".join(text.split())[:TITLE_CHARS]


def list_sessions(
    store: RecordStore, project: Path | None = None, limit: int = DEFAULT_LIMIT, *, mode: str = ""
) -> list[dict[str, str]]:
    """Sessions with a meta, newest first; with `project` only those of that project, with `mode` only that kind."""
    found = []
    if store.root.is_dir():
        for folder in store.root.iterdir():
            if not paths.SESSION_ID_RE.fullmatch(folder.name):
                continue
            meta = read_meta(store, folder.name)
            if meta and (project is None or meta.get("project") == str(project)) and (not mode or meta.get("mode") == mode):
                found.append(meta)
    return sorted(found, key=lambda m: m.get("updated", ""), reverse=True)[:limit]


def load_for_resume(store: RecordStore, sid: str, project: Path | None = None) -> tuple[dict[str, str], list]:
    """The meta and records of `sid`; raises `SessionsError` for a missing, foreign or orphaned session."""
    if not paths.SESSION_ID_RE.fullmatch(sid):
        raise SessionsError(f"{sid!r} is not a session id (12 hex characters)")
    meta, records = read_meta(store, sid), store.load(sid)
    if not records:
        raise SessionsError(f"no stored session {sid}")
    stored = (meta or {}).get("project", "")
    if stored and not Path(stored).is_dir():
        raise SessionsError(f"the project of session {sid} no longer exists: {stored}")
    if project is not None and stored and Path(stored) != project:
        raise SessionsError(f"session {sid} belongs to {stored}, not {project}")
    return meta or {"id": sid}, records


def fork(store: RecordStore, sid: str) -> str:
    """Copies the records and meta of `sid` to a new session and returns its id."""
    meta, records = load_for_resume(store, sid)
    new = new_id()
    for rec in records:
        store.append(new, rec)
    write_meta(store, new, **{k: v for k, v in meta.items() if k in ("project", "model", "mode", "title")}, fork_of=sid)
    return new


def _text(content: object) -> str:
    if isinstance(content, list):
        return " ".join(str(p.get("text", "[image]")) for p in content if isinstance(p, dict))
    return str(content or "")


def export(store: RecordStore, sid: str, fmt: str = "md", *, tool_output: bool = False) -> str:
    """The transcript of `sid` as redacted `md` or `jsonl`; tool output only with `tool_output`."""
    if fmt not in ("md", "jsonl"):
        raise SessionsError("format must be md or jsonl")
    records = store.load(sid)
    if not records:
        raise SessionsError(f"no stored session {sid}")
    if fmt == "jsonl":
        return "\n".join(json.dumps(redact.redact(r), ensure_ascii=True) for r in records) + "\n"
    lines = [f"# Session {sid}", ""]
    for rec in records:
        msg = rec.get("msg") if rec.get("type") == "message" else None
        if not isinstance(msg, dict) or msg.get("role") == "system":
            continue
        role = str(msg.get("role"))
        if role == "tool" and not tool_output:
            continue
        lines += [f"**{role}**", "", redact.scrub_text(_text(msg.get("content"))), ""]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """Runs `hal sessions list|export|resume`; returns the exit status."""
    p = argparse.ArgumentParser(prog="hal sessions")
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="show stored sessions, newest first")
    ls.add_argument("--project", type=Path)
    ls.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ls.add_argument("--json", action="store_true")
    ex = sub.add_parser("export", help="write a redacted transcript")
    ex.add_argument("sid")
    ex.add_argument("--format", choices=("md", "jsonl"), default="md")
    ex.add_argument("-o", "--output", type=Path)
    ex.add_argument("--with-tool-output", action="store_true")
    rs = sub.add_parser("resume", help="reopen a session in its project")
    rs.add_argument("sid")
    opts = p.parse_args(argv)
    store = RecordStore()
    try:
        if opts.cmd == "list":
            rows = list_sessions(store, opts.project.resolve() if opts.project else None, opts.limit)
            for m in rows:
                print(json.dumps(m) if opts.json else f"{m['id']}  {m.get('updated', '')}  {m.get('project', '')}  {m.get('title', '')}")
            return 0
        if opts.cmd == "export":
            text = export(store, opts.sid, opts.format, tool_output=opts.with_tool_output)
            if opts.output:
                opts.output.write_text(text, encoding="ascii")
            else:
                sys.stdout.write(text)
            return 0
        meta, _ = load_for_resume(store, opts.sid)
        from . import code  # noqa: PLC0415 - code imports the TUI, which imports this module

        return code.main([meta.get("project", "."), "--resume", opts.sid])
    except SessionsError as exc:
        print(f"hal sessions: {exc}", file=sys.stderr)
        return EXIT_USAGE
