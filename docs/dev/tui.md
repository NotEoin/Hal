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
| `halcore/tui/theme.py` | `[ui]` settings, glyphs, colour tokens and the Textual themes |
| `halcore/tui/widgets.py` | Shared Textual widgets: `SafeStatic`, `Slider`, `Segmented`, `FitGauge`, `ToolCard`, `DiffView`, `ThinkingFold` |
| `halcore/tui/screens.py` | Shared modals: `PickerModal`, `ConfirmModal`, `ChoiceModal`, `HelpScreen`, and `ask()` |

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
draft) act once, cheap ones (a gauge) on every move.

## Writing Pilot tests

Run apps with `App.run_test(size=(100, 30))` inside `asyncio.run`, no network, no GPU, and
`HAL_CONFIG_DIR` or `TempHalHome` for config. `pilot.mouse_down`, `pilot.hover` and
`pilot.mouse_up` drag a slider (the hover may leave the widget: the slider captures the mouse);
offsets must stay inside the screen. Modals are tested by pushing them on a bare `App` and
collecting the dismiss result. Verified against Textual 8.2.8.
