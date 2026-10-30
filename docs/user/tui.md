# The terminal UI

`hal code` (on a terminal) and `hal chat` open a terminal UI. The agent always runs in a box;
the UI talks to it over ACP and shows what it says. Verified by `packages/hal-core/tests/test_tui.py`
with a scripted agent; not yet run against a live model.

## What it shows

- The reply streams as it is written. Each tool call is one line; a finished call shows `ok` or
  `FAILED`; an edit shows a short diff (use `/diff` for the whole patch).
- Everything the model or a tool wrote is escaped before it is printed, so it cannot move the
  cursor, set the window title or fake a prompt.
- A broker approval shows the tool, tier, pack, the reason and every argument, then asks
  `Allow this exact call?`. Only `o` (once) or `s` (session, when offered) allows; anything else,
  including Enter, denies. `hal approve ID` in another terminal works too.

## Slash commands

| Command | What it does |
| --- | --- |
| `/help`, `/quit` | List commands; leave (Ctrl-D also leaves) |
| `/clear` | Start a new conversation in the same box |
| `/mode default\|auto-edit\|plan` | `default` asks per policy; `auto-edit` lets the agent edit the box copy without asking; `plan` is read-only: edits and commands are refused until you leave it |
| `/todo` | Show the agent's todo list |
| `/diff [N]`, `/rewind [N]` | Show changes since checkpoint N; list checkpoints, or restore one (files that cannot come back are listed) |
| `/apply [--allow-flags]` | Apply the isolated copy to your real project after the risk report |
| `/image PATH` | Attach a png, jpg, gif or webp (up to 5 MiB) to the next prompt; the model must take images (chat completions endpoint, profile `vision` not false) |
| `/model [NAME]` | Show the model, or switch to NAME from the next turn (the model server must serve it) |
| `/NAME ARGS` | Run `.hal/commands/NAME.md` from the project, with `$ARGUMENTS` replaced |

Ctrl-C during a turn cancels the turn. A checkpoint is taken before every prompt, so `/rewind`
always has a point to return to.

## When it does not work

| Symptom | Fix |
| --- | --- |
| `hal code` prints the agent's own prompt, not this UI | You passed `-- ARGS` or stdin is not a terminal; the UI needs a terminal |
| `/rewind` says no checkpoints | `hal chat` boxes have no work tree; use `hal code` |
| `/image` fails with "does not take image input" | The model profile sets `vision = false` or the endpoint is not chat completions |

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
