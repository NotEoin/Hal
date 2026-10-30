"""Host-owned transcripts: stores the `_hal/record` notifications of a boxed agent (03-agent 3.10.2).

The agent in the box writes no transcript file when the client asks for records, so a box that
dies leaves a complete history on the host. The box is untrusted: only known record types are
kept, a record is capped in size and the session id must be 12 hex characters, so a hostile
agent cannot pick a path or fill the disk through this channel.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from halkit import paths

log = logging.getLogger(__name__)

RECORD_TYPES = ("session", "message", "compaction", "notice", "turn_end", "checkpoint")
MAX_RECORD_BYTES = 2 * 2**20  # a tool result is capped well below this by the agent
MAX_TRANSCRIPT_BYTES = 256 * 2**20  # stops a runaway agent from filling the state disk
PRIVATE_FILE_MODE = 0o600


class RecordRefused(Exception):
    """A record the host will not store."""


class RecordStore:
    """Appends records to `<root>/<sid>/transcript.jsonl`, the layout `hal-agent` reads."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else paths.state_dir() / "sessions"

    def transcript(self, session_id: str) -> Path:
        """The transcript path of a valid session id."""
        if not paths.SESSION_ID_RE.fullmatch(session_id):
            raise RecordRefused(f"bad session id {session_id!r}")
        return self.root / session_id / "transcript.jsonl"

    def append(self, session_id: str, record: dict[str, object]) -> None:
        """Validates and appends one record; raises `RecordRefused` for anything unexpected."""
        if record.get("type") not in RECORD_TYPES:
            raise RecordRefused(f"unknown record type {record.get('type')!r}")
        line = json.dumps(record, ensure_ascii=True) + "\n"
        if len(line) > MAX_RECORD_BYTES:
            raise RecordRefused("record over the size cap")
        path = self.transcript(session_id)
        paths.ensure_private_dir(path.parent)
        if path.exists() and path.stat().st_size + len(line) > MAX_TRANSCRIPT_BYTES:
            raise RecordRefused("transcript over the size cap")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "a", encoding="ascii") as fh:
            fh.write(line)
            fh.flush()
            if record["type"] == "turn_end":
                os.fsync(fh.fileno())

    def load(self, session_id: str) -> list[dict[str, object]]:
        """All stored records of a session, oldest first."""
        path = self.transcript(session_id)
        if not path.is_file():
            return []
        return [json.loads(raw) for raw in path.read_text(encoding="ascii").splitlines() if raw.strip()]
