"""The full-screen `hal tune`: roles, sources and review in tabs, sliders for every number.

`halcore.tunestate.TuneSession` holds every decision; this app only draws it and turns clicks,
drags and keys into its edits, exactly like the line screen (`halcore.tuneui`), which stays as
the fallback. Nothing is written until Save, which shows the diff, asks with Cancel focused and
then writes through `TuneSession.write` (the `modelplan.apply` path of `hal tune --write`).

Layout: a hardware line, a preset row ("Plan for"), then tabs. Roles: a role list on the left and
the editor of the chosen role on the right (model, context, KV cache, GPU layers, effort,
temperature, a VRAM gauge and the reasoning). Sources: the model sources, add and remove.
Review: the config diff and Save. A first run (no role assigned) opens Quick start, which
previews what each preset would choose.

Design decisions:

- Sliders preview with `try_context` / `try_layers` while they move (pure maths, cheap) and edit
  the draft only on release or a key step (`Slider.Changed.final`), so a drag is one undo step.
- The editor is rebuilt only when the role or its model changes; other edits update the widgets
  in place, so keyboard focus stays where the user is.
- `[hal]` and `policy.toml` are read once when the app opens, not on every redraw.
- Every edit pushes a `TuneSession.snapshot`; Ctrl+Z restores the last one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

from halkit import modelconf, tuning
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, OptionList, TabbedContent, TabPane
from textual.widgets.option_list import Option

from . import modelplan, tunepresets, tunestate
from .tui import screens
from .tui.render import clean
from .tui.slash import PickItem
from .tui.theme import Look, textual_themes
from .tui.widgets import FitGauge, SafeStatic, Segment, Segmented, Slider, toast
from .tuneview import TIGHT_FRACTION, fit_line, fit_word, gib, guard_line, hal_models, hardware_lines, model_label

PRESET_SHORT = {"auto": "This machine", "cpu": "CPU only"}  # other presets show their label
TEMP_STEP = 0.05
TEMP_SHOWN_DEFAULT = 0.7  # where the knob rests while the temperature is the model's default
MAX_UNDO = 100
EXIT_OK = 0
GUARD = "guard"


def preset_label(preset: tunepresets.Preset) -> str:
    """A short label for the preset row."""
    return PRESET_SHORT.get(preset.key, preset.label.removesuffix(" GPU"))


def tokens_label(value: float) -> str:
    """`32K` for 32768 tokens."""
    return f"{int(value) // 1024}K"


def gauge_status(fit: tunestate.Fit) -> str:
    """The gauge colour key of a fit: `tight` for a GPU fit above `TIGHT_FRACTION` of the budget."""
    if fit.status == "gpu" and fit.budget_mib and fit.used_mib > TIGHT_FRACTION * fit.budget_mib:
        return "tight"
    return fit.status


def gauge_caption(fit: tunestate.Fit, look: Look) -> str:
    """`9.4 / 22.1 GB  fits` for a sized fit; the fit line otherwise."""
    if fit.status in ("gpu", "hybrid", "cpu", "does-not-fit") and fit.budget_mib:
        return f"{gib(fit.used_mib)} / {gib(fit.budget_mib)} GB  {fit_word(fit)}"
    if fit.status in ("gpu", "hybrid", "cpu", "does-not-fit"):
        return f"{gib(fit.used_mib)} GB  {fit_word(fit)}"
    return fit_line(fit, None, look)[1]


class RoleEditor(VerticalScroll, can_focus=False):
    """The settings of one role; `sync` updates the widgets in place from the draft."""

    DEFAULT_CSS = """
    RoleEditor { padding: 0 1; }
    RoleEditor .row { height: 1; margin-bottom: 1; }
    RoleEditor .label { width: 13; color: $text-muted; }
    RoleEditor .row Button { min-width: 9; height: 1; border: none; margin-left: 1; }
    RoleEditor .title { text-style: bold; color: $primary; margin-bottom: 1; }
    RoleEditor .why { color: $text-muted; height: auto; }
    RoleEditor .actions { height: 1; margin-top: 1; }
    RoleEditor .actions Button { height: 1; border: none; margin-right: 2; min-width: 12; }
    """

    def __init__(self, app_: TuneApp, role: str) -> None:
        super().__init__()
        self.tune, self.role = app_, role
        self.state = app_.state

    def compose(self) -> ComposeResult:
        """The fields of the role, or a read-only note for a role tune does not manage."""
        role, state = self.role, self.state
        if role == GUARD:
            model, note = self.tune.guard
            yield SafeStatic(Text(f"guard  {model}"), classes="title")
            yield SafeStatic(Text(f"{note}. Set in policy.toml; hal tune does not change it."), classes="why")
            return
        d = state.drafts.get(role)
        direct = self.tune.direct.get(role, "")
        if (d is None or not d.model) and direct:
            yield SafeStatic(Text(f"{role}  {direct}"), classes="title")
            yield SafeStatic(Text("Set in [hal] (not tuned): a gateway backend/model that [models.roles] cannot describe."), classes="why")
            yield Button("Choose a model to tune instead", id="model-btn")
            return
        yield SafeStatic(Text(role), classes="title")
        with Horizontal(classes="row"):
            yield SafeStatic(Text("Model"), classes="label")
            yield Button(self.model_text(), id="model-btn")
        if d is None or not d.model:
            yield SafeStatic(Text("No model yet: choose one, or Auto-tune this role.", style="dim"), classes="why")
            with Horizontal(classes="actions"):
                yield Button("Auto-tune this role", id="auto-role")
            return
        model = state.model_of(role)
        ceiling = int(state.limits["context_cap"])  # type: ignore[call-overload]  # validated config
        if model is not None and model.context_native:
            ceiling = min(ceiling, model.context_native)
        ceiling = max(ceiling, tuning.MIN_CONTEXT * 2)
        with Horizontal(classes="row"):
            yield SafeStatic(Text("Context"), classes="label")
            yield Slider(tuning.MIN_CONTEXT, tuning.MIN_CONTEXT, ceiling, tuning.CONTEXT_STEP, log=True,
                         fmt=tokens_label, allow_auto=True, auto_label=lambda v: f"auto {tokens_label(v)}",
                         id="ctx")  # fmt: skip
            yield Button("Auto", id="ctx-auto")
        with Horizontal(classes="row"):
            yield SafeStatic(Text("KV cache"), classes="label")
            yield Segmented([Segment(k, k) for k in tunestate.KV_OPTIONS], "auto", id="kv")
        total, reason = state.layers_of(role)
        with Horizontal(classes="row"):
            yield SafeStatic(Text("GPU layers"), classes="label")
            yield Slider(total, 0, max(total, 1), 1, fmt=lambda v: "all" if int(v) == total else f"{int(v)}",
                         allow_auto=True, auto_label=lambda v: "auto all" if int(v) == total else f"auto {int(v)}",
                         reason=reason, disabled=not total, id="layers")  # fmt: skip
            yield Button("Auto", id="layers-auto", disabled=not total)
        levels, why_not = state.effort_options(role)
        segments = [Segment(tunestate.DEFAULT_EFFORT, tunestate.DEFAULT_EFFORT)]
        segments += [Segment(lv, lv, "" if lv in levels else (why_not or "not for this model")) for lv in modelconf.EFFORT_CHOICES]
        with Horizontal(classes="row"):
            yield SafeStatic(Text("Effort"), classes="label")
            yield Segmented(segments, tunestate.DEFAULT_EFFORT, id="effort")
        with Horizontal(classes="row"):
            yield SafeStatic(Text("Temperature"), classes="label")
            yield Slider(TEMP_SHOWN_DEFAULT, 0, modelconf.MAX_TEMPERATURE, TEMP_STEP, allow_auto=True,
                         fmt=lambda v: f"{v:.2f}", auto_label=lambda v: "default", id="temp")  # fmt: skip
            yield Button("Default", id="temp-default")
        with Horizontal(classes="row"):
            yield SafeStatic(Text("Memory"), classes="label")
            yield FitGauge(id="gauge")
        yield SafeStatic("", classes="why", id="why")
        with Horizontal(classes="actions"):
            yield Button("Auto-tune this role", id="auto-role")
            yield Button("Clear role", id="clear-role")

    def model_text(self) -> Text:
        """The model button's label: the real model behind the role, then `change`."""
        d = self.state.drafts.get(self.role)
        if d is None or not d.model:
            return Text("choose a model...")
        return Text(f"{model_label(self.state, d.source, d.model)}  (change)")

    def on_mount(self) -> None:
        """Fills the widgets from the draft."""
        self.sync()

    def sync(self) -> None:
        """Sets every widget from the draft and its fit, without posting change messages."""
        if not self.query("#ctx"):
            return
        state, role = self.state, self.role
        d = state.drafts[role]
        fit = state.fit(role)
        sized = fit.status in ("gpu", "hybrid", "cpu", "does-not-fit")
        ctx = self.query_one("#ctx", Slider)
        ctx.disabled = not sized
        ctx.reason = "" if sized else (fit.notes[0] if fit.notes else "the server decides")
        _quiet(ctx, float(d.context or fit.context or tuning.MIN_CONTEXT), auto=not d.context)
        kv = self.query_one("#kv", Segmented)
        auto_kv = fit.kv_type if not d.kv_type and fit.kv_type else ""
        kv.segments = [Segment(f"auto ({auto_kv})" if k == "auto" and auto_kv else k, k) for k in tunestate.KV_OPTIONS]
        kv.selected = d.kv_type or "auto"
        kv.refresh(layout=True)
        layers = self.query_one("#layers", Slider)
        total, _ = state.layers_of(role)
        if total:
            shown = d.gpu_layers if d.gpu_layers is not None else (total if fit.gpu_layers < 0 else fit.gpu_layers)
            _quiet(layers, float(shown), auto=d.gpu_layers is None)
        effort = self.query_one("#effort", Segmented)
        effort.selected = d.effort or tunestate.DEFAULT_EFFORT
        temp = self.query_one("#temp", Slider)
        unset = d.temperature == modelconf.UNSET_TEMPERATURE
        _quiet(temp, TEMP_SHOWN_DEFAULT if unset else d.temperature, auto=unset)
        self.show_fit(fit)

    def show_fit(self, fit: tunestate.Fit) -> None:
        """The gauge and the reasoning for `fit` (the draft's, or a preview while a slider moves)."""
        self.query_one("#gauge", FitGauge).show(fit.used_mib, fit.budget_mib, gauge_status(fit), gauge_caption(fit, self.tune.look))
        notes = list(fit.notes)
        self.query_one("#why", SafeStatic).update(Text("\n".join(f"- {clean(n)}" for n in notes), style="dim"))


