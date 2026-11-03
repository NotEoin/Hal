"""The line UI of `hal code` and `hal chat`, and `run_tui`, which picks the UI for a session.

The line UI reads each message with a prompt_toolkit editor, prints the transcript to the
terminal's scrollback and answers questions on the same terminal; it is the fallback when the
full-screen UI cannot run (no terminal, `TERM=dumb`, `[ui].interface = "line"`). The turn loop
itself lives in `halcore.tui.controller`, so both UIs run the same logic. Ctrl-C or Esc during a
turn cancels the turn, not the program. Broker approvals appear in the escaped approval panel and
are answered on this terminal; `hal approve` in another terminal also works.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Awaitable, Callable, Iterator
from typing import TextIO

from halkit import config as kitconfig

from .. import pids, sessions
from ..acp.permissions import PermissionBridge
from .approval import make_asker
from .controller import MODE_ORDER, Controller, history_path, read_plan_choice
from .keys import KeyWatcher
from .prompt import Hooks, PromptInput, private_history
from .render import Renderer, TurnStats
from .slash import Result, TuiState, command_descriptions
from .status import segments, to_prompt_toolkit
from .theme import UI_TABLE, Look, load_look

log = logging.getLogger(__name__)

PROMPT = "\nyou> "
NOTIFY_MIN_S = 10.0  # a turn shorter than this never notifies
NOTIFY_LONG_S = 60.0  # a turn this long notifies even when the terminal cannot report focus
YES_ANSWERS = ("y", "yes")
LineReader = Callable[[str], Awaitable[str]]
__all__ = ["MODE_ORDER", "LineUi", "load_ui_look", "notify", "run_tui"]


def load_ui_look(out: TextIO) -> Look:
    """The `Look` from `[ui]` in the config file; a bad file is logged and the defaults are used."""
    try:
        return load_look(kitconfig.load(UI_TABLE), stream=out)
    except kitconfig.ConfigError as exc:
        log.warning("ignoring [ui] settings: %s", exc)
        return load_look(stream=out)


def notify(out: TextIO, mode: str) -> None:
    """Rings the terminal bell or sends an OSC 9 desktop notification (fixed text, nothing from the model)."""
    if mode == "off" or not out.isatty():
        return
    out.write("\x1b]9;Hal: turn finished\x07" if mode == "osc9" else "\a")
    out.flush()


class LineUi:
    """The `Ui` of the line interface: a prompt_toolkit editor (or a scripted reader) and the scrollback."""

    def __init__(self, controller: Controller, out: TextIO, *, stdin_line: LineReader | None = None) -> None:
        assert controller.state is not None
        self.controller = controller
        self.state: TuiState = controller.state
        self.renderer: Renderer = controller.renderer
        self.out = out
        self.look = self.renderer.look
        self.stdin_line = stdin_line
        self.loop = asyncio.get_running_loop()
        self.interactive = stdin_line is None and sys.stdin.isatty()
        self.watcher = KeyWatcher(self.on_key)
        self.editor: PromptInput | None = None
        if self.interactive:
            self.editor = PromptInput(
                self.look,
                command_descriptions(self.state.user_commands, self.state.surface),
                lambda: to_prompt_toolkit(segments(self.state.status, self.look), self.look),
                root=lambda: controller.workdir if self.state.surface == "code" else None,
                history=private_history(history_path()),
                hooks=Hooks(self.cycle_mode, self.renderer.toggle_verbose, self.renderer.toggle_todo),
                escape_rewind=self.state.surface == "code",
            )

    # -- reading ---------------------------------------------------------------------------------

    async def read_line(self, prompt: str = "") -> str:
        """One line: from the script in tests, the editor on a terminal, else plain `input`."""
        if self.stdin_line is not None:
            return await self.stdin_line(prompt)
        if self.editor is not None:
            return await self.editor.read(prompt)
        if prompt:
            self.renderer.info(prompt)
        return await self.loop.run_in_executor(None, input, "")

    async def read_message(self) -> str:
        """The next message."""
        return await self.read_line(PROMPT)

    async def _read_raw(self, label: str) -> str:
        if self.stdin_line is not None:
            return await self.stdin_line("")
        return await self.loop.run_in_executor(None, input, label)

    async def confirm(self, question: str) -> bool:
        """A typed yes; anything else is no."""
        if self.stdin_line is not None:
            answer = await self.stdin_line("")
        else:
            with self.suspend():
                answer = await self.loop.run_in_executor(None, input, question)
        return answer.strip().lower() in YES_ANSWERS

    async def choose_plan(self, state: TuiState) -> str | None:
        """The typed plan answer."""
        return await read_plan_choice(self.renderer, self.read_line)

    def approval_sender(self, bridge: PermissionBridge, on_note: Callable[[str], None]):  # noqa: ANN201 - a `Sender`
        """The escaped approval panel answered on this terminal."""
        return make_asker(
            bridge, lambda: self._read_raw("approve [y once / a session / n deny / e note]> "), self.out,
            look=self.look, console=self.renderer.console, suspend=self.suspend,
            read_note=lambda: self._read_raw("note for the agent> "), on_note=on_note,
        )  # fmt: skip

    # -- drawing ---------------------------------------------------------------------------------

    def show(self, result: Result) -> None:
        """Prints a slash command's table and message."""
        if result.renderable is not None:
            self.renderer.write(result.renderable)
        if result.message:
            self.renderer.plain(result.message)

    def status_changed(self) -> None:
        """Nothing to do: the editor's toolbar reads the status on every redraw."""

    def turn_started(self) -> None:
        """Starts the Esc / Ctrl-O / Ctrl-T watcher for the turn."""
        self.watcher.start()

    def turn_ended(self, stats: TurnStats) -> None:
        """Stops the watcher; rings the bell after a long turn the user was not watching."""
        self.watcher.stop()
        background = self.watcher.focus_known and not self.watcher.focused
        if stats.elapsed >= NOTIFY_LONG_S or (stats.elapsed >= NOTIFY_MIN_S and background):
            notify(self.out, self.look.notify)

    # -- keys ------------------------------------------------------------------------------------

    def on_key(self, key: str) -> None:
        """A key pressed during a turn."""
        if key == "escape":
            asyncio.ensure_future(self.controller.cancel())
        elif key == "verbose":
            self.renderer.toggle_verbose()
        elif key == "todo":
            self.renderer.toggle_todo()

    def cycle_mode(self) -> None:
        """Shift-Tab: the next permission mode."""
        self.state.status.notice = f"mode: {self.controller.cycle_mode()}"

    @contextlib.contextmanager
    def suspend(self) -> Iterator[None]:
        """Pauses the live region and the key watcher while a question owns the terminal."""
        with self.renderer.suspended(), self.watcher.suspended():
            yield

    def close(self) -> None:
        """Restores the terminal."""
        self.watcher.stop()


