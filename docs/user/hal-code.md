# Hal code: a tour of the terminal UI

`hal code [DIR]` and `hal chat` open a terminal UI that lives in your normal scrollback (no
alternate screen), so you can scroll, select and search what it printed. The agent always runs in
a box; the UI talks to it over ACP. Verified by the tests in `packages/hal-core/tests/test_tui*.py`
with a scripted agent. Not yet run against a live model: check a real session with a 12B to 32B
model before relying on the speed figures.

```
+------------------------------------------------------------------+
| Hal 0.1.0                                                        |
| qwen3-coder-30b | 32k context | lockdown standard | box runsc    |
| workspace ~/proj                                                 |
| copy ~/.local/state/hal/isolated/ab12/work                       |
+------------------------------------------------------------------+

> fix the failing test in @tests/test_parse.py

Looking at the test first.

* Read tests/test_parse.py  ok  0.0s
* Edit src/parse.py  ok  0.1s  src/parse.py +2 -1
       41   if not text:
       42 - return None
       42 + raise ValueError("empty input")
* Run pytest -q  ok  3.2s
    3 passed in 0.41s

[end_turn] mode default | 1 file +2 -1 | ~210 tok | 11.8s | 36 tok/s
> _
 qwen3-coder-30b | ####------ 38% | 36 tok/s | mode default | lockdown standard/balanced | 1 unaccepted
```

With a UTF-8 terminal the box lines are rounded and the marks are U+276F, U+25CF and U+2713 instead of `>`, `*` and `ok`;
set `[ui].unicode = false` to force the ASCII form shown above.

## Input

| Key or prefix | What it does |
| --- | --- |
| Enter | Send |
| Alt-Enter, Ctrl-J | New line in the message |
| Paste of 3+ lines | Shown as `[pasted N lines #K]`; the real text is sent |
| Up, Ctrl-R | History and history search (kept in `tui-history` in the state dir, mode 0600) |
| Tab | Complete `/commands` (with descriptions) and `@files` |
| `@path` | Attach that file of the workspace to the message (up to `[ui].mention_max_bytes`; links are never followed) |
| `!cmd` | Run `cmd` in the box (`/work`, 120 s limit); its output joins your next message |
| `# note` | Append the note to `AGENTS.md` in the copy |
| Shift-Tab | Cycle the permission mode: default, auto-edit, plan |
| Esc | Interrupt the turn (Ctrl-C does too) |
| Esc Esc | Open `/rewind` |
| Ctrl-O | Show folded tool output in full |
| Ctrl-T | Show or hide the todo panel |
| Ctrl-L | Clear the screen |
| Ctrl-D | Leave |

## What streams

- The answer is rendered as markdown while it arrives; finished blocks go to scrollback, the open
  block and a spinner line (time, tokens, tok/s, "Esc interrupts") stay at the bottom. Tokens are
  estimated at 4 characters each until the server reports usage.
- Code blocks are syntax highlighted. Links print as `text (url)`, never as hidden hyperlinks.
- Reasoning from thinking models is folded to one dim line, `thought for ~N tokens`; set
  `[ui].show_thinking = true` to see it.
- Each tool call is one line: mark, title, status, duration. Output is folded to
  `[ui].tool_output_lines` lines. Edits show a numbered, coloured unified diff, cut at
  `[ui].diff_max_lines`. Subagent calls are indented under their parent. The todo list is a
  checklist panel.
- The line after each turn shows the stop reason, mode, files changed, `+/-` lines, tokens, time
  and tok/s. `UNSANDBOXED` is added when the session runs without a box.
- A long turn rings the bell (`[ui].notify = "bell"`) or sends an OSC 9 notification (`"osc9"`)
  when it ends while the terminal is in the background (10 s or more, if the terminal reports
  focus) or after 60 s regardless. `"off"` silences it.

## Approvals

A broker approval is a rounded panel: tool, tier, pack, the classifier's verdict when the hub
sent one, the reason, every argument exactly as the tool receives it, and the keys.

| Key | Effect |
| --- | --- |
| `y` | Allow this exact call once |
| `a` | Allow for this session (only when the request offers it) |
| `n`, Enter | Deny |
| `e` | Deny, then type a note that is sent to the agent as your next message |

Anything else, and end of input, denies. The text is made by the host and escaped again here, so
the model cannot draw a fake panel. `hal approve ID` in another terminal also works.

## Slash commands

`/help` shows the table. Built in: `/model` (numbered picker with context sizes), `/mode`,
`/compact`, `/clear`, `/resume` (numbered picker), `/fork`, `/rewind`, `/diff`,
`/accept` (alias `/apply`: shows the diff report, asks `[y/N]`, then applies exactly what you
reviewed), `/ide`, `/export [FILE]`, `/cost`, `/context`, `/todo`, `/jobs`, `/init`, `/image FILE` (the workspace only; `--outside` allows another path and prints a warning),
`/theme dark|light|mono`, `/lockdown` and `/paranoia` (read only: change them on the host with
`hal config set`), `/quit`. Your own commands are files `.hal/commands/NAME.md` in the project;
`$ARGUMENTS` is replaced by what you type after the name.

In `plan` mode, when a turn ends with a todo list, the UI shows the plan and asks
`Run this plan?`. `d` or `a` leaves plan mode for `default` or `auto-edit`; anything else keeps it.

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

## When it does not work

| Symptom | Fix |
| --- | --- |
| Box lines show as `?` or letters | The terminal is not UTF-8: it falls back to ASCII by itself; set `[ui].unicode = false` to be sure |
| No colour | `NO_COLOR` is set or `[ui].theme = "mono"` |
| `!cmd` says it needs the docker box | The session has backend `none` or `bwrap`; `!` only runs through `docker exec` |
| `@file` is not attached | It is over `[ui].mention_max_bytes`, not UTF-8, a symlink, or outside the workspace |
| Esc does nothing during a turn | stdin is not a terminal; use Ctrl-C |
| `/rewind` says no checkpoints | `hal chat` boxes have no work tree; use `hal code` |
| `/model` shows no list | The gateway did not answer within 3 s; `/model NAME` still works |