def _quiet(slider: Slider, value: float, *, auto: bool) -> None:
    """Sets a slider without posting `Changed` (the draft is the source of the value)."""
    slider.set_reactive(Slider.value, min(slider.high, max(slider.low, value)))
    slider.set_reactive(Slider.auto, auto)
    slider.refresh()


class SourceModal(ModalScreen[str | None]):
    """Add a source: pick the kind, type the location; an error shows inline and keeps the text."""

    DEFAULT_CSS = screens.MODAL_CSS + "SourceModal { align: center middle; } SourceModal .error { color: $error; height: auto; }"
    BINDINGS = [Binding("escape", "cancel", "cancel")]
    HINTS = {"ollama": "URL (blank for the default)", "openai": "URL (blank for the default)", "gguf": "folder of .gguf files"}

    def __init__(self, submit: Callable[[str, str], str]) -> None:
        super().__init__()
        self.submit = submit  # returns an error message, or "" when the source was added

    def compose(self) -> ComposeResult:
        """Kind, location, error line and buttons."""
        with Vertical(classes="modal-box"):
            yield SafeStatic(Text("Add a model source"), classes="modal-title")
            yield Segmented([Segment(k, k) for k in modelconf.SOURCE_KINDS], modelconf.SOURCE_KINDS[0], id="kind")
            yield Input(placeholder=self.HINTS["ollama"], id="location")
            yield SafeStatic("", classes="error", id="error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Add", id="add", variant="primary")

    def on_mount(self) -> None:
        """Focus on the location."""
        self.query_one("#location", Input).focus()

    def on_segmented_changed(self, event: Segmented.Changed) -> None:
        """The location hint follows the kind."""
        self.query_one("#location", Input).placeholder = self.HINTS.get(event.value, "path of the key file")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter adds."""
        self.try_add()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Add or Cancel."""
        if event.button.id == "add":
            self.try_add()
        else:
            self.dismiss(None)

    def try_add(self) -> None:
        """Adds the source, or shows why not and stays open."""
        kind = self.query_one("#kind", Segmented).selected
        location = self.query_one("#location", Input).value.strip()
        error = self.submit(kind, location)
        if error:
            self.query_one("#error", SafeStatic).update(Text(clean(error)))
            return
        self.dismiss(kind)

    def action_cancel(self) -> None:
        """Esc closes without adding."""
        self.dismiss(None)