async def run_tui(box: sessions.BoxedAgent, **kwargs) -> int:  # noqa: ANN003 - same keywords as `_run_tui`
    """Runs the interactive loop with a pid file registered, so `hal kill` can find this process."""
    with pids.registered("tui"):
        return await _run_tui(box, **kwargs)


async def _run_tui(
    box: sessions.BoxedAgent,
    *,
    cwd: str,
    project=None,  # noqa: ANN001 - a `Path` or None
    model: str = "",
    resume: str = "",
    store=None,  # noqa: ANN001 - a `RecordStore` or None
    out: TextIO | None = None,
    stdin_line: LineReader | None = None,
    look: Look | None = None,
    mode: str = "code",
) -> int:
    """Runs the line UI on an already started box; returns the exit status.

    `mode` is `code` or `chat`: it is recorded in the session meta and limits `/resume` to
    sessions of the same kind. `stdin_line` replaces the terminal with a script (tests).
    """
    out = out or sys.stdout
    look = look or load_ui_look(out)
    renderer = Renderer(out, look)
    controller = Controller(
        box, renderer, cwd=cwd, project=project, model=model, resume=resume, store=store, mode=mode,
        live=stdin_line is None,
    )  # fmt: skip
    error = await controller.start()
    if error:
        renderer.plain(error)
        await controller.client.close()
        return 1
    ui = LineUi(controller, out, stdin_line=stdin_line)
    if ui.interactive:
        await controller.open_ide()
    try:
        return await controller.run(ui)
    finally:
        ui.close()
