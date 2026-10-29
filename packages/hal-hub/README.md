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

hal-hub needs Python 3.11 or later and nothing from PyPI. It lives in the Hal monorepo as
`packages/hal-hub`. Put its launcher on your `PATH`, or run `uv sync --all-packages` in the
repository root to get a `hal-hub` console script:

```bash
ln -s ~/src/Hal/packages/hal-hub/bin/hal-hub ~/.local/bin/hal-hub
```

To give Claude Code the tools, add the server to a project's `.mcp.json`:

```json
{"mcpServers": {"hal-hub": {"command": "/home/you/.local/bin/hal-hub", "args": ["mcp"]}}}
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

## Configuration

Settings live in the `[hub]` table of `~/.config/hal/config.toml`:

```toml
[hub]
module_paths = ["~/src/hal-ecosystem"]   # folders searched for modules
pinned = ["memory_remember", "calendar_add"]          # also listed directly to MCP clients
disabled = ["phone"]                                  # modules to ignore
```

`HAL_MODULE_PATH` (colon-separated) overrides `module_paths`.

## Licence

MIT. See [LICENSE](LICENSE).
