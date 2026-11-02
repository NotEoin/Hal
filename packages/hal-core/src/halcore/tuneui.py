"""The screen of the interactive `hal tune`: rich output, prompt_toolkit input, the `hal code` look.

`halcore.tunestate.TuneSession` holds every decision; this module only draws it and reads the
user's keys. The look comes from `halcore.tui.theme` (glyphs, palette, `NO_COLOR`), so the screen
matches `hal code`: ASCII glyphs when the look says so, no colour on `mono`. The layout stacks
three lines per role instead of using a wide table, so it reads the same at 80 columns.

Input goes through a `Prompter`. `PtPrompter` is the real one (a command line with completion and
an arrow-key picker with fuzzy filtering); tests pass a scripted prompter or feed `PtPrompter`
a prompt_toolkit pipe input, so nothing here needs a real terminal.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable, Sequence
from typing import Protocol, TextIO

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
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.text import Text

from . import modelplan, tunestate
from .tui.status import PT_COLORS, context_bar
from .tui.theme import Look, load_look, make_console

log = logging.getLogger(__name__)

PICK_ROWS = 9  # list rows shown at once; the picker scrolls so a 24-line terminal still fits
MIB_PER_GIB = 1024
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
    "help": "show this list",
}
ALIASES = {"m": "model", "c": "context", "k": "kv", "e": "effort", "t": "temp", "a": "auto", "s": "source",
           "d": "diff", "w": "write", "q": "quit", "r": "role", "?": "help", "exit": "quit"}
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
        rows.append(Text.assemble((f"{mark} ", pal.accent), (f"{role:<7}", ""), (f"{d.source}/{d.model}{changed}", "")))
        rows.append(Text(f"    context {ctx}  kv {fit.kv_type or 'auto'}  gpu layers {layers}  effort {eff}  temp {temp}", style=pal.muted))
        style, text = fit_line(fit, model.layers if model else None, look)
        rows.append(Text("    " + text, style={"ok": pal.ok, "bad": pal.bad}.get(style, pal.muted)))
    rows.append(Text(""))
    if state.has_changes():
        rows.append(Text("* changed, not written yet (d shows the diff, w writes)", style=pal.muted))
    rows.append(Text(f"role {current}. Commands: model context kv effort temp auto source diff write quit (? for help)", style=pal.muted))
    return Group(*rows)


# --- input -----------------------------------------------------------------------------------


class Prompter(Protocol):
    """How the screen asks questions; the real one is `PtPrompter`."""

    def ask(self, text: str, choices: Sequence[str] = ()) -> str | None:
        """One line of input, or None at end of input or on Ctrl-C."""

    def pick(self, title: str, items: Sequence[tuple[str, str]], current: str = "") -> str | None:
        """The value of the chosen `(label, value)` item, or None when cancelled."""


class PtPrompter:
    """A `Prompter` on prompt_toolkit: a completing command line and an arrow-key picker."""

    def __init__(self, look: Look, *, input: Input | None = None, output: Output | None = None) -> None:  # noqa: A002 - prompt_toolkit's own names
        self.look = look
        self._input, self._output = input, output
        theme = "mono" if not look.color else ("light" if look.palette.muted == "grey42" else "dark")
        self._colors = PT_COLORS[theme]

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

    def pick(self, title: str, items: Sequence[tuple[str, str]], current: str = "") -> str | None:
        """Up and Down move, typing filters (fuzzy), Enter chooses, Esc or Ctrl-C cancels."""
        if not items:
            return None
        state = {"query": "", "index": 0}
        marker = self.look.glyphs.prompt

        def visible() -> list[tuple[str, str]]:
            return [it for it in items if fuzzy(state["query"], it[0])]

        def draw() -> FormattedText:
            shown = visible()
            state["index"] = max(0, min(state["index"], len(shown) - 1))
            first = max(0, min(state["index"] - PICK_ROWS // 2, len(shown) - PICK_ROWS))
            out: list[tuple[str, str]] = [(self._colors["accent"], f"{title}  "), ("", f"{state['query']}\n")]
            for i, (label, value) in enumerate(shown[first : first + PICK_ROWS], first):
                on = i == state["index"]
                tag = "*" if value == current else " "
                out.append((self._colors["accent"] if on else "", f"{marker if on else ' '} {tag} {label}\n"))
            if not shown:
                out.append((self._colors["muted"], "  no match\n"))
            out.append((self._colors["muted"], "Up/Down move, type to filter, Enter choose, Esc cancel"))
            return FormattedText(out)

        keys = KeyBindings()

        @keys.add("up")
        def _up(event) -> None:  # noqa: ANN001 - prompt_toolkit's KeyPressEvent
            state["index"] -= 1

        @keys.add("down")
        def _down(event) -> None:
            state["index"] += 1

        @keys.add("enter")
        def _enter(event) -> None:
            shown = visible()
            event.app.exit(result=shown[state["index"]][1] if shown else None)

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        @keys.add("c-d")
        def _cancel(event) -> None:
            event.app.exit(result=None)

        @keys.add("backspace")
        def _back(event) -> None:
            state["query"], state["index"] = state["query"][:-1], 0

        @keys.add("<any>")
        def _type(event) -> None:
            if event.data.isprintable():
                state["query"] += event.data
                state["index"] = 0

        app: Application[str | None] = Application(
            layout=Layout(Window(FormattedTextControl(draw), height=PICK_ROWS + 2, wrap_lines=False)),
            key_bindings=keys, full_screen=False, input=self._input, output=self._output,
            style=Style.from_dict({}),
        )
        return app.run()


# --- the loop --------------------------------------------------------------------------------


class TuneScreen:
    """Runs the commands of the screen against a `TuneSession`."""

    def __init__(self, state: tunestate.TuneSession, look: Look, prompter: Prompter, console: Console) -> None:
        self.state, self.look, self.prompter, self.console = state, look, prompter, console
        self.role = DEFAULT_ROLE
        self.dirty = True

    def say(self, text: str, style: str = "") -> None:
        """Prints one line."""
        pal = self.look.palette
        self.console.print(Text(text, style={"bad": pal.bad, "ok": pal.ok, "muted": pal.muted}.get(style, style)))

    def run(self) -> int:
        """The command loop; returns 0 on a normal exit."""
        while True:
            if self.dirty:
                self.console.print(render_screen(self.state, self.look, self.role))
                self.dirty = False
            line = self.prompter.ask(f"tune ({self.role}) {self.look.glyphs.prompt} ", [*COMMANDS, *ALIASES])
            if line is None:
                return self._leave()
            if not line.strip():
                continue
            name, _, arg = line.strip().partition(" ")
            name = ALIASES.get(name.lower(), name.lower())
            if name == "quit":
                return self._leave()
            handler = getattr(self, f"cmd_{name}", None)
            if handler is None:
                self.say(f"unknown command {name!r}; type ? for the list", "bad")
                continue
            try:
                handler(arg.strip())
            except (ValueError, OSError, *modelplan.plain_errors()) as exc:
                self.say(str(exc), "bad")

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
            items.append((f"{m.source}/{m.name}  {size}  {m.quant}  {self._fit_of(role, m)}".rstrip(), f"{m.source}\n{m.name}"))
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
            items = [("auto: let tuning decide", "auto")]
            for tokens in tunestate.CONTEXT_PRESETS:
                fit = self.state.try_context(role, tokens)
                items.append((f"{tokens:>7}  {self._fit_text(fit)}", str(tokens)))
            items.append(("type a number...", "custom"))
            value = self.prompter.pick(f"{role} context", items) or ""
            if value == "custom":
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
        if fit.status in ("gpu", "hybrid", "cpu"):
            where = {"gpu": "GPU", "hybrid": "GPU + RAM", "cpu": "CPU"}[fit.status]
            return f"{gib(fit.used_mib)} GiB, {where}"
        return fit_line(fit, None, self.look)[1]

    def cmd_kv(self, arg: str) -> None:
        """Sets the KV cache type."""
        role, rest = self._target(arg)
        self.state._need(role)
        value = rest or self.prompter.pick(
            f"{role} KV type", [(f"{k:<5} {self._fit_text(self.state.try_kv(role, k))}", k) for k in tunestate.KV_OPTIONS]) or ""
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
        value = rest or self.prompter.pick(f"{role} effort", [(lv, lv) for lv in levels], self.state.drafts[role].effort) or ""
        if value:
            self.state.set_effort(role, value)
            self.dirty = True

    def cmd_temp(self, arg: str) -> None:
        """Sets the temperature."""
        role, rest = self._target(arg)
        self.state._need(role)
        value = rest or self.prompter.ask("temperature 0 to 2, or default: ") or ""
        if not value:
            return
        self.state.set_temperature(role, modelconf.UNSET_TEMPERATURE if value == "default" else float(value))
        self.dirty = True

    def cmd_auto(self, arg: str) -> None:
        """Tunes one role (or `all`) for this hardware."""
        role, rest = self._target(arg)
        for line in self.state.auto_tune(None if rest == "all" else role):
            self.say(line, "muted")
        self.dirty = True

    def cmd_source(self, arg: str) -> None:
        """Adds or removes a model source."""
        action = arg.split(" ")[0] or self.prompter.pick("sources", [("add a source", "add"), ("remove a source", "remove")]) or ""
        if action == "add":
            kind = self.prompter.pick("source kind", [(k, k) for k in modelconf.SOURCE_KINDS]) or ""
            if not kind:
                return
            hints = {"ollama": "URL (blank for the default): ", "openai": "URL (blank for the default): ",
                     "gguf": "folder of .gguf files: "}
            location = (self.prompter.ask(hints.get(kind, "path of the key file: ")) or "").strip()
            name = self.state.add_source(kind, location)
            found = self.state.found[name]
            self.say(f"added {name}: " + (found.error or f"{len(found.models)} models"), "bad" if found.error else "ok")
        elif action == "remove":
            name = self.prompter.pick("remove source", [(n, n) for n in self.state.sources]) or ""
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

    def cmd_write(self, arg: str) -> None:
        """Shows the diff, asks, then writes through the apply path."""
        if not self.state.has_changes():
            self.say("no changes to write", "muted")
            return
        self.cmd_diff("")
        answer = self.prompter.ask("Write these changes? [y/N] ")
        if (answer or "").strip().lower() not in ("y", "yes"):
            self.say("Not written.", "muted")
            return
        try:
            lines = self.state.write()
        except (ValueError, OSError, *modelplan.plain_errors()) as exc:
            self.say(f"{exc}", "bad")
            return
        for line in lines:
            self.say(line, "ok" if line.startswith(("wrote", "pinned")) else "muted")
        self.dirty = True


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