class PresetCard(Vertical, can_focus=True):
    """One Quick start choice: the preset and what it would pick per role; click or Enter takes it."""

    DEFAULT_CSS = """
    PresetCard { width: 1fr; height: auto; border: round $panel; padding: 0 1; }
    PresetCard:hover, PresetCard:focus { border: round $primary; }
    PresetCard .card-title { text-style: bold; }
    """
    BINDINGS = [Binding("enter", "take", "use this", show=False)]

    def __init__(self, preset: tunepresets.Preset, title: str, lines: list[Text]) -> None:
        super().__init__(id=f"card-{preset.key}")
        self.preset, self.title_text, self.lines = preset, title, lines

    def compose(self) -> ComposeResult:
        """Title and one line per role."""
        yield SafeStatic(Text(self.title_text), classes="card-title")
        for line in self.lines:
            yield SafeStatic(line)

    def action_take(self) -> None:
        """Takes this preset."""
        self.screen.dismiss(self.preset.key)

    def on_click(self) -> None:
        """A click takes this preset."""
        self.action_take()


class QuickStart(ModalScreen[str | None]):
    """First run: what each preset would choose, computed on a throwaway copy of the drafts."""

    DEFAULT_CSS = screens.MODAL_CSS + """
    QuickStart { align: center middle; }
    QuickStart .modal-box { width: 96%; max-width: 140; }
    QuickStart #scroll { height: auto; max-height: 30; }
    QuickStart #cards { grid-size: 2; grid-gutter: 0 1; height: auto; }
    """
    BINDINGS = [Binding("escape", "skip", "skip")]

    def __init__(self, previews: list[tuple[tunepresets.Preset, str, list[Text]]]) -> None:
        super().__init__()
        self.previews = previews

    def compose(self) -> ComposeResult:
        """A card per preset and two ways out."""
        with Vertical(classes="modal-box"):
            yield SafeStatic(Text("Quick start: pick what to plan for"), classes="modal-title")
            yield SafeStatic(Text("Nothing is written until you save. Click a card or press Enter on it.", style="dim"))
            with VerticalScroll(id="scroll"), Grid(id="cards"):
                for preset, title, lines in self.previews:
                    yield PresetCard(preset, title, lines)
            with Horizontal(classes="modal-buttons"):
                yield Button("Skip for now", id="skip")
                yield Button("Adjust by hand", id="manual")

    def on_mount(self) -> None:
        """Focus on the first card (this machine)."""
        self.query(PresetCard).first().focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Skip leaves hal tune; Adjust by hand goes to the roles."""
        self.dismiss("" if event.button.id == "manual" else None)

    def action_skip(self) -> None:
        """Esc: leave without writing."""
        self.dismiss(None)


class TuneApp(App[int]):
    """The full-screen tune app; `run()` returns the exit status of `hal tune`."""

    TITLE = "Hal tune"
    ENABLE_COMMAND_PALETTE = False  # its Quit would skip the unsaved-changes question
    CSS = """
    Screen { layout: vertical; overflow: hidden; }
    #tabs { height: 1fr; }
    #tabs ContentSwitcher { height: 1fr; }
    #tabs TabPane { height: 1fr; padding: 0; }
    #hw { height: auto; color: $text-muted; padding: 0 1; }
    #presets { height: 1; padding: 0 1; margin-bottom: 1; }
    #presets .label { width: 10; color: $text-muted; }
    #roles { width: 40; height: 1fr; border: none; }
    Button#model-btn { width: 1fr; content-align: left middle; text-align: left; }
    #role-pane { height: 1fr; }
    #sources { height: auto; max-height: 12; border: none; }
    .bar { height: 1; margin-top: 1; }
    .bar Button { height: 1; border: none; margin-right: 2; min-width: 10; }
    #source-note { color: $text-muted; height: auto; }
    #diff { height: 1fr; }
    """
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+z", "undo", "Undo"),
        Binding("escape", "leave", "Quit"),
        Binding("ctrl+q", "leave", "Quit", show=False),
        Binding("ctrl+c", "leave", "Quit", show=False, priority=True),
        Binding("f1", "help", "Help"),
        Binding("f2", "show_tab('tab-roles')", "Roles"),
        Binding("f3", "show_tab('tab-sources')", "Sources"),
        Binding("f4", "show_tab('tab-review')", "Review"),
    ]

    def __init__(self, state: tunestate.TuneSession, look: Look, *, first_run: bool | None = None) -> None:
        super().__init__()
        self.state, self.look = state, look
        self.unicode = look.unicode
        self.direct: Mapping[str, str] = hal_models()
        self.guard = guard_line()
        self.first_run = first_run if first_run is not None else not state.drafts
        self.role = "chat"
        self.undo: list[tunestate.Snapshot] = []
        self.mounted = False
        self.written: list[str] = []
        self.left_unsaved = False
        if not look.animations:
            self.animation_level = "none"

    # -- layout ----------------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Hardware, presets, the three tabs and the key footer."""
        yield SafeStatic(self.hardware_text(), id="hw")
        with Horizontal(id="presets"):
            yield SafeStatic(Text("Plan for"), classes="label")
            current = self.state.preset.key if self.state.preset else ""
            yield Segmented([Segment(preset_label(p), p.key) for p in tunepresets.PRESETS], current, id="preset")
        with TabbedContent(initial="tab-roles", id="tabs"):
            with TabPane("Roles", id="tab-roles"), Horizontal(id="role-pane"):
                yield OptionList(markup=False, id="roles")
                self._editor = RoleEditor(self, self.role)
                yield self._editor
            with TabPane("Sources", id="tab-sources"), Vertical():
                yield OptionList(markup=False, id="sources")
                yield SafeStatic("", id="source-note")
                with Horizontal(classes="bar"):
                    yield Button("Add source...", id="source-add")
                    yield Button("Remove", id="source-remove")
            with TabPane("Review", id="tab-review"), Vertical():
                yield VerticalScroll(id="diff")
                with Horizontal(classes="bar"):
                    yield Button("Save...", id="save", variant="primary")
        yield Footer()

    def hardware_text(self) -> Text:
        """The hardware lines; notes and warnings dimmed."""
        lines = hardware_lines(self.state.real_hw)
        text = Text()
        for i, line in enumerate(lines):
            text.append(clean(line) + ("\n" if i < len(lines) - 1 else ""), style="bold" if line.startswith("note") else "")
        return text

    def on_mount(self) -> None:
        """Theme, lists, and Quick start on a first run."""
        self.mounted = True
        for theme in textual_themes():
            self.register_theme(theme)
        self.theme = self.look.textual_theme
        self.fill_roles()
        self.fill_sources()
        self.query_one("#roles", OptionList).focus()
        if self.first_run and self.state.models():
            self.push_screen(QuickStart(self.previews()), self.quick_start_done)

    # -- role list -------------------------------------------------------------------------------

    def role_row(self, role: str) -> Text:
        """One role list row: role, model, a star when changed, and the fit word."""
        if role == GUARD:
            model, note = self.guard
            row = Text(f"{GUARD:<7}", style="bold")
            row.append(clean(model))
            row.append(f"\n        ({clean(note)})", style="dim")
            return row
        d = self.state.drafts.get(role)
        row = Text(f"{role:<7}", style="bold")
        if d is None or not d.model:
            direct = self.direct.get(role, "")
            row.append(clean(direct) if direct else "-", style="" if direct else "dim")
            note = "set in [hal] (not tuned)" if direct else ("falls back to chat" if role == "writer" else "no model yet")
            row.append(f"\n        {note}", style="dim")
            return row
        row.append(clean(model_label(self.state, d.source, d.model)))
        if role in self.state.changed_roles():
            row.append(" *", style="bold")
        fit = self.state.fit(role)
        word = fit_word(fit) or fit.status
        row.append(f"\n        {tokens_label(fit.context) + '  ' if fit.context else ''}{word}", style="dim")
        return row

    def fill_roles(self) -> None:
        """Redraws the role list, keeping the highlight."""
        roles = self.query_one("#roles", OptionList)
        keep = roles.highlighted
        names = [*modelconf.ROLES, GUARD]
        roles.set_options([Option(self.role_row(r), id=r) for r in names])
        roles.highlighted = keep if keep is not None else names.index(self.role)
        changes = len(self.state.changed_roles()) + (1 if self.state.has_changes() and not self.state.changed_roles() else 0)
        self.query_one("#tabs", TabbedContent).get_tab("tab-review").label = f"Review {changes}" if changes else "Review"

    @on(OptionList.OptionHighlighted, "#roles")
    def role_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Moving in the role list opens that role's editor."""
        role = str(event.option.id)
        if role != self.role:
            self.role = role
            self.rebuild_editor()

    def rebuild_editor(self) -> None:
        """Replaces the editor (a new role, or a new model with other ranges)."""
        old = self.editor()
        self._editor = RoleEditor(self, self.role)
        old.remove()
        self.query_one("#role-pane").mount(self._editor)

    def editor(self) -> RoleEditor:
        """The open role editor."""
        return self._editor

    # -- edits -----------------------------------------------------------------------------------

    def change(self, edit: Callable[[], object], *, rebuild: bool = False) -> object:
        """Runs one edit with an undo snapshot; a refused edit is undone and shown, never raised."""
        snap = self.state.snapshot()
        try:
            result = edit()
        except ValueError as exc:
            self.state.restore(snap)
            toast(self, clean(exc), title="Not changed", severity="error")
            self.editor().sync()
            return None
        self.undo = [*self.undo[-MAX_UNDO:], snap]
        self.after_change(rebuild=rebuild)
        return result

    def after_change(self, *, rebuild: bool = False) -> None:
        """Redraws what an edit can change."""
        self.fill_roles()
        if rebuild:
            self.rebuild_editor()
        else:
            self.editor().sync()
        if self.query_one("#tabs", TabbedContent).active == "tab-review":
            self.fill_review()

    def on_slider_changed(self, event: Slider.Changed) -> None:
        """Previews while a slider moves; edits the draft when it lands."""
        role, state, which = self.role, self.state, event.slider.id
        if which == "ctx":
            if event.auto:
                self.change(lambda: state.set_kv(role, "auto"))
            elif event.final:
                self.change(lambda: state.set_context(role, int(event.value)))
            else:
                self.editor().show_fit(state.try_context(role, int(event.value)))
        elif which == "layers":
            if event.auto:
                self.change(lambda: state.set_gpu_layers(role, None))
            elif event.final:
                self.change(lambda: state.set_gpu_layers(role, int(event.value)))
            else:
                self.editor().show_fit(state.try_layers(role, int(event.value)))
        elif which == "temp" and (event.final or event.auto):
            value = modelconf.UNSET_TEMPERATURE if event.auto else event.value
            self.change(lambda: state.set_temperature(role, value))

    def on_segmented_changed(self, event: Segmented.Changed) -> None:
        """KV cache, effort or a preset."""
        role, state = self.role, self.state
        if event.segmented.id == "kv":
            self.change(lambda: state.set_kv(role, event.value))
        elif event.segmented.id == "effort":
            self.change(lambda: state.set_effort(role, event.value))
        elif event.segmented.id == "preset":
            self.take_preset(event.value)

    def take_preset(self, key: str) -> None:
        """Tunes every role for the preset and lists what changed."""
        preset = next(p for p in tunepresets.PRESETS if p.key == key)
        notes = self.change(lambda: self.state.apply_preset(preset), rebuild=True)
        if notes:
            missing = [n for n in notes if "no discovered model" in n]  # type: ignore[union-attr]
            toast(
                self,
                Text("\n".join(clean(n) for n in notes)),  # type: ignore[union-attr]
                title=f"Planned for {preset.label}", severity="warning" if missing else "information", timeout=8,
            )  # fmt: skip
        segmented = self.query_one("#preset", Segmented)
        segmented.selected = key

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Every button of the app; a button that opens a modal runs it in a worker."""
        role, state, which = self.role, self.state, event.button.id
        if which == "model-btn":
            self.run_worker(self.pick_model(), exclusive=True, group="pick")
        elif which == "ctx-auto":
            self.change(lambda: state.set_kv(role, "auto"))
        elif which == "layers-auto":
            self.change(lambda: state.set_gpu_layers(role, None))
        elif which == "temp-default":
            self.change(lambda: state.set_temperature(role, modelconf.UNSET_TEMPERATURE))
        elif which == "auto-role":
            notes = self.change(lambda: state.auto_tune(role), rebuild=True)
            if notes:
                toast(self, "\n".join(map(str, notes)), title=f"Tuned {role}")  # type: ignore[arg-type]
        elif which == "clear-role":
            self.change(lambda: state.clear(role), rebuild=True)
        elif which == "source-add":
            self.push_screen(SourceModal(self.add_source), lambda _: self.after_sources())
        elif which == "source-remove":
            self.remove_source()
        elif which == "save":
            self.action_save()

    async def pick_model(self) -> None:
        """The model picker: usable models for the role, each with its size, quant and fit."""
        role, state = self.role, self.state
        items = []
        for m in state.models():
            if not modelplan._usable(m, role):  # noqa: SLF001 - same package
                continue
            size = f"{m.size_bytes / 1024**3:.1f} GiB" if m.size_bytes else "size ?"
            word = fit_word(self.preview_fit(role, m))
            items.append(PickItem(model_label(state, m.source, m.name), f"{m.source}\n{m.name}", f"{size}  {m.quant}  {word}".strip()))
        if not items:
            toast(self, "No models found: add a source on the Sources tab, or check that your server runs.", severity="warning")
            return
        d = state.drafts.get(role)
        current = f"{d.source}\n{d.model}" if d and d.model else ""
        chosen = await screens.ask(self, screens.PickerModal(f"{role} model", items, current))
        if not chosen:
            return
        source, _, name = chosen.partition("\n")
        model = state.find(source, name)
        if model is not None:
            self.change(lambda: state.choose(role, model), rebuild=True)

    def preview_fit(self, role: str, model) -> tunestate.Fit:  # noqa: ANN001 - a ModelInfo
        """The fit `role` would have with `model`; the draft is left as it was."""
        saved = self.state.drafts.get(role)
        self.state.drafts[role] = tunestate.Draft(model.source, model.name)
        try:
            return self.state.fit(role)
        finally:
            if saved is None:
                self.state.drafts.pop(role, None)
            else:
                self.state.drafts[role] = saved

    def action_show_tab(self, pane: str) -> None:
        """F2, F3, F4: the Roles, Sources and Review tabs.

        Focus leaves the old tab first: a focused widget in a hidden tab pulls the tabs back to it.
        """
        self.set_focus(None)
        self.query_one("#tabs", TabbedContent).active = pane
        first = {"tab-roles": "#roles", "tab-sources": "#sources", "tab-review": "#save"}[pane]
        self.call_after_refresh(self.query_one(first).focus)

    def action_undo(self) -> None:
        """Ctrl+Z: puts back the state before the last edit."""
        if not self.undo:
            toast(self, "Nothing to undo.")
            return
        self.state.restore(self.undo.pop())
        self.fill_sources()
        preset = self.query_one("#preset", Segmented)
        preset.selected = self.state.preset.key if self.state.preset else ""
        self.after_change(rebuild=True)

    # -- sources ---------------------------------------------------------------------------------

    def fill_sources(self) -> None:
        """Redraws the source list."""
        rows = []
        for name, src in self.state.sources.items():
            found = self.state.found.get(name)
            count = (found.error or f"{len(found.models)} models") if found else "not read"
            row = Text(f"{name:<12}", style="bold")
            row.append(f"{src.kind:<13}{clean(src.url or src.path or 'key file')}  ")
            row.append(clean(count), style="dim")
            rows.append(Option(row, id=name))
        sources = self.query_one("#sources", OptionList)
        sources.set_options(rows or [Option(Text("no source yet: Add source... to begin", style="dim"), disabled=True)])
        if rows:
            sources.highlighted = 0
        self.source_highlighted()

    @on(OptionList.OptionHighlighted, "#sources")
    def source_highlighted(self, event: OptionList.OptionHighlighted | None = None) -> None:
        """Remove is disabled, with the reason, while a role uses the highlighted source."""
        name = self.highlighted_source()
        users = [r for r, d in self.state.drafts.items() if d.source == name] if name else []
        button = self.query_one("#source-remove", Button)
        button.disabled = not name or bool(users)
        note = f"roles {', '.join(users)} use {name}; choose other models first" if users else ""
        self.query_one("#source-note", SafeStatic).update(Text(note))

    def highlighted_source(self) -> str:
        """The name of the highlighted source, or ""."""
        sources = self.query_one("#sources", OptionList)
        if sources.highlighted is None or not self.state.sources:
            return ""
        return str(sources.get_option_at_index(sources.highlighted).id or "")

    def add_source(self, kind: str, location: str) -> str:
        """`SourceModal`'s submit: adds the source as one undoable edit; returns an error or ""."""
        snap = self.state.snapshot()
        try:
            name = self.state.add_source(kind, location)
        except ValueError as exc:
            self.state.restore(snap)
            return str(exc)
        self.undo.append(snap)
        found = self.state.found[name]
        toast(self, clean(found.error or f"{len(found.models)} models"), title=f"Added {name}",
                    severity="warning" if found.error else "information")  # fmt: skip
        return ""

    def after_sources(self) -> None:
        """Redraws after the source list changed."""
        self.fill_sources()
        self.after_change(rebuild=True)

    def remove_source(self) -> None:
        """Removes the highlighted source (not written until Save)."""
        name = self.highlighted_source()
        if name:
            self.change(lambda: self.state.remove_source(name))
            self.fill_sources()

    # -- review and save -------------------------------------------------------------------------

    @on(TabbedContent.TabActivated)
    def tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """The Review tab shows the current diff."""
        if event.pane.id == "tab-review":
            self.fill_review()

    def diff_text(self) -> list[Text]:
        """The config diff, coloured; or why it cannot be built."""
        try:
            text = self.state.diff()
        except (ValueError, OSError, *modelplan.plain_errors()) as exc:
            return [Text(clean(exc), style="bold")]
        if not text:
            return [Text("No changes yet.", style="dim")]
        out = []
        for line in text.splitlines():
            style = "dim"
            if line.startswith("+") and not line.startswith("+++"):
                style = "green"
            elif line.startswith("-") and not line.startswith("---"):
                style = "red"
            out.append(Text(clean(line), style=style))
        return out

    def fill_review(self) -> None:
        """Redraws the diff."""
        pane = self.query_one("#diff", VerticalScroll)
        pane.remove_children()
        pane.mount_all([SafeStatic(line) for line in self.diff_text()])

    def action_save(self) -> None:
        """Ctrl+S: exits when nothing changed, else confirms (Cancel focused) and writes."""
        if not self.state.has_changes():
            self.exit(EXIT_OK, message="No changes to write.")
            return
        self.run_worker(self.save_flow(), exclusive=True, group="save")

    async def save_flow(self) -> None:
        """Diff, confirm, write in a thread, then Done or Keep tuning."""
        body = Text("\n").join(self.diff_text())
        choice = await screens.ask(
            self, screens.ChoiceModal("Write these changes to config.toml?", body,
                                      [screens.Choice("Cancel", "cancel"), screens.Choice("Write", "write", "primary")], "cancel"),
        )  # fmt: skip
        if choice != "write":
            return
        try:
            lines = await asyncio.to_thread(self.state.write)
        except (ValueError, OSError, *modelplan.plain_errors()) as exc:
            toast(self, clean(exc), title="Nothing written", severity="error", timeout=10)
            return
        self.written = list(lines)
        self.undo.clear()
        self.after_change(rebuild=True)
        done = await screens.ask(
            self, screens.ChoiceModal("Saved", Text("\n".join(map(clean, lines))),
                                      [screens.Choice("Keep tuning", "keep"), screens.Choice("Done", "done", "primary")], "done"),
        )  # fmt: skip
        if done == "done":
            self.exit(EXIT_OK)

    def action_leave(self) -> None:
        """Esc, Ctrl+Q, Ctrl+C: quits at once without changes; with changes asks, Keep editing focused."""
        if not self.state.has_changes():
            self.exit(EXIT_OK)
            return
        self.run_worker(self.leave_flow(), exclusive=True, group="leave")

    async def leave_flow(self) -> None:
        """The unsaved-changes question."""
        count = len(self.state.changed_roles())
        body = Text(f"{count} role{'s' if count != 1 else ''} changed and not saved." if count else "Source changes are not saved.")
        choice = await screens.ask(
            self, screens.ChoiceModal("Leave hal tune?", body, [
                screens.Choice("Keep editing", "keep"), screens.Choice("Discard and quit", "discard", "error"),
                screens.Choice("Save...", "save", "primary"),
            ], "keep"),
        )  # fmt: skip
        if choice == "discard":
            self.left_unsaved = True
            self.exit(EXIT_OK)
        elif choice == "save":
            await self.save_flow()

    async def action_quit(self) -> None:
        """Every way to quit (Textual's own Ctrl+Q too) asks first when there are unsaved changes."""
        self.action_leave()

    def action_help(self) -> None:
        """F1: the keys and what each control does."""
        keys = [("Tab / Shift+Tab", "next / previous control"), ("Left / Right", "adjust a slider or choose a segment"),
                ("PgUp / PgDn, Home / End", "big slider steps, ends"), ("a", "back to auto on a slider"),
                ("Ctrl+Z", "undo the last change"), ("Ctrl+S", "save (shows the diff first)"), ("Esc", "quit (asks when unsaved)")]  # fmt: skip
        mouse = [("click", "buttons, segments, list rows"), ("drag", "slider knobs; the memory bar follows"),
                 ("wheel", "slider steps, scrolling"), ("Shift+drag", "select text in most terminals")]  # fmt: skip
        self.push_screen(screens.HelpScreen("hal tune", [screens.HelpSection("Keys", keys), screens.HelpSection("Mouse", mouse)]))

    # -- quick start -----------------------------------------------------------------------------

    def previews(self) -> list[tuple[tunepresets.Preset, str, list[Text]]]:
        """What each preset would choose, on a throwaway copy: the drafts are restored after each."""
        out = []
        for preset in tunepresets.PRESETS:
            snap = self.state.snapshot()
            try:
                self.state.apply_preset(preset)
                lines = []
                for role in modelconf.ROLES:
                    d = self.state.drafts.get(role)
                    if d is None or not d.model:
                        continue
                    word = fit_word(self.state.fit(role))
                    line = Text(f"{role:<7}", style="dim")
                    line.append(clean(model_label(self.state, d.source, d.model)))
                    line.append(f"  {word}", style="dim")
                    lines.append(line)
            finally:
                self.state.restore(snap)
            title = f"{preset_label(preset)}  ({tunepresets.describe(preset, self.state.real_hw)})"
            out.append((preset, title, lines or [Text("no discovered model fits", style="dim")]))
        return out

    def quick_start_done(self, key: str | None) -> None:
        """A card applies its preset; Adjust by hand stays; Skip leaves."""
        if key is None:
            self.exit(EXIT_OK)
        elif key:
            self.take_preset(key)
            toast(self, "Review the roles, then Ctrl+S to save.", title="Preset applied")


def run_app(state: tunestate.TuneSession, look: Look) -> tuple[int, TuneApp]:
    """Runs the app; returns its exit status and the app (for what it wrote)."""
    app = TuneApp(state, look)
    status = app.run(mouse=look.mouse)
    return (status if isinstance(status, int) else EXIT_OK), app
