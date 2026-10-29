# hal-hub

hal-hub is the one way in to every Hal module's tools. It finds installed modules by their
`hal-module.toml` manifests, picks the few tools a request needs, runs them, and serves them to
Claude Code over MCP, so Hal's voice, chat and terminal clients share one set of abilities.

## Highlights

- Modules are plain folders with a manifest; installing one is cloning it, removing one is deleting it.
- Tool search ranks tools against a request, so a chat model sees eight tools, not two hundred.
- One MCP server with three meta-tools (`hal_find_tools`, `hal_call`, `hal_modules`), plus any tools you pin.
- Stdlib-only Python modules run in-process; modules with their own dependencies run as a subprocess.
- Tools marked `confirm` return a confirmation request until called again with `_confirmed: true`.
- Every call is logged to `~/.local/state/hal/hub-calls.jsonl`.

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
hal-hub calendar_add title=Dentist when="thursday 10am"
hal-hub power_shutdown --yes                     # a tool that asks for confirmation
hal-hub doctor                                   # check every manifest and entry point
```

From Python, the same calls are available to any client, such as hal-voice:

```python
import halhub
specs = halhub.ollama_tools("turn the lights red")   # function specs for a chat model
halhub.call("rgb_colour", {"colour": "red"})
```

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
