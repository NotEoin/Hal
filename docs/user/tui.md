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
| `/mode default\|auto-edit` | `default` asks per policy; `auto-edit` lets the agent edit the box copy without asking. `plan` is not available yet |
| `/todo` | Show the agent's todo list |
| `/diff [N]`, `/rewind [N]` | Show changes since checkpoint N; list checkpoints, or restore one (files that cannot come back are listed) |
| `/apply [--allow-flags]` | Apply the isolated copy to your real project after the risk report |
| `/image PATH` | Attach a png, jpg, gif or webp (up to 8 MiB) to the next prompt; the agent must accept images |
| `/model` | Show the model; change it with `hal code --model NAME` |
| `/NAME ARGS` | Run `.hal/commands/NAME.md` from the project, with `$ARGUMENTS` replaced |

Ctrl-C during a turn cancels the turn. A checkpoint is taken before every prompt, so `/rewind`
always has a point to return to.

## When it does not work

| Symptom | Fix |
| --- | --- |
| `hal code` prints the agent's own prompt, not this UI | You passed `-- ARGS` or stdin is not a terminal; the UI needs a terminal |
| `/rewind` says no checkpoints | `hal chat` boxes have no work tree; use `hal code` |
| `/mode plan` is refused | Not built into the agent yet; use `default` and read `/diff` before `/apply` |
| `/image` is accepted but the agent ignores it | The agent build does not advertise image input yet |
