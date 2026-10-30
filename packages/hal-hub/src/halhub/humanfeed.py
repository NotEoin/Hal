"""Human-message feed: how the broker tells a hub child what the person last said (REQ-SEC-93).

The broker writes one JSON line per human message to a pipe it hands to the child as the file
descriptor named by `HAL_HUMAN_FD`. A pipe, not the MCP socket: the box can write anything to the
socket, but it cannot reach this descriptor, so a message the model forged cannot reset taint or
become the reviewer's "last human message". The text stays in memory (see `HubSession`).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session import HubSession

log = logging.getLogger(__name__)

ENV_FD = "HAL_HUMAN_FD"
MAX_LINE_BYTES = 64 * 1024  # a longer line is dropped; the session keeps only a prefix anyway


def encode(text: str, surface: str) -> bytes:
    """One feed line for a human message."""
    return (json.dumps({"text": text, "surface": surface}) + "\n").encode()


def feed_fd(environ: Mapping[str, str] | None = None) -> int | None:
    """The descriptor named by `HAL_HUMAN_FD`, or None when unset; raises `ValueError` if malformed."""
    raw = (os.environ if environ is None else environ).get(ENV_FD)
    if raw is None:
        return None
    if not raw.isdigit():
        raise ValueError(f"{ENV_FD} must be a descriptor number, got {raw!r}")
    return int(raw)


def apply_line(session: HubSession, line: bytes) -> bool:
    """Applies one feed line to `session`; False (and a log line) when it is malformed."""
    try:
        data = json.loads(line)
        text, surface = data["text"], data["surface"]
        if not isinstance(text, str) or not isinstance(surface, str):
            raise TypeError("text and surface must be strings")
        session.human_message(text, surface=surface)
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("dropped a malformed human-message line: %s", exc)
        return False
    return True


def serve(session: HubSession, fd: int) -> threading.Thread:
    """Reads feed lines from `fd` on a daemon thread until it closes."""

    def run() -> None:
        with os.fdopen(fd, "rb") as stream:
            while True:
                line = stream.readline(MAX_LINE_BYTES + 1)
                if not line:
                    return
                if not line.endswith(b"\n") and len(line) > MAX_LINE_BYTES:
                    log.warning("dropped an over-long human-message line")
                    while (rest := stream.readline(MAX_LINE_BYTES)) and not rest.endswith(b"\n"):
                        pass
                    continue
                apply_line(session, line)

    thread = threading.Thread(target=run, name="human-feed", daemon=True)
    thread.start()
    return thread
