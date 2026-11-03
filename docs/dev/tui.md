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
| `halcore/tui/status.py` | The status line's content (`StatusInfo`, `segments`) for both UIs |
| `halcore/tui/choices.py` | The models `/model` offers, under the name the gateway serves them |
| `halcore/tui/mentions.py` | `@path` mentions: the workspace index, matching, the preview and attaching files |
| `halcore/tui/prompt.py` | The line UI's input editor (`PromptInput`, prompt_toolkit) |
| `halcore/tui/keys.py` | The line UI's keys during a turn (`KeyWatcher`: Esc, Ctrl-O, Ctrl-T, focus) |
| `halcore/tui/tokens.py` | Design tokens: colour roles per theme, glyph sets (Unicode and ASCII), spacing; no Textual or rich import |
| `halcore/tui/fuzzy.py` | `score` (subsequence match with highlight indices) and `rank`, used by pickers and `@` completion (no Textual import) |
| `halcore/tui/motion.py` | Easing helpers and `Ticker`, the one frame clock per app (no Textual import) |
| `halcore/tui/theme.py` | `[ui]` settings and the `Glyphs`, `Palette`, prompt_toolkit colours and Textual themes built from `tokens.py` |
| `halcore/tui/widgets.py` | Shared Textual widgets: `SafeStatic`, `Slider` (with `ValueEntry` to type a value), `Segmented`, `FitGauge`, `ToolCard`, `DiffView`, `ThinkingFold`, and `row_tail`, which keeps the time and result columns of every row aligned |
| `halcore/tui/chrome.py` | The bars around the transcript: `HalEye`, `TopBar` (fitted by `fit_top`), `StatusLine`, `SideHandle` and `ChangesList` |
| `halcore/tui/screens.py` | Shared modals: `PickerModal` (fuzzy ranked, matched letters bold), `ConfirmModal`, `ChoiceModal`, `HelpScreen`, `DiffScreen`, `EffortPopover`, `ContextPopover`, and `ask()` |
| `halcore/tui/approvalmodal.py` | The approval gate of the full screen (`ApprovalModal`) and `ModalAsker`, its bridge sender |
| `halcore/tui/agentapp.py` | The full-screen `hal code` and `hal chat`: `AgentApp`, `TextualUi`, `Composer` |
| `halcore/tui/transcript.py` | The conversation view: `Transcript`, `UserMessage`, `AssistantBlock`, `CodeBlock`, `ToolGroup`, `PlanBlock`, `SubagentTree` and `TextualSink` (the `render.Sink` that mounts into it) |
| `halcore/tui/palette.py` | The Ctrl+P rows (`PaletteItem`) from commands, sessions, files, models and settings (no Textual import) |
| `halcore/tui/welcome.py` | `WelcomeView`: eye art, facts, recent sessions (`RecentRow`) and `TIPS` |
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

