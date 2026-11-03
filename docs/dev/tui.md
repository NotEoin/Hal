# Terminal UI developer notes

How the terminal UIs of `hal code`, `hal chat` and `hal tune` are built and how to change them.
User docs: [hal-code.md](../user/hal-code.md), [tui.md](../user/tui.md) and
[models.md](../user/models.md#the-tune-screen).

## Modules

| Module | What it holds |
| --- | --- |
| `halcore/tui/controller.py` | The turn loop (`Controller`) and the `Ui` protocol both UIs implement |
| `halcore/tui/app.py` | The line UI (`LineUi`, prompt_toolkit) and `run_tui` |
| `halcore/tui/render.py` | `Renderer` (updates to safe renderables) and `ConsoleSink` (scrollback) |
| `halcore/tui/slash.py` | Slash commands; a bare listing command returns a `PickRequest` |
| `halcore/tui/approval.py` | Approval panel text, `answer_to_option`, the line UI's asker |
| `halcore/tui/tokens.py` | Design tokens: colour roles per theme, glyph sets (Unicode and ASCII), spacing; no Textual or rich import |
| `halcore/tui/motion.py` | Easing helpers and `Ticker`, the one frame clock per app (no Textual import) |
| `halcore/tui/theme.py` | `[ui]` settings and the `Glyphs`, `Palette`, prompt_toolkit colours and Textual themes built from `tokens.py` |
| `halcore/tui/widgets.py` | Shared Textual widgets: `SafeStatic`, `Slider` (with `ValueEntry` to type a value), `Segmented`, `FitGauge`, `ToolCard`, `DiffView`, `ThinkingFold` |
| `halcore/tui/screens.py` | Shared modals: `PickerModal`, `ConfirmModal`, `ChoiceModal`, `HelpScreen`, and `ask()` |
| `halcore/tui/approvalmodal.py` | The approval gate of the full screen (`ApprovalModal`) and `ModalAsker`, its bridge sender |
| `halcore/tui/agentapp.py` | The full-screen `hal code` and `hal chat`: `AgentApp`, `TextualUi`, `TextualSink`, `Composer` |
| `halcore/tui/fallback.py` | `can_run_textual`, `StartFailed`, `reason_of` and the notice printed before the line UI takes over |
| `halcore/tuneapp.py` | The full-screen `hal tune` (`TuneApp`, `QuickStart`, `SourceModal`) |
| `halcore/tuneview.py` | What both tune screens show, as plain text |

## How the two UIs share one loop

`Controller` (in `controller.py`) owns the ACP client, the session state and the approval bridge.
Its `run(ui)` reads each message with `ui.read_message()`, runs the slash command, prefix or turn,
and asks the `Ui` every question: `confirm`, `choose_plan` (only `PLAN_CHOICES` or None leave plan
mode), and `approval_sender` (the `send` that `PermissionBridge.run` calls). `LineUi` reads from
prompt_toolkit and prints to scrollback; `TextualUi` reads from `AgentApp.inbox` (filled by the
composer, pickers and buttons) and answers with modals. The full screen runs the controller in a
worker and puts the `QUIT` item in the inbox to end it.

Updates from the agent arrive on the ACP reader task. `TextualSink` hands each one to
`App.call_later`, so widgets are mounted on the app's loop, in order, with the app's context.

## Rendering safety

Model, tool and config text reaches the screen only as `rich.text.Text` built from `clean()`
output (terminal controls stripped, bidirectional and zero-width characters shown as `\uXXXX`).

- Never pass such text to Textual as markup. Textual markup can carry `[@click=app.quit]`, so a
  model could add clickable actions. Use `SafeStatic` (markup off, `str` cleaned to `Text`),
  `OptionList(markup=False)` with `Text` prompts, and fixed strings for button labels and ids.
- No Textual `Markdown` widget: its links are clickable and can open URLs. The transcript keeps
  rich's `Markdown(hyperlinks=False)`, which prints links as `text (url)`.
- `Renderer` cleans before any sink, so the display tests in `test_tui.py` and
  `test_tui_render.py` cover both UIs; `test_tui_widgets.py` feeds markup, click actions and
  escapes through `SafeStatic`.

## Slider maths

`Slider` maps a cell to a value with `value_at(x, track, low, high, step, log=...)` and back with
`position_of`. A log track suits context lengths (halfway from 8K to 128K is 32K); a linear one
suits temperature. Key steps move one `step` on a linear track and one cell on a log track.
`Slider.Changed.final` is true on mouse release and on key steps, so costly listeners (a config
draft) act once, cheap ones (a gauge) on every move. The wheel steps a slider only while it has
focus, so a scroll over a page of sliders never edits one.

## Start failures

Textual does not raise an error that happens inside an app: it prints the traceback when it
exits and returns None with `App.return_code` set to 1. `agentapp.run_full` and
`tune.run_screen` check that code: before the app's `on_mount` (`mounted` is False) it is a
`StartFailed` and the line UI opens on the same session; after it, the exit status is 1.

## Small terminals

Every screen is checked at 80x24 and 60x20 in the Pilot tests. The header chips shorten in steps
(`AgentApp.fit_header`), `Segmented` drops the padding around its labels when its width is short,
dialogs size their scrolling part in `vh` or by measuring the rest of the box (`ApprovalModal.fit`),
and the approval buttons stack as one-line buttons when they do not fit side by side.

## Writing Pilot tests

Run apps with `App.run_test(size=(100, 30))` inside `asyncio.run`, no network, no GPU, and
`HAL_CONFIG_DIR` or `TempHalHome` for config. `pilot.mouse_down`, `pilot.hover` and
`pilot.mouse_up` drag a slider (the hover may leave the widget: the slider captures the mouse);
offsets must stay inside the screen. Modals are tested by pushing them on a bare `App` and
collecting the dismiss result. Verified against Textual 8.2.8.

## Colour roles

`tokens.ROLES` holds eleven roles per theme: `bg`, `surface`, `panel`, `fg`, `muted`, `faint`,
`accent`, `accent_dim`, `ok`, `warn`, `bad`. Textual themes expose each as a CSS variable
(`$hal-faint`, `$hal-accent-dim`, ...). Hal CSS uses only `$hal-*`, `$primary`, `$warning`,
`$success`, `$error` and `$background`, never Textual's blended `$text-muted` or `$boost`, so
the snapshot role map stays exact. `test_tui_tokens.py` computes WCAG contrast: text roles at
least 4.5:1 on `bg`, `faint` at least 3:1.

## Glyphs

`tokens.UNICODE_GLYPHS` and `tokens.ASCII_GLYPHS` have the same keys; `Glyphs` in `theme.py`
gets one set or the other. Source files write glyphs as `\u` escapes. Helpers: `tokens.bar`
(block bar), `tokens.spark` (sparkline scaled to the window's maximum) and `tokens.spinner_frame`.
`test_tui_tokens.py` checks that the ASCII set is pure ASCII and each Unicode glyph is one cell.

## Motion

`motion.Ticker` is the app's only animation clock. Widgets call `subscribe(callback, hz)` for
motion (spinners, the eye), `tween(start, end, seconds, on_value)` to ease a value, and
`every(seconds, callback)` for numbers that change on their own (elapsed time, tok/s samples).
The single Textual timer is created on the first subscription, runs at
`min(FPS_MAX, fastest subscriber)` and is paused when the last subscription ends, so an idle app
has no running timer. With `[ui].animations = false`, `subscribe` starts nothing and `tween`
jumps to its end value; `every` still runs. A callback returns False to end its subscription; one
that raises is logged and dropped. Three frames in a row over budget halve the frame cap.

## Snapshot tests

`packages/hal-core/tests/test_tui_snapshots.py` renders each screen at 80x24 and 120x36, dark and
light, and compares it with a golden in `packages/hal-core/tests/data/tui_snapshots`. A golden is
an ASCII fingerprint (`tuisnap.py`): the screen text with non-ASCII written as `\uXXXX`, then one
colour-role letter per cell, run-length coded. Textual's SVG export is not used as the golden: it
is 40 to 100 KB per screen, changes on every Textual upgrade and is not ASCII. Each snapshot is
also checked, golden or not, for widgets inside the screen, one-row widgets one row high and no
label broken across two rows (`assert_unbroken`). `render.clock` is frozen so timings are stable.

```sh
HAL_SNAPSHOT_UPDATE=1 .venv/bin/python -m pytest -q packages/hal-core/tests/test_tui_snapshots.py  # rewrite goldens
HAL_SNAPSHOT_DIR=~/snap/chromium/common/halshots .venv/bin/python -m pytest -q packages/hal-core/tests/test_tui_snapshots.py  # failures also write .svg there
```

Read the `.snap` diff of a visual change in review. A golden made with another Textual version
fails with "regenerate them".
