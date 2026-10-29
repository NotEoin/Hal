# hal-hub

hal-hub is the one way in to every Hal module's tools. It finds installed modules by their
`hal-module.toml` manifests, picks the few tools a request needs, runs them, and serves them to
Claude Code over MCP, so Hal's voice, chat and terminal clients share one set of abilities.

## Highlights

- Modules are plain folders with a manifest; installing one is cloning it, removing one is deleting it.
- Tool search ranks tools against a request, so a chat model sees eight tools, not two hundred.
- One MCP server with three meta-tools (`hal_find_tools`, `hal_call`, `hal_modules`), plus any tools you pin.
- Stdlib-only Python modules run in-process; modules with their own dependencies run as a subprocess.
- Every call goes through one policy pipeline. Tools that act on the desktop, send data or
  change the system ask a host surface for approval; a model can never approve its own call,
  and a call nobody can approve is denied.
- Every decision is appended to the audit log in `~/.local/state/hal/audit/`.

## Install

hal-hub needs Python 3.11 or later and one package from PyPI, the official MCP SDK
(`mcp>=2.3,<3`). It lives in the Hal monorepo as
`packages/hal-hub`. Put its launcher on your `PATH`, or run `uv sync --all-packages` in the
repository root to get a `hal-hub` console script:

```bash
ln -s ~/src/Hal/packages/hal-hub/bin/hal-hub ~/.local/bin/hal-hub
```

To give Claude Code the tools, add the server to a project's `.mcp.json`:

```json
{"mcpServers": {"hal-hub": {"command": "/home/you/.local/bin/hal-hub", "args": ["mcp"]}}}
```

`hal-hub mcp [--mode meta|flat] [--profile P] [--packs A,B] [--tools X,Y]` serves on stdio.
It answers clients that use the `initialize` handshake (protocol 2024-11-05 to 2025-11-25) and
clients that use the 2026-07-28 per-request envelope. In `meta` mode (the default) it lists
the three meta-tools, then the `always_offer` and pinned tools your policy lets the session
use; any tool can still be called by name or through `hal_call`, and policy decides every
call. In `flat` mode it lists every tool the session may use, so a harness can set its own
per-tool rules (for Claude Code, `mcp__hal__calendar_add`), and refuses any name it did not
list. `--packs` and `--tools` narrow what the server lists and runs. Each listed tool carries
MCP annotations derived from its tier (display only) and `_meta` with `hal/tier` and
`hal/approval`. Each result carries `_meta.hal` (`status`, `untrusted`, `guard`).

When a call needs approval, `--approvals` (default `[hub.mcp].approvals`, `auto`) decides who
is asked:

| Mode | Who answers |
| --- | --- |
| `auto` | the client's own dialog (MCP elicitation, `Allow once` or `Deny`) when the client supports forms, and `hal-hub approve ID` from any terminal; the first answer wins |
| `elicit` | the client's dialog only; a client without it gets `no-approver` |
| `out-of-band` | `hal-hub approve ID` (or `hal approve ID`) only |
| `deny` | nobody: every call that needs approval is denied |

The request id goes to stderr. With no answer within `[approval].timeout_s` the call is
denied with rule `approval-timeout`; long calls send `notifications/progress` every 15 s to
clients that asked for progress. A dialog answer is advisory: a harness on your desktop runs
as you and could answer its own dialog, so treat `hal-hub mcp` approvals as protection against
a model's mistakes, not against a hostile harness. Sandboxed agents (`hal box`) never get a
dialog.

`hal remote` (07-remote) starts `hal-hub mcp --channel remote --origin remote:CLIENT` for a
laptop over SSH. That session uses the `remote` policy channel (tier `system` and the
`desktop` pack are denied), always uses `--approvals auto` (another value is a usage error),
and records the laptop's address from `HAL_REMOTE_ADDR` in every audit record. `--origin` is
accepted only with `--channel remote`.

`hal-hub mcp --transport http` serves `http://127.0.0.1:8742/mcp` (MCP streamable HTTP) for
harnesses that prefer a URL. It binds only `127.0.0.1` or `::1` (`--host`; anything else exits
2), and every request needs `Authorization: Bearer <token>`, with the token in
`~/.config/hal/hub-token` (created mode 0600 on first use; the server refuses a token file
others can read). A request with a foreign `Host` or an `Origin` not given with
`--allow-origin` is refused with 403, so a web page cannot reach the hub. `hal-hub token` prints
the token file's path and `hal-hub token --rotate` writes a new token. All HTTP clients share
one session and one approval socket. For Claude Code:

```bash
claude mcp add --transport http hal http://127.0.0.1:8742/mcp \
  --header "Authorization: Bearer $(cat ~/.config/hal/hub-token)"
```

## Use

