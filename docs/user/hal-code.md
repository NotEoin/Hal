# Hal code: a tour of the terminal UI

`hal code [DIR]` and `hal chat` open a full-screen terminal UI you can drive with the keys or the
mouse. The agent always runs in a box; the UI talks to it over ACP. Where the full screen cannot
run (no terminal, `TERM=dumb`, `[ui].interface = "line"`), the line UI opens instead: it prints
into your scrollback and is described in [The line UI](#the-line-ui). Verified by the tests in
`packages/hal-core/tests/test_tui*.py` with a scripted agent. Not yet run against a live model:
check a real session with a 12B to 32B model before relying on the speed figures.

```text
 o  hal code  ~/proj  copy    qwen3-coder-30b v | default v | effort default v                         ####------ 38%
 > fix the failing test in @tests/test_parse.py                          | Todo  Changes  Context
 Looking at the test first.                                              | 1 changed path in the copy
   * Read tests/test_parse.py  ok  0.0s                                  | [Diff] [Accept...] [Export]
   * Edit src/parse.py  ok  0.1s  src/parse.py +2 -1                     |
          41   if not text:                                              |
          42 -     return None                                           |
          42 +     raise ValueError("empty input")                       |
   | Run pytest -q  running 3s                                           |
 / working 12s | ~210 tok | 36 tok/s | Esc stops
 -------------------------------------------------------------------------------------------------------------
 > Message Hal   / commands  @ files  Enter sends  Alt+Enter new line
 box runsc | lockdown standard/balanced | main | 1 unaccepted | .:-=+*#= 36 tok/s      Esc stops  ^C stop  F1 help
```

The top bar is one row. The red dot at its left is the HAL eye: dim while Hal is idle, pulsing
slowly while a turn runs, blinking amber while an approval waits (the status line says "approval
waiting" too; in the ASCII look the dot is `o`, `O` or `!`). Then come the surface, the project and
`copy` when edits go to an isolated copy, then the model, the mode (`hal code` only) and the effort:
a click on one opens its picker. The context gauge at the right edge runs `/context`. On a narrow
terminal the project becomes its folder name, the model name is cut with an ellipsis and the gauge
gets shorter, so model, mode, effort and the gauge always stay clickable. Lockdown and paranoia
are in the status line (change them on the host with `hal config set`). The status line at the
bottom shows the box, lockdown, branch and unaccepted changes, a sparkline of the last turn's
tokens per second, and key hints at the right (`^P commands  ^B panel  F1 help`, or how to stop
while a turn runs). F1 lists every key.

Each message starts a turn: your message after a `>` mark, then everything Hal did for it, held
together by a thin rail on the left. The rail is red while the turn runs, amber while an approval
waits, and grey once it is done; a message sent during a turn gets its own faint rail and a
`queued` tag until its turn starts. Each tool call is one row: a spinner and the time while it runs, then the verb, its target,
the `+N -N` of an edit, the time taken and the result at the right. A click on the row (or Enter
on it) opens its output or diff, and a failed call opens by itself. An edit also shows its first
six changed lines under the row, with a line before and after each, and `> N more lines` when
there is more; a click on the row or on that line shows the whole diff. A click on a path in the
transcript only opens that row's diff: nothing the model wrote can open a file. Three or more
calls in a row that succeeded and changed nothing fold into one `ran 3 tools  read a.py, b.py`
row once the run ends; a click shows them. Edits and failures never fold. Ctrl+O opens or closes them all.
A code block in a reply shows its language and `copy` on the row above it; a click on that row
copies the code only, and a long line scrolls sideways instead of wrapping. Hovering one of
your messages shows `copy edit again` at the right end of its first row, plus `rewind` in
`hal code` once the message has a checkpoint; a reply block shows `copy` (its markdown). `edit`
puts the message back in the box to change and send; `again` sends the same text as a new turn.
Neither removes the earlier turn from the agent's history: it is not a regenerate. `rewind`
puts back the files from before that message (the conversation stays). A click elsewhere on a
message, or Tab to it and Enter, offers the same actions in a dialog. The side panel
(Ctrl+B; open from 110 columns and closed below, following resizes until you press Ctrl+B)
has the todo list, the changes of the copy with Diff, Accept and Export buttons, and the
context use. With a UTF-8 terminal the marks are Unicode;
set `[ui].unicode = false` to force ASCII.

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
| Ctrl+R | Pick an earlier message from a filterable list |
| `/`, Tab | The command list with descriptions, then the fixed arguments; Tab takes the highlighted entry |
| `@path` | Attach that file of the workspace (up to `[ui].mention_max_bytes`; links are never followed); `@` opens the file list |
| `!cmd` | Run `cmd` in the box (`/work`, 120 s limit); its output joins your next message |
| `# note` | Append the note to `AGENTS.md` in the copy |
| Shift+Tab | Cycle the permission mode: default, auto-edit, plan |
| Esc | Close the completion list; else stop the running turn |
| Ctrl+C | Clear the message; else stop the turn and deny an open approval; else press twice within 2 s to quit |
| Ctrl+D, Ctrl+Q | Quit (Ctrl+D on an empty message); asks first when the copy has unaccepted changes |
| Ctrl+O | Expand every tool card and thinking block when any is folded, else fold them all |
| Ctrl+B, Ctrl+T | Show or hide the side panel; open it on the todo list |
| Ctrl+P | Every command in a filterable list |
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
- Each tool call is a card: title, then status and duration when it ends. Output is folded to
  `[ui].tool_output_lines` lines. Edits show a numbered, coloured unified diff, cut at
  `[ui].diff_max_lines`. Subagent calls are indented under their parent. A subagent is one
  row in the full screen (name, task, time; a spinner while it runs, `ok` and its time when done)
  with its last three tool calls below it and `+N more` above them. The todo list is a
  checklist: in the full screen one `plan` block per turn with a progress bar and `1 of 3`,
  updated in place as items finish; in the line UI a `Todo` panel each time it changes.
- The line after each turn shows files changed, `+/-` lines, tokens, time and tok/s, and the stop
  reason in words when the turn did not finish normally (`cancelled`, `stopped: hit the output
  limit`). `UNSANDBOXED` is added to it, to the status bar and to a red banner when the session
  runs without a box. The line UI shows the raw reason and mode: `[end_turn] mode default`.
- A long turn rings the bell when it ends while the terminal is in the background, and a turn
  of a minute or more always does, for terminals that do not report focus
  (`[ui].notify = "bell"`; `"osc9"` sends a desktop notification instead; `"off"` silences it).

## Approvals

In the full screen a broker approval opens a dialog over everything else:

```text
+-- Approval needed: leaves the box ------------------------------- 1 of 2 --+
| shell.run [exec] from hal-tools   key 3f9a0c12d4e8                          |
| classifier: high - writes outside the workspace                             |
| why: tool tier exec needs approval under lockdown standard                  |
| Arguments (every one, exactly as the tool receives them)                    |
| {"command": "rm -rf build/", "cwd": "/work"}                                |
| [ Deny  n ] [ Deny with a note  e ] [ Allow once  y ] [ Allow these exact   |
|                                       arguments this session  a ]           |
+-----------------------------------------------------------------------------+
```

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

`/accept` (or Accept... in the side panel) shows the diff report and the exact patch that will be
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
