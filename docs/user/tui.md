# The terminal UI

`hal code` (on a terminal) and `hal chat` open the terminal UI. The tour, keys, slash commands,
approvals and settings are in [hal-code.md](hal-code.md). This page says which UI runs when, and
keeps the non-interactive paths: headless `hal -p` and `hal sessions`.

## Full screen and line UI

| Situation | UI |
| --- | --- |
| A terminal on stdin and stdout, `TERM` set and not `dumb` | Full screen (mouse and keys) |
| `[ui].interface = "line"`, or `HAL_TUI=line` | Line UI (scrollback, keys only) |
| `[ui].interface = "full"` | Full screen wherever it can start |
| No terminal, piped stdin, `TERM=dumb` | Line UI; `hal code` without a terminal runs the agent's own prompt |
| The `textual` package is missing (a pull without re-running the installer) | One line `full-screen UI unavailable: the textual package is not installed (re-run scripts/install.sh); using the line UI`, then the line UI |
| The full screen fails before it draws | One line `full-screen UI unavailable: REASON; using the line UI`, then the line UI on the same session |
| `hal chat MESSAGE`, `hal chat --json`, `hal -p`, `hal acp`, `hal tune --auto` | No UI: these never load the full-screen code |

`hal tune` follows the same rules ([models.md](models.md#the-tune-screen)). Checked by
`packages/hal-core/tests/test_tui_fallback.py`.

## Headless: `hal -p`

`hal -p "PROMPT"` runs one turn in a coding box on the current directory and exits. Add
`--assistant` for an assistant box (`/scratch` only), `--dir DIR` for another project, and
`--output-format text|json|stream-json`. Standard input is the prompt when no arguments are given.

```
hal -p --output-format json "list the failing tests"   # one object: reply, stop_reason, tool_calls, isolated_session
hal -p --output-format stream-json "..."               # one line per update, then a result line
```

A coding turn works on an isolated copy, so `isolated_session` names what to give to
`hal code accept` or `hal code export`. Exit status: 0 for a finished turn, 1 for another stop
reason or an agent failure, 2 for no prompt, 3 for a refused start (docker down, lockdown, no
consent). Verified by `packages/hal-core/tests/test_boxed_channels.py` with a fake agent.

## Sessions from the shell

| Command | What it does |
| --- | --- |
| `hal sessions list [--project DIR] [--limit N] [--json]` | Stored sessions, newest first, with project and title |
| `hal sessions export SID [--format md\|jsonl] [-o FILE] [--with-tool-output]` | Write a transcript with secrets redacted; tool output is left out unless asked for |
| `hal sessions resume SID`, `hal code --resume SID` | Reopen a session in its project (needs a terminal); refused when the project folder is gone |
