"""The screen of the interactive `hal tune`: menus, rich output, prompt_toolkit input, the `hal code` look.

`halcore.tunestate.TuneSession` holds every decision; this module only draws it and reads the
user's keys. The look comes from `halcore.tui.theme` (glyphs, palette, `NO_COLOR`), so the screen
matches `hal code`: ASCII glyphs when the look says so, no colour on `mono`. The layout stacks
three lines per role instead of using a wide table, so it reads the same at 80 columns.

The screen is a set of menus: a live summary of every role, then a highlighted list. Up and Down
move, Enter chooses, 1 to 9 jump to a row, Esc or `q` go back, and a click on a row chooses it.
Pressing `:` on the main menu opens the old typed command line (`model code`, `context chat 48k`)
for power users.

Input goes through a `Prompter`. `PtPrompter` is the real one (a command line with completion and
a list that filters when it is long); tests pass a scripted prompter or feed `PtPrompter`
a prompt_toolkit pipe input, so nothing here needs a real terminal.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable, Sequence
from typing import Protocol, TextIO

from halhub.policy.load import load_policy
from halkit import config as halconfig
from halkit import hardware as hwmod
from halkit import modelconf, tuning
from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.text import Text

from . import modelplan, tunepresets, tunestate
from .tui.status import context_bar
from .tui.theme import PT_COLORS, Look, load_look, make_console

log = logging.getLogger(__name__)

PICK_ROWS = 9  # list rows shown at once; the picker scrolls so a 24-line terminal still fits
MIB_PER_GIB = 1024
# a cache that takes more than this share of the VRAM budget leaves little slack for long prompts;
# a judgement, not a measured limit
TIGHT_FRACTION = 0.85
TEMPERATURES = ("default", "0", "0.2", "0.5", "0.7", "1.0", "1.5")  # common values; Custom takes 0 to 2
CUSTOM = "custom"
COMMAND_KEY = ":"
MAIN_ITEMS = (
    ("Quick setup (presets)", "preset"),
    ("Adjust a role", "role"),
    ("Model sources (add or remove)", "source"),
    ("Review changes", "review"),
    ("Save and exit", "save"),
    ("Quit without saving", "quit"),
)
ROLE_ITEMS = (
    ("Model", "model"),
    ("Context", "context"),
    ("KV cache", "kv"),
    ("Effort", "effort"),
    ("Temperature", "temp"),
    ("Auto-tune this role", "auto"),
    ("Back", "back"),
)
YES_NO = (("Yes", "yes"), ("No", "no"))
SUMMARY_MODEL_WIDTH = 40  # role 7 + model 40 + mark 2 + context 6 + longest fit text 24 = 79 columns
COMMANDS = {
    "role": "choose which role the other commands change",
    "model": "choose a model from your sources",
    "context": "set the context length and see the fit",
    "kv": "set the KV cache type",
    "effort": "set the default thinking effort",
    "temp": "set the temperature",
    "auto": "tune for this hardware (auto all: every role)",
    "source": "add or remove a model source",
    "diff": "show the config changes",
    "write": "show the changes, then write them",
    "quit": "leave without writing",
    "menu": "go back to the menus",
    "help": "show this list",
}
ALIASES = {"m": "model", "c": "context", "k": "kv", "e": "effort", "t": "temp", "a": "auto", "s": "source",
           "d": "diff", "w": "write", "q": "quit", "r": "role", "?": "help", "exit": "quit", "b": "menu"}
DEFAULT_ROLE = "chat"
TOKENS = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*$")


def parse_tokens(text: str) -> int:
    """`32768`, `32k` or `1.5m` as a token count; raises `ValueError` for anything else."""
    found = TOKENS.match(text)
    if not found:
        raise ValueError(f"not a number of tokens: {text!r} (try 32768 or 32k)")
    factor = {"": 1, "k": 1024, "m": 1024 * 1024}[found.group(2).lower()]
    return int(float(found.group(1)) * factor)


def fuzzy(query: str, text: str) -> bool:
    """True when every character of `query` appears in `text` in order, ignoring case."""
    pos = 0
    low = text.lower()
    for ch in query.lower():
        pos = low.find(ch, pos)
        if pos < 0:
            return False
        pos += 1
    return True


def gib(mib: int) -> str:
    """MiB as GiB with one decimal."""
    return f"{mib / MIB_PER_GIB:.1f}"


# --- drawing ---------------------------------------------------------------------------------


def hardware_lines(hw: hwmod.Hardware) -> list[str]:
    """One line per fact about the machine: each GPU, memory, CPU, WSL2 and detection notes."""
    lines = [f"GPU {g.index}  {g.name}  {g.free_mib} / {g.total_mib} MiB free" for g in hw.gpus]
    lines = lines or ["GPU  none: models run on the CPU"]
    lines.append(f"RAM  {hw.ram_free_mib} / {hw.ram_total_mib} MiB free   CPU  {hw.cpu_threads} threads")
    warning = tuning.busy_warning(hw)
    if warning:
        lines.append(f"note: {warning}")
    if hw.wsl2:
        lines.append("WSL2: VRAM and RAM are what Windows lends this distro")
    return lines + [f"note: {n}" for n in hw.notes]


def fit_line(fit: tunestate.Fit, layers: int | None, look: Look) -> tuple[str, str]:
    """`(style role, text)` of the fit line of one role."""
    g = look.glyphs
    if fit.status == "empty":
        return "muted", "no model: press m to choose one, or a to tune"
    if fit.status in ("missing", "server"):
        return "bad" if fit.status == "missing" else "muted", fit.notes[0] if fit.notes else fit.status
    bar = context_bar(fit.used_mib, fit.budget_mib, g)
    if fit.status == "gpu":
        return "ok", f"{bar} {gib(fit.used_mib)} of {gib(fit.budget_mib)} GiB VRAM, all layers on the GPU"
    if fit.status == "hybrid":
        total = f"of {layers} " if layers else ""
        return "muted", f"{bar} {gib(fit.used_mib)} GiB needed, {fit.gpu_layers} {total}layers on the GPU, the rest in RAM"
    if fit.status == "cpu":
        return "muted", f"{gib(fit.used_mib)} GiB in system RAM, CPU only"
    return "bad", "does not fit: " + (fit.notes[-1] if fit.notes else "use a smaller model or context")


def render_screen(state: tunestate.TuneSession, look: Look, current: str) -> Group:
    """The main screen as a rich renderable."""
    pal, g = look.palette, look.glyphs
    rows: list[Text] = [Text("Hal tune", style=pal.accent), Text("")]
    rows += [Text(line, style=pal.muted if line.startswith("note") else "") for line in hardware_lines(state.hw)]
    rows += [Text(""), Text("Sources", style=pal.accent)]
    if not state.sources:
        rows.append(Text("none yet: press s to add one", style=pal.muted))
    for name, src in state.sources.items():
        found = state.found.get(name)
        count = f"{len(found.models)} models" if found and not found.error else (found.error if found else "not read")
        rows.append(Text.assemble((f"{name}", ""), (f"  {src.kind}  {src.url or src.path or 'key file'}  {g.sep} {count}", pal.muted)))
    rows += [Text(""), Text("Roles", style=pal.accent)]
    for role in modelconf.ROLES:
        d = state.drafts.get(role)
        mark = g.prompt if role == current else " "
        if d is None or not d.model:
            tail = " (falls back to chat)" if role == "writer" else ""
            rows.append(Text.assemble((f"{mark} ", pal.accent), (f"{role:<7}", ""), (f"-{tail}", pal.muted)))
            continue
        fit = state.fit(role)
        model = state.model_of(role)
        levels, reason = state.effort_options(role)
        eff = d.effort or "-" if levels else "n/a"
        ctx = f"{fit.context}" + ("" if d.manual else " (auto)") if fit.context else "-"
        layers = "all" if fit.gpu_layers < 0 else str(fit.gpu_layers)
        temp = "default" if d.temperature == modelconf.UNSET_TEMPERATURE else f"{d.temperature:g}"
        changed = " *" if role in state.changed_roles() else ""
        rows.append(Text.assemble((f"{mark} ", pal.accent), (f"{role:<7}", ""), (f"{model_label(state, d.source, d.model)}{changed}", "")))
        rows.append(Text(f"    context {ctx}  kv {fit.kv_type or 'auto'}  gpu layers {layers}  effort {eff}  temp {temp}", style=pal.muted))
        style, text = fit_line(fit, model.layers if model else None, look)
        rows.append(Text("    " + text, style={"ok": pal.ok, "bad": pal.bad}.get(style, pal.muted)))
    rows.append(Text(""))
    if state.has_changes():
        rows.append(Text("* changed, not written yet (d shows the diff, w writes)", style=pal.muted))
    rows.append(Text(f"role {current}. Commands: model context kv effort temp auto source diff write quit (? for help)", style=pal.muted))
    return Group(*rows)


def fit_word(fit: tunestate.Fit) -> str:
    """`fits`, `tight`, `too big` and the like: one word for how well a draft fits."""
    if fit.status == "gpu":
        return "tight" if fit.budget_mib and fit.used_mib > TIGHT_FRACTION * fit.budget_mib else "fits"
    return {"hybrid": "tight, uses RAM", "cpu": "CPU only", "does-not-fit": "too big"}.get(fit.status, "")


def fit_text(fit: tunestate.Fit, look: Look) -> str:
    """`9.4 GB  fits` for a sized fit; the fit line text otherwise."""
    if fit.status in ("gpu", "hybrid", "cpu", "does-not-fit") and fit.used_mib:
        return f"{gib(fit.used_mib)} GB  {fit_word(fit)}"
    return fit_line(fit, None, look)[1]


def short(text: str, width: int) -> str:
    """`text` cut to `width` characters, ending in `...` when it was cut."""
    return text if len(text) <= width else text[: width - 3] + "..."


def model_label(state: tunestate.TuneSession, source: str, name: str) -> str:
    """`source/name`, or `source/base (tag)` when `name` is a tag built from another model.

    Hal's role tags (`hal-chat:latest` and the like) say nothing about the weights behind them,
    so the model they were built from leads and the tag follows in brackets.
    """
    info = state.find(source, name)
    base = info.base if info else ""
    if not base or base == name:
        return f"{source}/{name}"
    return f"{source}/{base} ({name.removesuffix(':latest')})"


def role_line(state: tunestate.TuneSession, role: str, look: Look) -> tuple[str, str, str]:
    """`(model, context, fit text)` of one role, for the summary and the role menu."""
    d = state.drafts.get(role)
    if d is None or not d.model:
        direct = str(halconfig.hal().get(f"{role}_model") or "")
        if direct:
            # a gateway `backend/model` (such as a llama.cpp router preset) is set in `[hal]`, and
            # `[models.roles]` cannot describe it, so the screen shows it without tuning it
            return direct, "", "set in [hal] (not tuned)"
        return "-", "", "falls back to chat" if role == "writer" else "no model yet"
    fit = state.fit(role)
    ctx = f"{fit.context // 1024}K" if fit.context else ""
    return model_label(state, d.source, d.model), ctx, fit_text(fit, look)


def render_summary(state: tunestate.TuneSession, look: Look) -> Group:
    """The live summary above the menus: hardware, then one line per role."""
    pal = look.palette
    rows: list[Text] = [Text("Hal tune", style=pal.accent)]
    rows += [Text(line, style=pal.muted if line.startswith("note") else "") for line in hardware_lines(state.hw)]
    if state.preset is not None and state.preset.vram_mib is not None:
        rows.append(Text(f"Planning for: {state.preset.label} (a preset, not this machine)", style=pal.muted))
    if not state.sources:
        rows.append(Text("no model source yet: choose Model sources", style=pal.muted))
    rows.append(Text(""))
    changed = state.changed_roles()
    for role in modelconf.ROLES:
        model, ctx, fit = role_line(state, role, look)
        word = fit.rsplit(" ", 1)[-1]
        style = pal.ok if word == "fits" else (pal.bad if word == "big" else pal.muted)
        star = " *" if role in changed else "  "
        rows.append(
            Text.assemble(
                (f"{role:<7}", ""),
                (f"{short(model, SUMMARY_MODEL_WIDTH):<{SUMMARY_MODEL_WIDTH}}{star} ", ""),
                (f"{ctx:>4}  ", ""),
                (fit, style),
            )
        )
    guard, note = guard_line()
    rows.append(
        Text.assemble(
            (f"{'guard':<7}", ""),
            (f"{short(guard, SUMMARY_MODEL_WIDTH):<{SUMMARY_MODEL_WIDTH}}   ", ""),
            (f"{'':>4}  ", ""),
            (note, pal.muted),
        )
    )
    if changed:
        rows.append(Text("* changed, not saved yet", style=pal.muted))
    rows.append(Text(""))
    return Group(*rows)


def guard_line() -> tuple[str, str]:
    """`(model, note)` of the tool-call risk classifier, which `policy.toml` sets, not this screen.

    Shown so the summary names every model Hal runs; `hal tune` does not change it.
    """
    try:
        settings = load_policy().classifier
    except (halconfig.ConfigError, ValueError, OSError) as exc:
        return "?", f"policy.toml unreadable ({type(exc).__name__})"
    if settings.paranoia == "off":
        return "-", "safety check off (paranoia off)"
    where = "CPU" if not settings.gpu else "GPU when free"
    return settings.model, f"safety check, {where}"


# --- input -----------------------------------------------------------------------------------


class Prompter(Protocol):
    """How the screen asks questions; the real one is `PtPrompter`."""

    def ask(self, text: str, choices: Sequence[str] = ()) -> str | None:
        """One line of input, or None at end of input or on Ctrl-C."""

    def pick(
        self, title: str, items: Sequence[tuple[str, str]], current: str = "", *, filterable: bool = True, keys: str = ""
    ) -> str | None:
        """The value of the chosen `(label, value)` item, or None when cancelled.

        A short list (`filterable` false) takes 1 to 9 as shortcuts and `q` as back; a long one
        filters as you type. A key in `keys` is returned as itself.
        """


class ListView:
    """State, keys and drawing of one selectable list; `PtPrompter.pick` runs it, tests poke it directly."""

    def __init__(
        self, title: str, items: Sequence[tuple[str, str]], current: str, look: Look, colors: dict[str, str],
        *, filterable: bool, keys: str = "",
    ) -> None:
        self.title, self.items, self.current, self.look, self.colors = title, list(items), current, look, colors
        self.filterable, self.keys = filterable, keys
        self.query, self.finished, self.result = "", False, None
        self.app: Application[str | None] | None = None
        self.index = next((i for i, (_, v) in enumerate(self.items) if v == current), 0)

    def visible(self) -> list[tuple[str, str]]:
        """The items that match the typed filter."""
        return [it for it in self.items if fuzzy(self.query, it[0])]

    def finish(self, value: str | None) -> None:
        """Ends the list with `value`; a second call is ignored."""
        if self.finished:
            return
        self.finished, self.result = True, value
        if self.app is not None:
            self.app.exit(result=value)

    def move(self, step: int) -> None:
        """Moves the highlight, staying inside the list."""
        self.index = max(0, min(self.index + step, len(self.visible()) - 1))

    def press(self, char: str) -> None:
        """A printable key: filter text, a row shortcut, back or an extra key, by the kind of list."""
        shown = self.visible()
        if self.filterable:
            self.query, self.index = self.query + char, 0
        elif char.isdigit() and 1 <= int(char) <= min(len(shown), 9):
            self.finish(shown[int(char) - 1][1])
        elif char in "qQ":
            self.finish(None)
        elif char in self.keys:
            self.finish(char)

    def hint(self) -> str:
        """The one-line key help under the list."""
        sep = f" {self.look.glyphs.sep} "
        if self.filterable:
            return sep.join(("up/down move", "type to filter", "enter select", "esc back"))
        parts = ["up/down move", "enter select", "1-9 jump", "esc back"]
        return sep.join(parts + (["': commands'"] if COMMAND_KEY in self.keys else []))

    def click(self, event: MouseEvent, row: int) -> object:
        """Chooses `row` when the mouse button is released on it."""
        shown = self.visible()
        if event.event_type != MouseEventType.MOUSE_UP or row >= len(shown):
            return NotImplemented
        self.index = row
        self.finish(shown[row][1])
        return None

    def fragments(self) -> list[tuple]:  # type: ignore[type-arg]  # prompt_toolkit's fragments have 2 or 3 parts
        """The list as styled fragments; each row carries a mouse handler."""
        shown = self.visible()
        self.index = max(0, min(self.index, len(shown) - 1))
        first = 0
        if len(shown) > PICK_ROWS:
            first = max(0, min(self.index - PICK_ROWS // 2, len(shown) - PICK_ROWS))
        out: list[tuple] = [(self.colors["accent"], f"{self.title}  "), ("", f"{self.query}\n")]  # type: ignore[type-arg]
        for i in range(first, min(first + PICK_ROWS, len(shown))):
            label, value = shown[i]
            on = i == self.index
            number = f"{i + 1}. " if not self.filterable and i < 9 else ""
            tag = "*" if value == self.current else " "
            style = self.colors["accent"] if on else ""
            marker = self.look.glyphs.prompt if on else " "
            out.append((style, f"{marker} {tag} {number}{label}\n", lambda ev, row=i: self.click(ev, row)))
        if not shown:
            out.append((self.colors["muted"], "  no match\n"))
        out.append((self.colors["muted"], self.hint()))
        return out


class PtPrompter:
    """A `Prompter` on prompt_toolkit: a completing command line and a keyboard-and-mouse list."""

    def __init__(self, look: Look, *, input: Input | None = None, output: Output | None = None) -> None:  # noqa: A002 - prompt_toolkit's own names
        self.look = look
        self._input, self._output = input, output
        self._colors = PT_COLORS[look.theme]

    def ask(self, text: str, choices: Sequence[str] = ()) -> str | None:
        """One line of input with Tab completion of `choices`."""
        session: PromptSession[str] = PromptSession(
            completer=WordCompleter(list(choices), ignore_case=True) if choices else None,
            input=self._input, output=self._output,
        )
        try:
            return session.prompt(FormattedText([(self._colors["accent"], text)]))
        except (EOFError, KeyboardInterrupt):
            return None

    def pick(
        self, title: str, items: Sequence[tuple[str, str]], current: str = "", *, filterable: bool = True, keys: str = ""
    ) -> str | None:
        """Up and Down move, Enter or a click chooses, Esc or Ctrl-C cancels; see `ListView` for the rest."""
        if not items:
            return None
        view = ListView(title, items, current, self.look, self._colors, filterable=filterable, keys=keys)
        bindings = KeyBindings()

        @bindings.add("up")
        def _up(event) -> None:  # noqa: ANN001 - prompt_toolkit's KeyPressEvent
            view.move(-1)

        @bindings.add("down")
        def _down(event) -> None:
            view.move(1)

        @bindings.add("enter")
        def _enter(event) -> None:
            shown = view.visible()
            view.finish(shown[view.index][1] if shown else None)

        @bindings.add("escape", eager=True)
        @bindings.add("c-c")
        @bindings.add("c-d")
        def _cancel(event) -> None:
            view.finish(None)

        @bindings.add("backspace")
        def _back(event) -> None:
            if view.filterable:
                view.query, view.index = view.query[:-1], 0

        @bindings.add("<any>")
        def _type(event) -> None:
            if event.data.isprintable():
                view.press(event.data)

        rows = min(len(items), PICK_ROWS) + 2  # title, rows, hint
        view.app = Application(
            layout=Layout(Window(FormattedTextControl(view.fragments), height=rows, wrap_lines=False)),
            key_bindings=bindings, full_screen=False, input=self._input, output=self._output,
            mouse_support=True, style=Style.from_dict({}),
        )
        return view.app.run()


# --- the loop --------------------------------------------------------------------------------


class TuneScreen:
    """Runs the menus (and, on `:`, the typed commands) of the screen against a `TuneSession`."""

    def __init__(self, state: tunestate.TuneSession, look: Look, prompter: Prompter, console: Console) -> None:
        self.state, self.look, self.prompter, self.console = state, look, prompter, console
        self.role = DEFAULT_ROLE
        self.dirty = True

    def say(self, text: str, style: str = "") -> None:
        """Prints one line."""
        pal = self.look.palette
        self.console.print(Text(text, style={"bad": pal.bad, "ok": pal.ok, "muted": pal.muted}.get(style, style)))

    def run(self) -> int:
        """The menu loop; returns 0 on a normal exit."""
        while True:
            self.console.print(render_summary(self.state, self.look))
            choice = self.prompter.pick("What next?", MAIN_ITEMS, filterable=False, keys=COMMAND_KEY)
            if choice == COMMAND_KEY:
                status = self.run_commands()
                if status is not None:
                    return status
            elif choice == "preset":
                self.menu_presets()
            elif choice == "role":
                self.menu_roles()
            elif choice == "source":
                self._guarded(self.cmd_source, "")
            elif choice == "review":
                self.cmd_diff("")
            elif choice == "save":
                if not self.state.has_changes():
                    self.say("No changes to write.", "muted")
                    return 0
                if self._guarded(self.cmd_write, ""):
                    return 0
            elif choice == "quit":
                if not self.state.has_changes() or self._confirm("Leave without saving?", "no"):
                    return self._leave()
            elif choice is None and (not self.state.has_changes() or self._confirm("Leave without saving?", "no")):
                return self._leave()

    def _confirm(self, question: str, otherwise: str = "yes") -> bool:
        """A Yes / No menu; Esc and Ctrl-C answer `otherwise`."""
        answer = self.prompter.pick(question, YES_NO, otherwise, filterable=False) or otherwise
        return answer == "yes"

    def _guarded(self, handler: Callable[[str], object], arg: str) -> object:
        """Runs a command handler and reports its errors in red instead of raising."""
        try:
            return handler(arg)
        except (ValueError, OSError, *modelplan.plain_errors()) as exc:
            self.say(str(exc), "bad")
            return None

    def menu_presets(self) -> None:
        """Quick setup: tunes every role for a preset, then shows the result for review."""
        items = [
            (f"{p.label:<20} {tunepresets.describe(p, self.state.real_hw)}", p.key) for p in tunepresets.PRESETS
        ]
        key = self.prompter.pick("Quick setup", items, self.state.preset.key if self.state.preset else "auto", filterable=False)
        preset = next((p for p in tunepresets.PRESETS if p.key == key), None)
        if preset is None:
            return
        for note in self.state.apply_preset(preset):
            self.say(note, "bad" if "no discovered model" in note else "muted")
        self.cmd_diff("")
        self.say("This is a preview: choose Save and exit to write it, or adjust a role first.", "muted")

    def menu_roles(self) -> None:
        """Picks a role, then shows its menu until the user goes back."""
        while True:
            items = []
            for role in modelconf.ROLES:
                model, ctx, fit = role_line(self.state, role, self.look)
                items.append((f"{role:<7} {short(model, SUMMARY_MODEL_WIDTH)}  {ctx}", role))
            role = self.prompter.pick("Adjust which role?", items, self.role, filterable=False)
            if role is None:
                return
            self.role = role
            self.menu_role(role)

    def menu_role(self, role: str) -> None:
        """The settings of one role: model, context, KV cache, effort, temperature, auto-tune."""
        while True:
            model, ctx, fit = role_line(self.state, role, self.look)
            title = f"{role}: {short(model, SUMMARY_MODEL_WIDTH)} {ctx} {fit}".rstrip()
            choice = self.prompter.pick(title, ROLE_ITEMS, filterable=False)
            if choice in (None, "back"):
                return
            self._guarded(getattr(self, f"cmd_{choice}"), role)

    def run_commands(self) -> int | None:
        """The typed command loop (`:`); returns an exit status, or None for `menu` (back to the menus)."""
        self.console.print(Text("Typed commands: ? lists them, menu goes back to the menus.", style=self.look.palette.muted))
        while True:
            line = self.prompter.ask(f"tune ({self.role}) {self.look.glyphs.prompt} ", [*COMMANDS, *ALIASES])
            if line is None:
                return self._leave()
            if not line.strip():
                continue
            name, _, arg = line.strip().partition(" ")
            name = ALIASES.get(name.lower(), name.lower())
            if name == "quit":
                return self._leave()
            if name == "menu":
                return None
            handler = getattr(self, f"cmd_{name}", None)
            if handler is None:
                self.say(f"unknown command {name!r}; type ? for the list", "bad")
                continue
            self._guarded(handler, arg.strip())

    def _leave(self) -> int:
        if self.state.has_changes():
            self.say("Left without writing: the config is unchanged.", "muted")
        return 0

    def _target(self, arg: str) -> tuple[str, str]:
        """`(role, rest)`: a leading role name in `arg` overrides the current role."""
        first, _, rest = arg.partition(" ")
        if first in modelconf.ROLES:
            return first, rest.strip()
        return self.role, arg

    def cmd_help(self, arg: str) -> None:
        """Lists the commands."""
        for name, text in COMMANDS.items():
            self.say(f"  {name:<8}{text}")
        self.say("  Commands take a role first: model code, context fast 32k. Short forms: m c k e t a s d w q.", "muted")

    def cmd_role(self, arg: str) -> None:
        """Chooses the current role."""
        name = arg or self.prompter.pick("role", [(r, r) for r in modelconf.ROLES], self.role)
        if name not in modelconf.ROLES:
            if name:
                raise ValueError(f"roles: {', '.join(modelconf.ROLES)}")
            return
        self.role, self.dirty = name, True

    def cmd_model(self, arg: str) -> None:
        """Picks a model from the discovered ones."""
        role, _ = self._target(arg)
        items = []
        for m in self.state.models():
            if not modelplan._usable(m, role):  # noqa: SLF001 - same package
                continue
            size = f"{m.size_bytes / 1024**3:.1f} GiB" if m.size_bytes else "size ?"
            items.append((f"{model_label(self.state, m.source, m.name)}  {size}  {m.quant}  {self._fit_of(role, m)}".rstrip(), f"{m.source}\n{m.name}"))
        if not items:
            raise ValueError("no models found: add a source with s, or check that your server runs")
        d = self.state.drafts.get(role)
        current = f"{d.source}\n{d.model}" if d and d.model else ""
        chosen = self.prompter.pick(f"{role} model", items, current)
        if chosen:
            source, _, name = chosen.partition("\n")
            model = self.state.find(source, name)
            if model is not None:
                self.state.choose(role, model)
                self.role, self.dirty = role, True

    def _fit_of(self, role: str, model) -> str:  # noqa: ANN001 - a ModelInfo
        saved = self.state.drafts.get(role)
        self.state.drafts[role] = tunestate.Draft(model.source, model.name)
        try:
            fit = self.state.fit(role)
        finally:
            if saved is None:
                self.state.drafts.pop(role, None)
            else:
                self.state.drafts[role] = saved
        return {"gpu": "fits the GPU", "hybrid": "GPU and RAM", "cpu": "CPU", "does-not-fit": "too big"}.get(fit.status, "")

    def cmd_context(self, arg: str) -> None:
        """Sets the context with presets or a typed value, showing the fit of each."""
        role, rest = self._target(arg)
        self.state._need(role)  # noqa: SLF001 - same package; raises the "choose a model" error
        value = rest
        if not value:
            items = [("Auto: let tuning decide", "auto")]
            for tokens in tunestate.CONTEXT_PRESETS:
                fit = self.state.try_context(role, tokens)
                items.append((f"{tokens // 1024:>3}K  {self._fit_text(fit)}", str(tokens)))
            items.append(("Custom...", CUSTOM))
            value = self.prompter.pick(f"{role} context", items, filterable=False) or ""
            if value == CUSTOM:
                value = self.prompter.ask("tokens (for example 48000 or 48k): ") or ""
        if not value:
            return
        if value == "auto":
            self.state.set_kv(role, "auto")
        else:
            self.state.set_context(role, parse_tokens(value))
        self.dirty = True
        fit = self.state.fit(role)
        self.say(f"{role}: " + self._fit_text(fit), "ok" if fit.ok else "bad")

    def _fit_text(self, fit: tunestate.Fit) -> str:
        return fit_text(fit, self.look)

    def cmd_kv(self, arg: str) -> None:
        """Sets the KV cache type."""
        role, rest = self._target(arg)
        self.state._need(role)
        value = rest or self.prompter.pick(
            f"{role} KV type", [(f"{k:<5} {self._fit_text(self.state.try_kv(role, k))}", k) for k in tunestate.KV_OPTIONS],
            self.state.drafts[role].kv_type or "auto", filterable=False) or ""
        if value:
            self.state.set_kv(role, value)
            self.dirty = True

    def cmd_effort(self, arg: str) -> None:
        """Sets the default effort, only for models that support it."""
        role, rest = self._target(arg)
        self.state._need(role)
        levels, reason = self.state.effort_options(role)
        if not levels:
            self.say(f"{role}: no effort setting, {reason}", "muted")
            return
        current = self.state.drafts[role].effort or tunestate.DEFAULT_EFFORT
        value = rest or self.prompter.pick(
            f"{role} effort", [(tunestate.DEFAULT_EFFORT, tunestate.DEFAULT_EFFORT), *((lv, lv) for lv in levels)],
            current, filterable=False) or ""
        if value:
            self.state.set_effort(role, value)
            self.dirty = True

    def cmd_temp(self, arg: str) -> None:
        """Sets the temperature."""
        role, rest = self._target(arg)
        self.state._need(role)
        value = rest or self.prompter.pick(
            f"{role} temperature", [*((t.capitalize(), t) for t in TEMPERATURES), ("Custom...", CUSTOM)], filterable=False) or ""
        if value == CUSTOM:
            value = self.prompter.ask("temperature 0 to 2, or default: ") or ""
        if not value:
            return
        try:
            number = modelconf.UNSET_TEMPERATURE if value == "default" else float(value)
        except ValueError:
            raise ValueError(f"not a temperature: {value!r} (0 to 2, or default)") from None
        self.state.set_temperature(role, number)
        self.dirty = True

    def cmd_auto(self, arg: str) -> None:
        """Tunes one role (or `all`) for this hardware."""
        role, rest = self._target(arg)
        for line in self.state.auto_tune(None if rest == "all" else role):
            self.say(line, "muted")
        self.dirty = True

    def cmd_source(self, arg: str) -> None:
        """Adds or removes a model source."""
        action = arg.split(" ")[0] or self.prompter.pick("sources", [("Add a source", "add"), ("Remove a source", "remove")], filterable=False) or ""
        if action == "add":
            kind = self.prompter.pick("source kind", [(k, k) for k in modelconf.SOURCE_KINDS], filterable=False) or ""
            if not kind:
                return
            hints = {"ollama": "URL (blank for the default): ", "openai": "URL (blank for the default): ",
                     "gguf": "folder of .gguf files: "}
            location = (self.prompter.ask(hints.get(kind, "path of the key file: ")) or "").strip()
            name = self.state.add_source(kind, location)
            found = self.state.found[name]
            self.say(f"added {name}: " + (found.error or f"{len(found.models)} models"), "bad" if found.error else "ok")
        elif action == "remove":
            name = self.prompter.pick("remove source", [(n, n) for n in self.state.sources], filterable=False) or ""
            if name:
                self.state.remove_source(name)
                self.say(f"removed {name} (not written yet)", "muted")
        elif action:
            raise ValueError("use: source add, or source remove")
        self.dirty = True

    def cmd_diff(self, arg: str) -> None:
        """Shows what a write would change."""
        text = self.state.diff()
        if not text:
            self.say("no changes", "muted")
            return
        pal = self.look.palette
        for line in text.splitlines():
            style = pal.ok if line.startswith("+") and not line.startswith("+++") else (
                pal.bad if line.startswith("-") and not line.startswith("---") else pal.muted)
            self.console.print(Text(line, style=style))

    def cmd_write(self, arg: str) -> bool:
        """Shows the diff, asks Yes / No, then writes through the apply path; True when written."""
        if not self.state.has_changes():
            self.say("no changes to write", "muted")
            return False
        self.cmd_diff("")
        if not self._confirm("Write these changes?", "no"):
            self.say("Not written.", "muted")
            return False
        try:
            lines = self.state.write()
        except (ValueError, OSError, *modelplan.plain_errors()) as exc:
            self.say(f"{exc}", "bad")
            return False
        for line in lines:
            self.say(line, "ok" if line.startswith(("wrote", "pinned")) else "muted")
        self.dirty = True
        return True


def open_state(
    *, hw: hwmod.Hardware | None = None, discover: Callable | None = None, say: Callable[[str], None] | None = None
) -> tunestate.TuneSession:
    """Detects the hardware, reads the config and lists every source's models."""
    say = say or (lambda _: None)
    say("Detecting hardware and listing models...")
    sources, roles, limits = modelconf.load_sources(), modelconf.load_roles(), modelconf.limits()
    hw = hw or hwmod.detect()
    kwargs = {"discover": discover} if discover else {}
    found = modelplan.survey(sources, **kwargs)
    return tunestate.TuneSession(hw, sources, found, roles, limits, **kwargs)


def run_interactive(
    *, hw: hwmod.Hardware | None = None, out: TextIO | None = None, prompter: Prompter | None = None,
    look: Look | None = None, width: int | None = None, discover: Callable | None = None,
) -> int:
    """Opens the tune screen; returns the exit status of `hal tune`."""
    out = out or sys.stdout
    look = look or load_look(stream=out)
    console = make_console(look, out, width=width)
    try:
        state = open_state(hw=hw, discover=discover, say=lambda t: console.print(Text(t, style=look.palette.muted)))
    except (ValueError, OSError, *modelplan.plain_errors()) as exc:
        console.print(Text(f"hal tune: {exc}", style=look.palette.bad))
        return 1
    return TuneScreen(state, look, prompter or PtPrompter(look), console).run()
