"""Keys while a turn runs: Esc interrupts, Ctrl-O and Ctrl-T toggle, terminal focus is tracked.

prompt_toolkit owns the keyboard only while a prompt is open. During a turn this watcher puts the
terminal in cbreak mode (Ctrl-C still raises SIGINT), reads single keys from the event loop and
asks the terminal for focus reports (DEC mode 1004) so a finished turn can notify only when the
window is in the background. It does nothing when stdin is not a terminal. Keys typed during a
turn are discarded when it stops, so they cannot answer a later prompt by accident.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import termios
import tty
from collections.abc import Callable, Iterator

log = logging.getLogger(__name__)

ESC = b"\x1b"
CTRL_O = b"\x0f"
CTRL_T = b"\x14"
FOCUS_IN = b"\x1b[I"
FOCUS_OUT = b"\x1b[O"
FOCUS_ON = "\x1b[?1004h"
FOCUS_OFF = "\x1b[?1004l"
READ_BYTES = 64


def parse_keys(data: bytes) -> list[str]:
    """Names of the keys in one read: `escape`, `verbose`, `todo`, `focus-in`, `focus-out`; others are ignored."""
    if data == ESC:
        return ["escape"]
    found: list[str] = []
    rest = data
    while rest:
        if rest.startswith(FOCUS_IN):
            found.append("focus-in")
            rest = rest[len(FOCUS_IN) :]
        elif rest.startswith(FOCUS_OUT):
            found.append("focus-out")
            rest = rest[len(FOCUS_OUT) :]
        elif rest.startswith(ESC):
            break  # some other escape sequence (arrow keys): not ours
        else:
            if rest[:1] == CTRL_O:
                found.append("verbose")
            elif rest[:1] == CTRL_T:
                found.append("todo")
            rest = rest[1:]
    return found


class KeyWatcher:
    """Reads turn-time keys from stdin and calls the handler for each one."""

    def __init__(self, handler: Callable[[str], None]) -> None:
        self.handler = handler
        self.focused = True
        self.focus_known = False
        self._saved: list[object] | None = None
        self._fd = -1
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        """Starts watching; a no-op when stdin is not a terminal or already started."""
        if self._saved is not None or not sys.stdin.isatty():
            return
        self._fd = sys.stdin.fileno()
        try:
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self._loop = asyncio.get_running_loop()
            self._loop.add_reader(self._fd, self._read)
        except (termios.error, OSError, RuntimeError) as exc:
            log.warning("turn keys unavailable: %s", exc)
            self._saved = None
            return
        sys.stdout.write(FOCUS_ON)
        sys.stdout.flush()

    def stop(self) -> None:
        """Stops watching, restores the terminal and drops keys typed during the turn."""
        if self._saved is None or self._loop is None:
            return
        self._loop.remove_reader(self._fd)
        with contextlib.suppress(termios.error, OSError):
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            termios.tcflush(self._fd, termios.TCIFLUSH)
        sys.stdout.write(FOCUS_OFF)
        sys.stdout.flush()
        self._saved = None

    @contextlib.contextmanager
    def suspended(self) -> Iterator[None]:
        """Pauses the watcher while another reader (an approval prompt) owns the keyboard."""
        was_on = self._saved is not None
        self.stop()
        try:
            yield
        finally:
            if was_on:
                self.start()

    def _read(self) -> None:
        try:
            data = os.read(self._fd, READ_BYTES)
        except OSError:
            return
        for key in parse_keys(data):
            if key in ("focus-in", "focus-out"):
                self.focused, self.focus_known = key == "focus-in", True
            self.handler(key)
