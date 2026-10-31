# The hal command

`hal --help` lists every command. A bare `hal` runs `hal code` in a project folder and `hal chat`
elsewhere. Exit codes: 0 ok, 1 failed, 2 usage error, 3 prerequisite missing, 130 interrupted.

`hal --help` groups the commands (work, safety, system). `hal help COMMAND` is the same as
`hal COMMAND --help`, and `hal version` lists the installed Hal packages.

## hal doctor

`hal doctor` (also `hal status`) prints one read-only report and starts nothing. It checks the
runtime directory, docker, runsc, the box images, the model server and the lockdown and
paranoia lines. Add `--json` for machine output. It exits 1 when a line says FAIL.

```
[FAIL] runtime dir  /run/user/1000/hal has mode 0755, it must be 0700: run `chmod 700 /run/user/1000/hal`
[warn] runtime      runc shares the host kernel; install gVisor (docs/howto/install-gvisor.md)
[ok  ] model server 2 model(s): hal-fast, hal-code
```

Verified by `packages/hal-core/tests/test_doctor.py`.

## hal migrate

`hal migrate ecosystem [--from DIR] [--yes]` plans the move from the old layout and applies it
only with `--yes`. `hal migrate undo JOURNAL` reverts it. See
[move-from-hal-ecosystem](../howto/move-from-hal-ecosystem.md).

## Installing

`scripts/install.sh` creates `~/.local/share/hal/venv` with uv, installs the packages of this
checkout editable and links `~/.local/bin/hal`. It never uses sudo. `--dry-run` prints every
step and changes nothing; a second run changes nothing. Options: `--core-only`, `--no-link`.
Then run `hal doctor`. Verified by `packages/hal-core/tests/test_install_script.py`.

## hal connect

`hal connect HARNESS` prints the environment and MCP entry that point an outside tool (claude,
qwen, opencode, codex, crush, goose, cline) at the Hal gateway. The first line says the tool runs
unsandboxed. `--list` shows the harnesses, `--issue` mints the token `connect-<harness>` into
`~/.config/hal/tokens/` (mode 0600), `--json` prints the lines as JSON. The gateway needs
`[gateway].tcp = true`; `hal connect` sets it in `config.toml` after printing a notice, and the
gateway needs a restart to pick it up. `--write` (claude and qwen only) merges the settings into
`~/.claude/settings.json` or `~/.qwen/settings.json` after a diff with the token masked and a y/N
prompt, keeping the old file as `<file>.hal.bak`.
Verified by `packages/hal-core/tests/test_connect.py`.

## hal packs

Packs add tools to Hal. `hal ext` still works and prints a notice, then runs `hal packs`.

| Command | What it does |
| --- | --- |
| `hal packs list [--available] [--json]` | installed packs, and the catalogue with `--available` |
| `hal packs add NAME...` | install from the catalogue at its pinned commit |
| `hal packs add --set NAME` | install every pack of a catalogue set (`assistant`, `computer`, `personal`) |
| `hal packs add --local PATH` | install a development checkout (editable, unpinned); `[tool.hal-pack]` in its `pyproject.toml` picks `shared`, `own` or `system` |
| `hal packs add URL@REF --dist NAME` | install from a tag or a full commit; first shows the pack's modules, tools with tiers, units, install steps and unpinned dependencies, then needs a typed `y`; branches are refused |
| `hal packs remove NAME` | uninstall; data stores stay, an own-env folder moves to `data_dir()/removed/` |
| `hal packs enable NAME`, `disable NAME` | edit only `[hub].disabled` in the config file |
| `hal packs update [NAME...]` | move packs to the catalogue pin |
| `hal packs setup NAME` | run the pack's install steps through the hub, each shown (argv, folder, network) and run only after a `y` typed on a terminal; with no terminal they stay `pending`; a failed step disables the pack until setup succeeds |
| `hal packs doctor [NAME...] [--online]` | installed commit vs pin, modules, interpreters, setup state; `--online` also asks each source whether its release tag still points at the pin; exits 1 on a failure |

Packs install into `data_dir()/envs/packs` (a pack with `env = "own"`, such as voice, gets `data_dir()/envs/NAME` built with `uv sync --frozen`) and are recorded in `state_dir()/packs.json`, which the
hub reads. The installed commit is checked against the pin and the install is rolled back on a
mismatch. The catalogue ships with placeholder commits until a release fills them, so
`hal packs add NAME` refuses until then (unverified for a real release: run it against a built wheel).

`hal doctor --only NAME` (repeatable) shows only checks whose name starts with NAME, for example
`--only runtime`. `hal doctor --fix` sets your own runtime directory to 0700 and re-checks; it
changes nothing else and refuses a directory owned by another user.

## Other commands

Each command also answers `--help`. Verified 2026-10-31 by running `python -m halcore COMMAND
--help` under a temporary `HOME` and XDG tree.

| Command | What it does |
| --- | --- |
| `hal jobs [list \| show ID \| cancel ID \| add TASK... \| run [--wait] \| follow ID \| accept ID \| export ID FILE]` | Background boxed jobs; changes leave only through `accept` or `export` |
| `hal kill [--quiet]` | Stop every Hal process, unit and box |
| `hal power` | One-line report: GPU services running, models loaded in Ollama, GPU light state |
| `hal gpulight claim NAME`, `release`, `status` | Talk to gpu-light; `status` prints `unknown` when it is not installed |
| `hal security [status\|doctor]` | Lockdown, paranoia and whether the classifier model is reachable |
| `hal audit tail [-n N] [--session ID] [--json]`, `hal audit verify` | Read the audit log, or check its hash chain |
| `hal units install [--start] [--no-desktop]`, `remove`, `list` | Render and install the systemd user units |
| `hal console [--port N] [--open]` | UNBOXED local web page for trying Hal; `--open` opens the single-use link (valid 60 s, sets a strict cookie). Broker approvals queue on the page: Allow stays disabled until the whole argument text is scrolled into view. Verified by `test_consolesurface.py` |
| `hal eval [--model M] [--only IDS] [--category C] [--repeat N]` | Ask Hal the test cases and check the answers |
