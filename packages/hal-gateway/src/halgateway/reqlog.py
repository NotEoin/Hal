"""The request metadata log: one JSONL record per request, rotated by size (REQ-GW-70, 74).

Decisions:

- Records hold metadata only: no message content, headers or tokens. Bodies are written
  only when `[gateway.log].content` is set, one 0600 file per request.
- A failed write never fails the request; it is logged at ERROR at most once a minute.
- Usage comes from the last `USAGE_TAIL_BYTES` of the reply, the only part a streamed
  relay keeps, and is best effort.
- This is an operational log, not the security audit log (that belongs to the broker).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

LOG_NAME = "requests.jsonl"
CONTENT_DIR = "content"
USAGE_TAIL_BYTES = 8 * 1024  # design: the relay keeps the last 8 KiB of the reply
ERROR_REPORT_INTERVAL_S = 60.0  # REQ-GW-74: one ERROR line per minute
MAX_CONTENT_BYTES = 8 * 1024 * 1024  # a stored reply is cut here; the log is for debugging
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PRIVATE_FILE_MODE = 0o600
USAGE_KEYS = {
    "prompt_tokens": "input_tokens",
    "input_tokens": "input_tokens",
    "prompt_eval_count": "input_tokens",
    "completion_tokens": "output_tokens",
    "output_tokens": "output_tokens",
    "eval_count": "output_tokens",
}
_USAGE_RE = re.compile(r'"(' + "|".join(USAGE_KEYS) + r')"\s*:\s*(\d+)')


def request_id(client_value: str | None) -> str:
    """Echoes a well-formed client `X-Request-Id`, else makes `r-<8 hex>`."""
    if client_value and REQUEST_ID_RE.match(client_value):
        return client_value
    return "r-" + os.urandom(4).hex()


def scan_usage(tail: bytes) -> dict[str, int] | None:
    """Returns the last token counts found in `tail`, normalised to input and output, or None."""
    found: dict[str, int] = {}
    for key, value in _USAGE_RE.findall(tail.decode("utf-8", errors="replace")):
        found[USAGE_KEYS[key]] = int(value)
    return found or None


def utc_now() -> str:
    """The current time as UTC ISO 8601 with a `Z`."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RequestLog:
    """Appends records to `<dir>/requests.jsonl` and rotates it."""

    def __init__(self, directory: Path, settings: Mapping[str, object]) -> None:
        self.directory = Path(directory)
        self.enabled = bool(settings["requests"])
        self.content = bool(settings["content"])
        self.max_bytes = int(settings["max_bytes"])  # type: ignore[call-overload]
        self.keep = int(settings["keep"])  # type: ignore[call-overload]
        self.path = self.directory / LOG_NAME
        self._lock = threading.Lock()
        self._last_error = float("-inf")

    def write(self, record: Mapping[str, object]) -> None:
        """Appends one record; a failure is reported once a minute and swallowed."""
        if not self.enabled:
            return
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        try:
            with self._lock:
                self._append(line)
        except OSError as exc:
            self._report(exc)

    def write_content(self, rid: str, request: bytes, reply: bytes) -> None:
        """Stores the bodies of one request when `content` is on."""
        if not (self.enabled and self.content):
            return
        doc = {
            "id": rid,
            "request": request.decode("utf-8", errors="replace"),
            "response": reply[:MAX_CONTENT_BYTES].decode("utf-8", errors="replace"),
        }
        try:
            folder = self.directory / CONTENT_DIR
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(folder / f"{rid}.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                json.dump(doc, fh, ensure_ascii=True)
        except OSError as exc:
            self._report(exc)

    def _append(self, line: str) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
            self._rotate()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "a", encoding="ascii") as fh:
            fh.write(line)

    def _rotate(self) -> None:
        if self.keep == 0:
            self.path.unlink(missing_ok=True)
            return
        oldest = self.path.with_name(f"{LOG_NAME}.{self.keep}")
        oldest.unlink(missing_ok=True)
        for n in range(self.keep - 1, 0, -1):
            src = self.path.with_name(f"{LOG_NAME}.{n}")
            if src.exists():
                src.rename(self.path.with_name(f"{LOG_NAME}.{n + 1}"))
        self.path.rename(self.path.with_name(f"{LOG_NAME}.1"))

    def _report(self, exc: OSError) -> None:
        now = time.monotonic()
        if now - self._last_error >= ERROR_REPORT_INTERVAL_S:
            self._last_error = now
            log.error("cannot write the request log %s: %s", self.path, exc)
