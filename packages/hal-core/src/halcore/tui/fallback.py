"""Whether the full-screen UI can run on this terminal, and how a failed start falls back.

The full-screen UIs (Textual) need a real terminal on both stdin and stdout, a `TERM` that is
set and not `dumb`, and the `textual` package. `[ui].interface = "line"` (or `HAL_TUI=line`)
asks for the line UI, `full` asks for the full screen wherever it can run. When the app fails
before it has drawn anything, the caller prints one notice and runs the line UI on the same
session (`StartFailed`); a failure after the first draw is a normal crash, never a silent
restart. Nothing here imports Textual at module load, so headless paths stay light.
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Mapping
from typing import TextIO

log = logging.getLogger(__name__)

NOTICE = "full-screen UI unavailable: {reason}; using the line UI"


class StartFailed(Exception):
    """The full-screen app stopped before its first draw; the line UI can take over."""


def can_run_textual(
    stdin: TextIO, stdout: TextIO, environ: Mapping[str, str], interface: str = "auto"
) -> tuple[bool, str]:
    """`(True, "")` when the full-screen UI can start here, else `(False, reason)`."""
    if interface == "line":
        return False, "[ui].interface is line"
    if not (_isatty(stdin) and _isatty(stdout)):
        return False, "stdin or stdout is not a terminal"
    term = environ.get("TERM", "")
    if not term or term == "dumb":
        return False, f"TERM is {term or 'unset'}"
    if importlib.util.find_spec("textual") is None:
        return False, "the textual package is not installed"
    return True, ""


def _isatty(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):  # a closed or fake stream
        return False


def notice(reason: str) -> str:
    """The one line printed before the line UI takes over."""
    return NOTICE.format(reason=reason)
