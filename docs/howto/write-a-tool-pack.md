# Write a tool pack

A tool pack is a Python distribution with one or more Hal tool modules. Hal's hub finds it
through its `hal.packs` entry point, applies the user's policy to every call, and serves its
tools to Hal, to the `hal-hub` command and to any MCP client. The pack also gets its own
command, `hal-<pack>`, that works without the rest of Hal. These steps make a pack called
`weather`. They were run end to end on 2026-10-29 in a fresh virtual environment with
editable installs of `hal-kit`, `hal-hub` and the generated pack.

## Steps

1. Create the skeleton. The name is lowercase letters and digits; it is also the prefix of
   every tool name.

   ```bash
   hal-hub new weather --dir ~/src              # writes ~/src/hal-weather/
   hal-hub new weather --dir ~/src --exec       # instead: a pack that imports nothing from Hal
   ```

2. Write the tools. Each tool is a function in `src/hal_weather/tools.py` and a `[[tools]]`
   table in `src/hal_weather/hal-module.toml`. Pick the tier by what the tool does, because
   the tier sets the default policy: `read` (no side effects, allowed), `write` (changes the
   pack's own data, allowed and logged), `act` (changes the desktop or the user's files,
   asks), `send` (sends data off the machine, asks), `system` (power, packages, services,
   asks). Set `network = true` for a tool that uses the network, and `untrusted_output = true`
   when its result can hold text from strangers (web pages, mail). Every key is in the
   [manifest reference](../dev/manifest.md).

   ```toml
   [[tools]]
   name = "weather_now"                          # <module>_<words>
   function = "now"
   description = "Current weather for a town."
   tier = "read"
   network = true
   untrusted_output = true
   [tools.params.place]
   description = "town or city"
   required = true
   max_length = 100
   ```

3. Read secrets and settings through `halkit`, never from the arguments. Declare a secret in
   `[[module.secrets]]`; the hub passes it to the worker for that module only.

4. Install the pack into the environment the hub runs in, and run its test.

   ```bash
   cd ~/src/hal-weather
   uv pip install -e .                           # into the hub's environment
   uv run --group dev pytest                     # the generated behaviour test
   ```

   Inside the Hal monorepo the workspace version is `0.1.0.dev0`, which the generated
   `hal-hub>=0.1,<0.2` pin does not accept; install with `--no-deps` there.

5. Check the pack through the hub.

   ```bash
   hal-hub doctor                                # starts the worker, checks signatures
   hal-hub tools weather                         # the pack's tools as search finds them
   hal-hub call weather_hello name=Ada           # one call through the policy pipeline
   ```

6. Use the pack's own command, which needs no Hal core.

   ```bash
   hal-weather tools                             # only this pack's tools
   hal-weather call weather_hello name=Ada       # profile "standalone" by default
   hal-weather mcp                               # flat MCP server with only this pack's tools
   ```

7. If the pack needs a download or a build, declare `[[install]]` steps in the manifest and
   run them; each step asks for a typed `y`.

   ```bash
   hal-hub setup weather --dry-run               # show the steps
   hal-hub setup weather                         # run them, one confirmation each
   ```

## Symptoms and fixes

| Symptom | Fix |
| --- | --- |
| `hal-hub tools` does not show the pack | `hal-hub modules --all` shows why; install the pack into the hub's environment, or check that `[project.entry-points."hal.packs"]` names the package |
| `ManifestError ... unknown key` | The key is misspelt or not in schema 2; compare with the [manifest reference](../dev/manifest.md) |
| `manifest_mismatch` when calling a tool | The function's parameters differ from `[tools.params]`; `hal-hub doctor` lists the difference |
| `worker_failed: cannot load ...` | The worker cannot import the module: install the pack (step 4) or fix the `python` import path |
| A call returns `policy_denied` with rule `no-approver` | The tool's tier asks for approval and no one can answer; call it from a terminal, or answer with `hal-hub approve ID` |
| A call returns `timeout` | The tool ran longer than its `timeout` (default 60 s); raise it in the manifest, up to 7200 s |
| `hal-hub new` refuses to write | A file it would create already exists; choose another `--dir` |
| `uv pip install` says the requirements are unsatisfiable | The pack pins `hal-hub>=0.1,<0.2` and the installed hub is a development version; use `--no-deps` |