The command line covers every module:

```bash
hal-hub modules                                  # what is installed
hal-hub tools "remind me about the dentist"      # the best tools for a request
hal-hub show calendar_add                        # schema, tier and verdict per profile
hal-hub calendar_add title=Dentist when="thursday 10am"
hal-hub call calendar_add --json '{"title": "Dentist", "when": "thursday 10am"}'
hal-hub doctor                                   # start every worker and check signatures
```

When a tool needs approval and you are at a terminal, `hal-hub` shows the exact call and asks
`Allow once? [y/N]`; `--yes` answers yes for that one call. `--yes` is refused when stdin is
not a terminal or `HAL_HARNESS` is set, so an AI harness that runs `hal-hub` cannot approve its
own calls. Without a terminal the call waits (up to `[approval].timeout_s` in `policy.toml`)
for you to answer it from any terminal:

```bash
hal-hub approvals                                # pending requests
hal-hub approve apr_5f2c9a0d1e7b4c33             # shows the exact call, asks y/N, allows it once
hal-hub deny apr_5f2c9a0d1e7b4c33
```

These three refuse to run unless stdin and stdout are terminals and `HAL_HARNESS` is unset.

Exit codes: 0 success, 1 tool error, 2 usage or configuration error (including a malformed
`config.toml` or `policy.toml`), 3 denied or approval missing.

From Python, `import halhub` is cheap: the API loads on first use and runs every call through
one process-wide hub. A pack calls another pack's tool with `halhub.client.call`, which returns
the result or raises `ToolDenied` or `ToolFailed`:

```python
import halhub
from halhub import client

specs = halhub.function_specs("turn the lights red")    # function specs for a chat model
tools = halhub.find("add a dentist appointment")        # ToolSpec objects, best match first
client.call("rgb_colour", {"colour": "red"}, caller="mypack")
```

Listings leave out tools that policy denies. A tool sets `always_offer = true` in its manifest
to be offered on every turn (at most eight are). `halhub.call` and `halhub.ollama_tools` still
work but are deprecated and warn once: `halhub.call` returns the v1 dict (`status`, then
`result` or `error`) and never returns `needs_confirmation`.

## Write a module

A module is a folder with a `hal-module.toml` beside its code. The manifest names the module, says
how to call it, and declares each tool:

```toml
[module]
name = "weather"
summary = "Forecasts"
python = "halweather.tools"        # or: exec = ["{root}/.venv/bin/python", "-m", "halweather", "--stdin"]

[[tools]]
name = "weather_now"
function = "now"
description = "Current weather for a place."
[tools.params.place]
description = "town or city"
required = true
```

Search matches a tool's name, description and `keywords`, plus the module's `keywords`. The hub
knows only a few generic synonyms (`meeting` finds `event`, `todo` finds `task`) and no pack's
words, so list the words people use for your tools in `keywords`, such as `["lights"]` for a
colour tool.

With `python`, the hub imports the module and calls the function with keyword arguments. With
`exec`, it runs the command with the function name as the last argument and the arguments as JSON on
stdin, and reads JSON from stdout. A tool returns anything JSON can encode; raising an exception
reports an error to the caller.

`scaffold.py` in the ecosystem folder creates a module with this layout.

## Run a pack on its own

A pack can ship its own command, `hal-<pack>`, with no Hal core installed. Its entry point
calls `halhub.pack.main` with the pack's name and modules:

```python
from halhub import pack

def main() -> int:
    return pack.main("organiser", ("calendar", "tasks", "clock"))
```

`hal-organiser mcp|tools|call|doctor` then behave like the `hal-hub` commands, limited to the
pack's own modules, with the `standalone` policy profile (the default profile without
`system` tools) unless `--profile` says otherwise. `mcp` serves flat mode with only the
pack's tools (`--module NAME` narrows it further). `extra={"calendar-connect": fn}` adds a
human-only subcommand that gets the remaining arguments and returns an exit code; it is never
a tool, and a name that clashes with a built-in subcommand raises `ValueError`.

## Configuration

Settings live in the `[hub]` table of `~/.config/hal/config.toml`:

```toml
[hub]
module_paths = ["~/src/hal-ecosystem"]   # folders searched for modules
pinned = ["memory_remember", "calendar_add"]          # also listed directly to MCP clients
disabled = ["phone"]                                  # modules to ignore
```

`HAL_MODULE_PATH` (colon-separated) overrides `module_paths`. `[hub.mcp]` holds the MCP
server's defaults: `mode` (`meta` or `flat`), `approvals` (`auto`, `elicit`, `out-of-band`
or `deny`), `http_host`, `http_port` and `allow_origins`; the `hal-hub mcp` options override
them.

## Licence

MIT. See [LICENSE](LICENSE).
