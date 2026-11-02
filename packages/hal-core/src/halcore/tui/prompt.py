"""The input editor: a prompt_toolkit session with the key bindings of a modern coding CLI.

Enter sends; Alt-Enter or Ctrl-J inserts a newline; a bracketed paste of several lines becomes a
`[pasted N lines #K]` chip that expands to the real text on send; Shift-Tab cycles the permission
mode; Esc Esc asks for `/rewind`; Ctrl-O and Ctrl-T toggle verbose tool output and the todo list.
Tab completes `/commands` (with descriptions) and `@path` files of the workspace. History lives in
a private file so Ctrl-R finds earlier prompts across sessions.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import Completer, CompleteEvent, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import ColorDepth, Output
from prompt_toolkit.styles import Style

from halkit import effort

from . import mentions
from .status import PT_COLORS
from .theme import Look

log = logging.getLogger(__name__)

PASTE_COLLAPSE_LINES = 3  # fewer lines are inserted as they are
HISTORY_MODE = 0o600  # prompts may hold secrets the user pasted
TOKEN_RE = re.compile(r"(?:^|\s)(@[^\s]*)$")


@dataclass
class Hooks:
    """Callbacks the key bindings call; any may be None."""

    cycle_mode: Callable[[], None] | None = None
    toggle_verbose: Callable[[], None] | None = None
    toggle_todo: Callable[[], None] | None = None


COMMAND_ARGUMENTS = {  # fixed words Tab completes after a command name
    "effort": (*effort.LEVELS, "default"),
    "model": ("save",),
    "mode": ("default", "auto-edit", "plan"),
    "theme": ("dark", "light", "mono"),
}


class HalCompleter(Completer):
    """Completes `/command` names (with their descriptions) and `@path` files of the workspace."""

    def __init__(self, commands: Mapping[str, str], root: Callable[[], Path | None]) -> None:
        self.commands = commands
        self.root = root

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        """Yields completions for the text before the cursor."""
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            for name in sorted(self.commands):
                if name.startswith(text[1:]):
                    yield Completion("/" + name, start_position=-len(text), display_meta=self.commands[name])
            return
        if text.startswith("/") and text.count(" ") == 1:
            name, _, partial = text[1:].partition(" ")
            for word in COMMAND_ARGUMENTS.get(name, ()):
                if word.startswith(partial):
                    yield Completion(word, start_position=-len(partial))
            return
        match = TOKEN_RE.search(text)
        root = self.root()
        if match and root is not None:
            token = match.group(1)
            for rel in mentions.match_files(root, token[1:]):
                yield Completion("@" + rel, start_position=-len(token), display=rel)


def private_history(path: Path) -> History:
    """A file history at `path` created with mode 0600, or an in-memory one when the file cannot be made."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, HISTORY_MODE))
        return FileHistory(str(path))
    except OSError as exc:
        log.warning("prompt history disabled (%s): %s", path, exc)
        return InMemoryHistory()


class PromptInput:
    """One prompt_toolkit session; `read` returns the next message with paste chips expanded."""

    def __init__(
        self,
        look: Look,
        commands: Mapping[str, str],
        status: Callable[[], list[tuple[str, str]]],
        *,
        root: Callable[[], Path | None] = lambda: None,
        history: History | None = None,
        hooks: Hooks | None = None,
        input: Input | None = None,  # noqa: A002 - prompt_toolkit's own keyword; tests pass a pipe
        output: Output | None = None,
    ) -> None:
        self.look = look
        self.pastes: dict[str, str] = {}
        self.hooks = hooks or Hooks()
        theme = "mono" if not look.color else ("light" if look.palette.muted == "grey42" else "dark")
        colors = PT_COLORS[theme]
        self.style = Style.from_dict(
            {
                "bottom-toolbar": "noreverse",
                "bottom-toolbar.text": "noreverse",
                "completion-menu": "noreverse " + colors["muted"],
                "completion-menu.completion.current": "noreverse bold " + colors["accent"],
                "completion-menu.meta.completion": "noreverse " + colors["muted"],
                "completion-menu.meta.completion.current": "noreverse " + colors["muted"],
                "prompt": "bold " + colors["accent"],
            }
        )
        self.session: PromptSession[str] = PromptSession(
            history=history or InMemoryHistory(),
            completer=HalCompleter(commands, root),
            complete_while_typing=True,
            key_bindings=self._bindings(),
            bottom_toolbar=lambda: FormattedText(status()),
            style=self.style,
            color_depth=ColorDepth.DEPTH_24_BIT if look.color else ColorDepth.DEPTH_1_BIT,
            enable_history_search=False,
            input=input,
            output=output,
        )

    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")
        def _send(event) -> None:  # noqa: ANN001 - prompt_toolkit's KeyPressEvent
            event.current_buffer.validate_and_handle()

        @kb.add("escape", "enter")
        @kb.add("c-j")
        def _newline(event) -> None:  # noqa: ANN001
            event.current_buffer.insert_text("\n")

        @kb.add("s-tab")
        def _mode(event) -> None:  # noqa: ANN001
            self._call(self.hooks.cycle_mode)

        @kb.add("escape", "escape")
        def _rewind(event) -> None:  # noqa: ANN001
            event.app.exit(result="/rewind")

        @kb.add("c-o")
        def _verbose(event) -> None:  # noqa: ANN001
            self._call(self.hooks.toggle_verbose)

        @kb.add("c-t")
        def _todo(event) -> None:  # noqa: ANN001
            self._call(self.hooks.toggle_todo)

        @kb.add("c-c")
        def _interrupt(event) -> None:  # noqa: ANN001
            if event.current_buffer.text:
                event.current_buffer.reset()
            else:
                event.app.exit(exception=KeyboardInterrupt)

        @kb.add(Keys.BracketedPaste)
        def _paste(event) -> None:  # noqa: ANN001
            data = event.data.replace("\r\n", "\n").replace("\r", "\n")
            lines = data.count("\n") + 1
            if lines < PASTE_COLLAPSE_LINES:
                event.current_buffer.insert_text(data)
                return
            chip = f"[pasted {lines} lines #{len(self.pastes) + 1}]"
            self.pastes[chip] = data
            event.current_buffer.insert_text(chip)

        return kb

    @staticmethod
    def _call(hook: Callable[[], None] | None) -> None:
        if hook is not None:
            run_in_terminal(hook)

    def expand(self, text: str) -> str:
        """`text` with every paste chip replaced by the pasted text; clears the chips."""
        for chip, data in self.pastes.items():
            text = text.replace(chip, data)
        self.pastes.clear()
        return text

    async def read(self, prompt: str = "") -> str:
        """Reads one message; raises `EOFError` on Ctrl-D and `KeyboardInterrupt` on Ctrl-C at an empty line."""
        glyph = self.look.glyphs.prompt
        message = FormattedText([("class:prompt", f"{glyph} ")])
        text = await self.session.prompt_async(
            message, prompt_continuation=lambda width, line, wrap: FormattedText([("", "  ")]), multiline=False
        )
        return self.expand(text)
