# Hal code: a tour of the terminal UI

`hal code [DIR]` and `hal chat` open a full-screen terminal UI. You can drive it with the keys or
the mouse. The agent always runs in a box, and the UI talks to it over ACP. Where the full screen
cannot run (no terminal, `TERM=dumb`, `[ui].interface = "line"`), the line UI opens instead. It
prints into your scrollback; see [The line UI](#the-line-ui).

How this page was checked: the screens below are copied from the snapshot goldens in
`packages/hal-core/tests/data/tui_snapshots/`, and the behaviour is covered by
`packages/hal-core/tests/test_tui*.py` with a scripted agent. A live `hal chat` was started on
2026-11-03 to check that it opens. A full turn against a live model has not been checked: run one
with a 12B to 32B model before you rely on the speed figures.

A finished turn at 80x24, in the ASCII look (`turn-80x24-dark-ascii.snap`):

```text
 o  code  work    gemma-3-27b-it v | default v | default v       ####------ 40%
 | > fix the add test
 | ~ thought for <1s >
 | plan  ###-----  1 of 3
 |   [x] Read the failing test
 |   [>] Fix add()
 |   [ ] Run pytest
 | * ran 3 tools  read calc.py, test_calc.py | grep add(         0.0s    ok  >
 | * Edit   src/calc.py  +1 -1                                   0.0s    ok  >
 |      1   def add(a, b):
 |      2 -     return a - b
 |      2 +     return a + b
 | * Run    pytest -q                                            0.0s    ok  >
 | Here is the fix:
 |   python                                                               copy
 |   def add(a, b):
 |       return a + b
 | The tests pass now.
 | 1 file +1 -1 | ~70 tok | 0.0s
 |

--------------------------------------------------------------------------------
 > Message Hal   / commands  @ files  Enter sends  Alt+Enter new line
 box runsc                                           ^P cmds  ^B panel  F1 help
```

With a UTF-8 terminal the marks are Unicode. Set `[ui].unicode = false` to force ASCII.

## The welcome

The transcript opens with a welcome. It shows:

- the HAL eye (one row below 30 screen rows);
- the model, the box and the workspace;
- your most recent sessions of this project: three, or five on a tall screen. A click or Enter
  resumes one. The title is the only preview a session keeps;
- a tip that changes daily.

The welcome scrolls away like the rest of the transcript. The line UI keeps its boxed header.

## The top bar

The top bar is one row.

- **The HAL eye** is the red dot at the left. It is dim while Hal is idle, pulses slowly while a
  turn runs, and blinks amber while an approval waits. In the ASCII look it is `o`, `O` or `!`.
- **The surface and the project**, then `copy` when edits go to an isolated copy.
- **The model, the mode (`hal code` only) and the effort.** A click on one opens its picker. For
  effort, the picker is a row of the levels the model supports: Left, Right and Enter, or a
  click; Esc closes it.
- **The mouse wheel over effort** steps the level while no turn runs, and sends it once the wheel
  stops. The wheel over the model or the mode does nothing, so a stray scroll never swaps the
  model or lets edits pass without asking. There is no temperature control: the session cannot
  set one.
- **The context gauge** at the right edge. A click opens the same split `/context` prints (input
  side, model output kept, free, lockdown) with a Compact button that runs `/compact`. The button
  is disabled while a turn runs.

When the bar runs short of room it drops detail in steps: the project becomes its folder name,
the `effort` label goes (as in the screen above), the model name is cut with an ellipsis and the
gauge gets shorter. Model, mode, effort and the gauge always stay clickable.

## The status line

The status line at the bottom shows the box, lockdown, branch and unaccepted changes, and a
sparkline of the last turn's tokens per second. Key hints sit at the right: `^P commands  ^B
panel  F1 help`, or how to stop while a turn runs. F1 lists every key. Lockdown and paranoia are
read only here; change them on the host with `hal config set`.

## Turns

Each message starts a turn: your message after a `>` mark, then everything Hal did for it. A thin
rail on the left holds the turn together. The rail is red while the turn runs, amber while an
approval waits, and grey once it is done. A message sent during a turn gets its own faint rail
and a `queued` tag until its turn starts.

### Tool rows

- **Each tool call is one row.** While it runs it shows a spinner and the time. Then it shows the
  verb, its target, the `+N -N` of an edit, the time taken and the result at the right.
- **A click on the row, or Enter on it, opens its output or diff.** A failed call opens by
  itself.
- **An edit shows its first six changed lines** under the row, with one line before and after
  each, and `> N more lines` when there is more. A click on the row or on that line shows the
  whole diff.
- **Three or more calls in a row that succeeded and changed nothing fold** into one row, such as
  `ran 3 tools  read a.py, b.py`, once the run ends. A click shows them. Edits and failures never
  fold. Ctrl+O opens or closes them all.
- **A click on a path only opens that row's diff.** Nothing the model wrote can open a file.

### Code blocks

A code block in a reply shows its language and `copy` on the row above it. A click on that row
copies the code only. A long line scrolls sideways instead of wrapping. Copying uses the
terminal's clipboard escape (OSC 52), so it also works over SSH where the terminal allows it.

### Message actions

Hovering one of your messages shows `copy edit again` at the right end of its first row, plus
`rewind` in `hal code` once the message has a checkpoint. A reply block shows `copy` (its
markdown). In `hal chat` both also show the local time (`18:42`) they appeared on screen.

| Action | What it does |
| --- | --- |
| `copy` | Copies the message, or the reply block's markdown |
| `edit` | Puts the message back in the box to change and send |
| `again` | Sends the same text as a new turn |
| `rewind` | Puts back the files from before that message; the conversation stays |

`edit` and `again` do not remove the earlier turn from the agent's history: neither is a
regenerate. A click elsewhere on a message, or Tab to it and Enter, offers the same actions in a
dialog.

## The side panel

The side panel is hidden on start at every width.

- Ctrl+B, or a click on the handle at the right edge of the transcript, opens and closes it. The
  handle shows when you hover it.
- Drag the handle to resize the panel: 26 to 60 columns, and at most half the screen. The width
  holds for the session.

It has no tabs, only three sections:

| Section | What it shows |
| --- | --- |
| PLAN | The todo list, with the same marks as the plan block in the turn |
| CHANGES | `hal code` only: the changed paths of the copy, with `diff`, `accept...` and `export`. Click a path to read that file's diff; Esc closes it |
| CONTEXT | The use bar and token counts |

## Input

The message box has no frame: a thin rule above it and the prompt mark `>` (red while it has
focus). It grows with each line up to 10 rows, then scrolls. Under it, a row of chips shows each
collapsed paste (`paste 42 lines x`) and each attachment waiting for the next message
(`image 1 x` after `/image`, `text 1 x` after a `!command`); a click on `x` removes it. Chips name
the kind, not the file: an attachment holds only its data.

| Key or prefix | What it does |
| --- | --- |
| Enter | Send; with the completion list open, take the highlighted entry first |
| Alt+Enter, Ctrl+J | New line in the message (Shift+Enter too where the terminal reports it) |
| Paste of 3+ lines | Shown as `[pasted N lines #K]`; Backspace removes the whole chip; the real text is sent, also when Up or Ctrl+R brings the message back in the same session; the history file keeps only the chip |
| Up, Down | History on the first or last line (kept in `tui-history` in the state dir, mode 0600) |
| Ctrl+R | Search earlier messages, newest first (letters in order match); Ctrl+R again moves to the next match, Enter puts it in the message box without sending |
| `/`, Tab | The command list with descriptions, then the fixed arguments; Tab takes the highlighted entry |
| `@path` | Attach that file of the workspace (up to `[ui].mention_max_bytes`; links are never followed); `@` opens the file list, matched fuzzily: `@clc` finds `src/calc.py`; on a terminal 100 columns or wider the first 12 lines of the highlighted file show beside it |
| `!cmd` | Run `cmd` in the box (`/work`, 120 s limit); its output joins your next message |
| `# note` | Append the note to `AGENTS.md` in the copy |
| Shift+Tab | Cycle the permission mode: default, auto-edit, plan |
| Esc | Close the completion list; else stop the running turn |
| Ctrl+C | Clear the message; else stop the turn and deny an open approval; else press twice within 2 s to quit |
| Ctrl+D, Ctrl+Q | Quit (Ctrl+D on an empty message); asks first when the copy has unaccepted changes |
| Ctrl+O | Expand every tool card and thinking block when any is folded, else fold them all |
| Ctrl+B, Ctrl+T | Show or hide the side panel; open it scrolled to PLAN |
| Ctrl+P | One search over commands, stored sessions of this project, files of the copy, models (once `/model` has listed them) and settings (theme, mode, effort, animations for this session, side panel, expand all); Enter runs a command or resumes a session, a file goes into the message box as `@path` |
| Ctrl+L | Clear the transcript view (the session keeps its history) |
| PgUp, PgDn | Scroll the transcript; while you read further up, a `N new below` line counts new output, and a click on it (or scrolling to the bottom) follows the output again |
| F1, `/help` | Help: keys, commands, prefixes and mouse |

A message typed while a turn runs is queued and sent when the turn ends; the working line counts
the queued messages. `hal chat` has no mode chip, no Changes tab and none of `@`, `!`, `#`.

The mouse: click chips, cards, buttons, messages and completion entries; the wheel scrolls.
To select text with the mouse, hold Shift while you drag (Windows Terminal, GNOME Terminal,
kitty and most others), or set `[ui].mouse = false` to leave the mouse to the terminal.

When you leave, the full screen prints one line: the session id and the command that reopens it.
`/export` and `hal sessions export` write the whole transcript.

## What streams

- The answer is rendered as markdown while it arrives; finished blocks stay put, the open block
  grows at the bottom, with a working line (time, tokens, tok/s, "Esc stops"). Tokens are
  estimated at 4 characters each until the server reports usage.
- Code blocks are syntax highlighted. Links print as `text (url)`, never as hidden hyperlinks.
- Reasoning from thinking models is folded to one dim line: `thought for 4s` in the full screen
  (the token estimate shows once it is open), `thought for ~N tokens` in the line UI. A click opens
  it in place, and `[ui].show_thinking = true` starts it open.
- In the line UI each tool call is a card: title, then status and duration when it ends. In the
  full screen it is a row (see [Tool rows](#tool-rows)). Output is folded to
  `[ui].tool_output_lines` lines. Edits show a numbered, coloured unified diff, cut at
  `[ui].diff_max_lines`.
- Subagent calls are indented under their parent. In the full screen a subagent is one row:
  name, task and time, with a spinner while it runs and `ok` and its time when done. Its last
  three tool calls show below it, with `+N more` above them.
- The todo list is a checklist. In the full screen it is one `plan` block per turn with a
  progress bar and `1 of 3`, updated in place as items finish. In the line UI it is a `Todo`
  panel each time it changes.
- The line after each turn shows files changed, `+/-` lines, tokens, time and tok/s, and the stop
  reason in words when the turn did not finish normally (`cancelled`, `stopped: hit the output
  limit`). `UNSANDBOXED` is added to it, to the status bar and to a red banner when the session
  runs without a box. The line UI shows the raw reason and mode: `[end_turn] mode default`.
- A long turn rings the bell when it ends while the terminal is in the background, and a turn
  of a minute or more always does, for terminals that do not report focus
  (`[ui].notify = "bell"`; `"osc9"` sends a desktop notification instead; `"off"` silences it).

## Approvals

In the full screen a broker approval opens a dialog over everything else. At 80x24 it looks like
this (`approval-80x24-dark-ascii.snap`, with the border drawn in ASCII here):

```text
+----------------------------------------------------------------------+
| Approval needed: leaves the box                                      |
| shell.run [exec] from hal-tools   key 3f9a0c12d4e8                   |
| ! classifier: high - writes outside the workspace                    |
| why: exec tier asks                                                  |
| Arguments (every one, exactly as the tool receives them)             |
| +------------------------------------------------------------------+ |
| | {"command": "rm -rf build/", "cwd": "/work"}                     | |
| +------------------------------------------------------------------+ |
| every argument shown                                                 |
|                                Deny  n                               |
|                         Deny with a note  e                          |
|                            Allow once  y                             |
|             Allow these exact arguments this session  a              |
| y or a selects an allow button; Enter or a click confirms. Enter     |
| alone denies.                                                        |
+----------------------------------------------------------------------+
```

With two or more requests waiting, the title ends with `1 of 2`. The screen behind the dialog dims, the focused Deny is filled red and a focused allow button is
filled amber. After you answer, one row in the turn records it, for example
`x denied  shell.run [exec] from hal-tools  key 3f9a0c12d4e8` (also `denied with a note`,
`allowed once`, `allowed this session` and `answered elsewhere`). The line UI prints what it
printed before.

- Deny has the focus, so Enter alone denies; Esc and Ctrl+C deny too.
- Allowing takes two steps: `y` or `a` moves the focus to that button, then Enter presses it;
  with the mouse it is a full click on the button. Typing cannot allow: a `y` or `a` in the
  middle of typing is ignored, and any other key sends the focus back to Deny, so a sentence and
  Enter typed into the dialog by mistake deny.
- The allow buttons stay off for a moment after the dialog opens, so a key you were typing in
  the message box cannot allow anything, and they stay off until you have scrolled to the end
  of the arguments (PgDn, End or the wheel).
- "Allow these exact arguments this session" appears only when the request offers it.
- On a small terminal (down to 60x20) the buttons stack as one-line buttons and the arguments
  view gets shorter, so every button stays on screen; you still scroll it to the end to allow.
- `e` (or the button) opens a line for a note: Enter denies and sends the note to the agent as
  your next message.
- Requests queue and are answered one at a time; the status bar says `approval waiting` and the
  bell rings once.

In the line UI a broker approval is a rounded panel: tool, tier, pack, the label `leaves the box` and the first
12 hex digits of the approval key, the classifier's verdict when the hub sent one, the reason,
every argument exactly as the tool receives it, and the keys.

| Key | Effect |
| --- | --- |
| `y` | Allow once |
| `a` | Allow these exact arguments this session (only when the request offers it) |
| `n`, Enter | Deny |
| `e` | Deny with a note: type a note that is sent to the agent as your next message |

Anything else, and end of input, denies. The text is made by the host and escaped again here, so
the model cannot draw a fake panel. `hal approve ID` in another terminal also works; the panel
then closes with `approval answered elsewhere` and stops reading. Two requests are shown one
after the other.

## Slash commands

`/help` shows the table. Built in: `/model` (models of your sources, by source), `/effort`, `/mode`,
`/compact`, `/clear`, `/resume` (numbered picker), `/fork`, `/rewind`, `/diff`,
`/accept` (alias `/apply`: shows the diff report, asks `[y/N]`, then applies exactly what you
reviewed), `/ide`, `/export [FILE]`, `/cost`, `/context`, `/todo`, `/jobs`, `/init`, `/image FILE` (the workspace only; `--outside` allows another path and prints a warning),
`/theme dark|light|mono`, `/lockdown` and `/paranoia` (read only: change them on the host with
`hal config set`), `/quit`. Your own commands are files `.hal/commands/NAME.md` in the project;
`$ARGUMENTS` is replaced by what you type after the name.

`hal chat` has no project copy, so it leaves out `/rewind`, `/diff`, `/accept`, `/export`, `/ide`
and `/init`, and `!`, `#` and `@` are plain text there. `/help` and Tab show only what works.

### Model and effort

`hal code` asks the gateway for `hal-code`, the `code` role, so it runs the model, context and
effort `hal tune` set for that role (an empty `code` role means `chat`'s). `hal chat` asks for
`hal-chat`. The box reaches the gateway through a session token of its own that the gateway
refuses for remote backends, so a box cannot send your code to a cloud model. If the gateway is
not running, `hal code` warns and uses `[hal] code_model` (else `chat_model`) at `ollama_url`
directly, without the tuned context. `--model NAME` (or `HAL_AGENT_MODEL`) picks another name,
such as a named role `hal-code-128k`; `--upstream URL` (or `HAL_CODE_UPSTREAM`) skips the gateway.

`/model` lists the models Hal finds in your sources (see [models.md](models.md)), grouped by source,
with the current one marked and a `thinks` tag on models that support an effort level.

```text
/model                 # the list
/model 2               # switch to the second model for this session, from the next turn
/model qwen3:8b        # or by name, or SOURCE/NAME, or a part of the name
/model save            # write the current model as the code role in your config
```

Switching changes only this session. `/model save` uses the same pin-and-wire path as
`hal models assign code SOURCE MODEL`, and also saves the effort you set. The gateway reads its config
at start, so restart it before a saved model from a new source can be served. A model the gateway
cannot serve yet (a GGUF file no role holds, an Anthropic model) is listed with the reason and is not
selectable.

`/effort` sets how much the model thinks: `off`, `low`, `medium`, `high` or `max` (`default` clears it).
Without an argument it shows the current level and the levels the model supports. For a model with
no thinking control, or one that always thinks, it says why and changes nothing. The level appears in the
status line next to the model, is sent with every request, and is dropped when you switch to a model
that cannot use it. When the `code` role has an `effort`, `hal code` starts with it. Tab completes the
levels. How a level maps to a model is in [models.md](models.md#effort).

In `plan` mode, when a turn ends with a todo list, the UI shows the plan and asks
`Run this plan?`. In the full screen the buttons are Keep planning (focused, so Enter keeps it),
Run in default mode and Run in auto-edit. In the line UI `d` or `a` leaves plan mode for
`default` or `auto-edit`; anything else keeps it. Only your choice leaves plan mode.

### Accepting and leaving

`/accept` (or `accept...` in the side panel) shows the diff report and the exact patch that will be
applied, scrollable, with Cancel focused. A checkbox to apply high-risk changes too appears only
when the report flags some. Apply writes exactly the patch you saw: if the copy changed in the
meantime, nothing is applied.

Leaving `hal code` with unaccepted changes asks first: Keep for later (focused; `hal code accept
SID` applies them later), Review and accept, Discard the copy (asks again), or Cancel. Esc and
Ctrl+C mean Cancel: you stay in the session. Without changes it leaves at once.

## The line UI

The line UI prints into your normal scrollback, so you can scroll, select and search what it
printed. It runs when the full screen cannot (no terminal, `TERM=dumb`, a failed start, which
prints `full-screen UI unavailable: REASON; using the line UI`) or when you ask for it with
`[ui].interface = "line"` or `HAL_TUI=line`.

| Key or prefix | What it does |
| --- | --- |
| Enter | Send |
| Alt-Enter, Ctrl-J | New line in the message |
| Paste of 3+ lines | Shown as `[pasted N lines #K]`; the real text is sent |
| Up, Ctrl-R | History and history search |
| Tab | Complete `/commands` (with descriptions) and `@files` |
| Shift-Tab | Cycle the permission mode |
| Esc | Interrupt the turn (Ctrl-C does too) |
| Esc Esc | Open `/rewind` (`hal code` only) |
| Ctrl-O | Show folded tool output and the last thinking block in full |
| Ctrl-T | Show or hide the todo panel |
| Ctrl-C | Clear the line; on an empty line, leave |
| Ctrl-D | Leave |

## Settings (`[ui]`)

| Key | Default | Meaning |
| --- | --- | --- |
| `unicode` | `true` | Unicode glyphs when the terminal is UTF-8; ASCII otherwise or when false |
| `theme` | `"dark"` | `dark`, `light` or `mono`; `NO_COLOR` also removes colour |
| `notify` | `"bell"` | `off`, `bell` or `osc9` |
| `show_thinking` | `false` | Print reasoning text, dimmed |
| `tool_output_lines` | `8` | Lines of tool output before folding |
| `diff_max_lines` | `80` | Diff lines shown inline |
| `markdown_refresh_hz` | `8` | Redraws per second of the streaming tail |
| `mention_max_bytes` | `32768` | Largest `@file` that is attached |
| `interface` | `"auto"` | `auto`, `full` or `line`: the full-screen UI where the terminal can run it, or force one (`HAL_TUI` overrides) |
| `animations` | `true` | Motion in the full-screen UI (spinners, the HAL eye, gauges easing); `false` draws static glyphs and runs no animation timer. Elapsed times still count up |
| `mouse` | `true` | Clicks and drags in the full-screen UI; `false` leaves the mouse to the terminal |

## When it does not work

| Symptom | Fix |
| --- | --- |
| `full-screen UI unavailable: ...; using the line UI` | The terminal or the start failed; the line UI runs the same session. Check `TERM`, or set `HAL_TUI=line` to skip the attempt |
| Clicks do nothing | The terminal does not send mouse events, or `[ui].mouse = false`; every action also has a key (F1 lists them) |
| Cannot select text with the mouse | Hold Shift while dragging, or set `[ui].mouse = false` |
| Shift+Enter sends instead of adding a line | Your terminal does not report Shift+Enter (Windows Terminal does not); use Alt+Enter or Ctrl+J |
| The allow buttons stay grey | Scroll the arguments to the end; they also wait a moment after the dialog opens |
| Ctrl+C did not quit | One Ctrl+C on an empty message only warns; press it again within 2 s, or Ctrl+D |
| Box lines show as `?` or letters | The terminal is not UTF-8: it falls back to ASCII by itself; set `[ui].unicode = false` to be sure |
| No colour | `NO_COLOR` is set or `[ui].theme = "mono"` |
| `!cmd` says it needs the docker box | The session has backend `none` or `bwrap`; `!` only runs through `docker exec` |
| `@file` is not attached | It is over `[ui].mention_max_bytes`, not UTF-8, a symlink, or outside the workspace |
| Esc does nothing during a turn (line UI) | stdin is not a terminal; use Ctrl-C |
| `/rewind` says it works in `hal code` | `hal chat` boxes have no work tree; use `hal code` |
| `/model` shows no list | No source is configured or none answered (`hal models list`); `/model NAME` still works |
| `/effort` says not available | The model has no thinking control; pick one marked `thinks` in `/model` |
| `/model` says a model cannot be used yet | Run `/model save` (after choosing a served model), or `hal models assign code SOURCE MODEL` |