The transcript holds one `TurnView` per message. `AgentApp.send` mounts a queued turn; when
`TextualUi.read_message` takes that message from the inbox, `Transcript.activate_next` makes its
turn the active one, so output (also of slash commands) lands in the turn of the message that
caused it, even when messages were queued. The rail is the Textual `solid` border (U+2502 in
Textual 8.2.8), `ascii` in the ASCII look.
`TextualSink` passes each block's kind (`prose`, `code`, `tools`, `thought`, `plan`, `end`) to
`Transcript.add`; a block whose kind differs from the one before it gets the `gap` class (one
blank row above). The app sets `compact` on the transcript below 90 columns or 30 rows, which
drops those rows so a turn fits a small terminal.
`Renderer` sends the todo list to `Sink.plan` as `(status, text)` pairs (cleaned, clipped, an
unknown status read as `pending`) when the sink has it; `TextualSink.plan` keeps one `PlanBlock`
per turn and updates it in place, the bar easing on the motion ticker. `ConsoleSink` has no
`plan`, so the line UI writes the `Todo` panel as before.
`HoverActions` draws its fixed action words over the right end of a block's first row while
hovered (in `render_line`, so hovering never reflows the text) and maps a click to a word by its
column. `UserMessage` sends `copy`, `edit`, `again` and `rewind` to `AgentApp.message_action`;
`AssistantBlock` (each prose block written as rich `Markdown`) copies its markdown source.
In `hal chat` a hover block also notes `stamp_now()` at mount and draws it after the actions;
the stamp is text, never matched as an action.
The side panel (`#side`, a `VerticalScroll` of headed sections) starts hidden at every width.
`SideHandle` (chrome.py) sits between the transcript and the panel, in the column the
transcript's right padding used to take: a click posts `Toggled`, a drag (mouse capture) posts
`Dragged(x)`, and the app sets the width through `tokens.side_width` (`SIDE_MIN..SIDE_MAX`, at
most `SIDE_SHARE` of the screen). The CHANGES list comes from `StatusInfo.changed`, filled with
`isolated.changes` off the loop by `refresh_status`; paths are cleaned before they are shown.
A click on effort in the top bar opens `screens.EffortPopover` (a `Segmented` of `default` plus
the levels `slash.effort_levels` returns) at the item's column; its answer runs `/effort LEVEL`.
`TopBar.Wheeled` is handled for effort only, and only while idle: each notch moves
`wheel_effort` (drawn in the bar at once) and restarts an `EFFORT_SETTLE_S` one-shot timer that
sends one `/effort`. Model and mode ignore the wheel on purpose (spec section 6).
Completions live in `#complete-box`: the `#popup` `OptionList` (rows built by `match_row`, typed
letters in the accent) and `#preview`, shown from `PREVIEW_MIN_W` columns. An `@` row's preview is
`mentions.preview` (`open_regular_beneath`, at most `PREVIEW_MAX_BYTES`, a NUL means binary) read
in a thread under `PREVIEW_TIMEOUT_S`, then `clean()`ed into a rich `Syntax`; a command with fixed
arguments shows them as a usage line, since the list itself cannot restyle only its highlighted row.
Ctrl+R opens `screens.HistorySearch`, a `PickerModal` subclass over `Composer.sent` (stored history
plus this session) newest first; its answer goes through `Composer.load`, never `send`.
`Controller.banner(ui)` lets a `ui` with a `welcome(state, session)` method draw its own welcome;
`TextualUi` mounts `welcome.WelcomeView` as the first transcript item and the line UI keeps the
`controller.welcome()` panel. Recent rows reuse `palette.sessions` (ids checked, titles cleaned)
in a thread, and only post `WelcomeView.Resume("/resume ID")`. The tip reads the module's `clock`,
which the snapshot tests pin.
Ctrl+P opens `screens.CommandPalette`. Its rows come from the pure `palette` module (commands,
`list_sessions` minus the current one, `mentions.index_files` in code, `state.choices` or one
`/model` row, settings), gathered in a thread under `PALETTE_TIMEOUT_S` once per open. An item only
names an action (`send`, `insert`, `compose`, `ui`) and a value built by this program or from
`clean()`ed disk text; the app performs it, so a session title cannot choose what runs.
The gauge opens `screens.ContextPopover` with `slash.context_breakdown(state)`, the same
`Breakdown` that `/context` formats, so the two cannot disagree. It lives in `slash`, not
`controller`, because `controller` imports `slash`. Compact answers `"compact"` and the app runs
`/compact`; the gauge eases in with `Ticker.tween` only when motion is on.
`ChangesList` (chrome.py) draws one unwrapped row per path and maps a click to its row by index,
so a path's text cannot pick what opens. `show_change` reads `session.diff(None)` in a thread,
cuts it with the pure `split_patch` (by `diff --git` headers, keyed by the new-side path) and
shows that part as `patch_text` (cleaned, `ok`/`bad` lines) in the read-only `DiffScreen`.
Child agent updates (`hal_subagent`) go to `Sink.subagent(sid, phase, name, task, tool,
elapsed)` when the sink has it; `TextualSink` keeps one `SubagentTree` per id, updated in place,
and stops the trees still running when the turn ends. Without it the renderer writes today's rows.
`Renderer` cuts each committed block with `split_fences` and, when the sink has `code`, sends
fenced code there and the prose around it through `write`. `ConsoleSink` has no `code`, so the
line UI prints one markdown block as before. `CodeBlock` shows only a sanitised language label
(`[A-Za-z0-9+#._-]`, 16 cells) and copies the cleaned source, never the label.

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

Every screen is checked at 80x24 and 60x20 in the Pilot tests. The top bar fits itself in steps
(`chrome.fit_top`, tested at every width from 40 to 200), `Segmented` drops the padding around its labels when its width is short,
dialogs size their scrolling part in `vh` or by measuring the rest of the box (`ApprovalModal.fit`),
and the approval buttons stack as one-line buttons when they do not fit side by side.

Dialogs share `screens.MODAL_CSS`: a `round $panel` box on a `$background 70%` backdrop, and flat
one-row buttons (no border, one column of line padding a side, so `BUTTON_CHROME_COLS` is 3) with
the focused one filled in the accent. Only the approval box and the plan card
(`ChoiceModal(attention=True)`) keep the heavy accent border. The CSS uses Textual's own `$panel`
and `$text-disabled`, not `$hal-*`, because the modal tests run on a plain `App`.
`ApprovalModal` keeps its in-box `.modal-title` (with `N of M`) instead of a border title, because
`test_tui_approval_modal.py` is the unchanged contract and reads it there. `ModalAsker` calls
`on_answer(letter, view)` after `bridge.consume` (and for `gone`), and logs a failing callback
instead of raising; `TextualUi.approval_answered` mounts `approvalmodal.record_text` as a
`.approval-record` row of the active turn.

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
