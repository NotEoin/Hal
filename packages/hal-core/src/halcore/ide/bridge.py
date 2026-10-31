"""The TUI side of the IDE channel: turns validated editor messages into prompt text.

`IdeBridge` is the only consumer of `IdeServer` messages. It never executes or applies anything:

- `add_selection` reads the named lines from the real project (host side, `open_beneath`, size
  capped) and queues them as a text block for the next prompt, and shows an `@path#L10-20` chip.
- `context` remembers the editor's current file and selection; it joins a prompt as one short
  line only while ambient sharing is on (off by default: the editor's open file is private).
- `diagnostics` queues the editor's problems for the next prompt.
- `show_changes` answers with the changed files and both roots, so the editor can diff them.
- `accept` only prints a notice; the user runs `/accept`, which asks its own confirmation.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from halkit import fsafe

from .protocol import IdeRefused, Message, chip
from .server import IdeServer

log = logging.getLogger(__name__)

MAX_SELECTION_BYTES = 32 * 1024  # about 8k tokens: a quarter of a 32k context at most
MAX_DIAGNOSTIC_LINES = 40
Block = dict[str, object]


class IdeBridge:
    """Holds what the editor has sent since the last prompt."""

    def __init__(
        self,
        project: Path,
        server: IdeServer,
        say: Callable[[str], None],
        changed: Callable[[], list[str]],
        copy_root: Callable[[], Path | None],
    ) -> None:
        self.project, self.server, self.say = project, server, say
        self.changed, self.copy_root = changed, copy_root
        self.pending: list[Block] = []
        self.ambient_on = False
        self.current: tuple[str | None, tuple[int, int] | None] = (None, None)
        self.accept_requested = False

    async def handle(self, msg: Message) -> None:
        """Applies one validated message."""
        f = msg.fields
        if msg.type == "add_selection":
            self._add_selection(str(f["path"]), int(f["start"]), int(f["end"]))  # type: ignore[call-overload]
        elif msg.type == "context":
            self.current = (f["path"], f["selection"])  # type: ignore[assignment]
        elif msg.type == "diagnostics":
            self._add_diagnostics(str(f["path"]), f["items"])  # type: ignore[arg-type]
        elif msg.type == "show_changes":
            root = self.copy_root()
            await self.server.send(
                {"type": "changes", "project": str(self.project), "copy": str(root) if root else "", "files": self.changed()}
            )
        elif msg.type == "accept":
            self.accept_requested = True
            self.say("the editor asks to accept the changes: type /accept to review and confirm")

    def _add_selection(self, rel: str, start: int, end: int) -> None:
        try:
            fd = fsafe.open_beneath(self.project, rel)
            with os.fdopen(fd, "rb") as fh:
                data = fh.read(MAX_SELECTION_BYTES * 4)
        except (OSError, fsafe.UnsafePathError, IdeRefused) as exc:
            self.say(f"ide: cannot read {rel}: {exc}")
            return
        lines = data.decode("utf-8", errors="replace").splitlines()[start - 1 : end]
        text = "\n".join(lines)[:MAX_SELECTION_BYTES]
        label = chip(rel, start, end)
        self.pending.append({"type": "text", "text": f"Selected in the editor ({label}):\n```\n{text}\n```"})
        self.say(f"ide: added {label}")

    def _add_diagnostics(self, rel: str, items: list[dict[str, object]]) -> None:
        rows = [f"{rel}:{i['line']} {i['severity']}: {i['message']}" for i in items[:MAX_DIAGNOSTIC_LINES]]
        if rows:
            self.pending.append({"type": "text", "text": "Editor diagnostics:\n" + "\n".join(rows)})
        self.say(f"ide: {len(rows)} diagnostics for {rel}")

    def blocks(self) -> list[Block]:
        """Queued editor blocks for the next prompt (cleared), plus the ambient line when on."""
        out, self.pending = self.pending, []
        path, sel = self.current
        if self.ambient_on and path:
            out.append({"type": "text", "text": "Editor has open: " + (chip(path, *sel) if sel else f"@{path}")})
        return out

    def status(self) -> str:
        """One line for `/ide`."""
        return (
            f"ide: {'connected' if self.server.connected else 'waiting for an editor'} "
            f"({self.server.sock_path}); ambient context {'on' if self.ambient_on else 'off'}"
        )
