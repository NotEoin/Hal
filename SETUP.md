# Set up Hal

This page installs Hal from a checkout, points it at your models and checks the result. The
commands were read against the argument parsers of this checkout (`hal setup --help`,
`hal tune --help`, `scripts/install.sh --help`). Steps that need a live docker daemon or GPU are
marked unverified where they were not run.

On Windows, do [Run on Windows with WSL2](docs/howto/run-on-windows-with-wsl2.md) first, then
come back here inside the Ubuntu shell.

## Before you start

| Need | Check |
| --- | --- |
| Linux with systemd | `systemctl --user is-system-running` prints `running` or `degraded` |
| Python 3.11 or later | `python3 --version` |
| uv | `uv --version`; install it from <https://docs.astral.sh/uv/> |
| Docker | `docker info --format '{{.ServerVersion}}'`; needed for `hal code` and `hal chat` |
| A model | An Ollama server, an OpenAI-compatible server, or a folder of GGUF files |

`scripts/install.sh` never uses sudo and never pipes a download into a shell. Installing docker or
gVisor needs root and is a separate step.

## 1. Install the commands

```sh
git clone https://github.com/NotEoin/Hal.git && cd Hal
scripts/install.sh --dry-run   # print every step, change nothing
scripts/install.sh             # run it
```

This creates `~/.local/share/hal/venv`, installs the packages of the checkout in it (editable)
and links `~/.local/bin/hal`. Options: `--core-only` skips the gateway and remote packages,
`--no-link` skips the link. A second run changes nothing. Make sure `~/.local/bin` is on your
`PATH`, then open a new terminal.

```sh
hal version   # lists the installed Hal packages
```

## 2. Finish the install

```sh
hal setup --dry-run   # prints each step as [plan]; changes nothing
hal setup             # run it
```

In order, `hal setup` syncs the workspace, installs the packs and the employment helper it finds
as sibling checkouts, installs gpu-light from a sibling checkout when it is missing, builds the box
image when docker exists, writes the hal-* user units and runs `hal doctor`. A missing piece is
skipped. It downloads no model. Units are written but not enabled; add `--enable` to start them
at login. Use `--from DIR` (repeatable) to name the folder that holds the sibling repos, and
`--replace` only when you mean to replace the units of another install. The step list and its
rules are in [docs/user/cli.md](docs/user/cli.md#hal-setup).

The suite repos are under [github.com/NotEoin](https://github.com/NotEoin): gpu-light, hal-voice,
hal-tools, hal-memory and hal-employment. Clone the ones you want next to this checkout before
you run `hal setup`.

## 3. Point Hal at your models

```sh
hal models add-source ollama http://127.0.0.1:11434   # an Ollama server
hal models add-source gguf ~/models                   # or a folder of GGUF files
hal models list                                       # what Hal can see
hal tune --auto                                       # the plan; changes nothing
hal tune --auto --write                               # pin the models and save the settings
```

On a terminal, a bare `hal tune` opens an interactive screen. The roles, the settings and the
other source kinds are in [docs/user/models.md](docs/user/models.md); a step-by-step version is
[Point Hal at your models](docs/howto/point-hal-at-your-models.md).

## 4. Check it

```sh
hal doctor   # lockdown, backend, box images, model server, models, units
```

Every non-ok line carries its fix. Common ones:

| Line | Meaning |
| --- | --- |
| `images` fail | The box image is not built: `hal box image build` |
| `runtime` warn | The box runs on runc. Install gVisor: [docs/howto/install-gvisor.md](docs/howto/install-gvisor.md) |
| `models` warn | No chat model is assigned: step 3 |
| `model server` warn | Nothing answers on the gateway: start Ollama or `hal-gateway serve` |

## 5. Use it

```sh
cd ~/code/my-project
hal code      # coding session on a copy of the project
hal chat      # a conversation
```

`hal code` works on a copy. Changes reach the real project only after `hal code accept SID` (or
`hal code export`). See [docs/user/hal-code.md](docs/user/hal-code.md) and
[docs/user/sandbox.md](docs/user/sandbox.md).

## Configuration

Hal reads `~/.config/hal/config.toml`; `HAL_STATE_DIR`, `HAL_DATA_DIR` and `HAL_RUNTIME_DIR` move
a whole install. The security levels are yours alone to change:

| Setting | Values | Documented in |
| --- | --- | --- |
| `[sandbox].lockdown` | `strict`, `standard` (default), `relaxed`, `off` | [docs/user/security.md](docs/user/security.md) |
| `[policy].paranoia` | `off`, `low`, `balanced` (default), `high`, `max` | [docs/user/security.md](docs/user/security.md) |
| `[models.*]` | sources and roles | [docs/user/models.md](docs/user/models.md) |
| `[gateway]` | listeners, tokens, backends | [docs/user/gateway.md](docs/user/gateway.md) |

## Turn the GPU services off and on

```sh
hal off      # stop the services that use the GPU and unload models; reminders keep running
hal on       # start them again
hal kill     # stop everything Hal started
```

To share one GPU with background jobs, install gpu-light; the gateway then claims the card only
for a request ([docs/user/gateway.md](docs/user/gateway.md#sharing-the-gpu-with-gpu-light)).

## Work from an editor or another machine

| Task | Page |
| --- | --- |
| Zed, JetBrains, Neovim, VS Code | [docs/howto/](docs/howto/) pages named `use-hal-code-in-*` |
| Another Linux or Windows laptop | [use-hal-from-a-laptop.md](docs/howto/use-hal-from-a-laptop.md), [use-hal-from-a-windows-laptop.md](docs/howto/use-hal-from-a-windows-laptop.md) |
| Claude Code or another MCP client | [connect-a-harness-to-hal-tools.md](docs/howto/connect-a-harness-to-hal-tools.md) |

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `hal: command not found` | Put `~/.local/bin` on your `PATH` and open a new terminal. |
| `scripts/install.sh` exits 3 | uv is missing. Install it, then rerun. |
| `scripts/install.sh` says `~/.local/bin/hal` is a regular file | Move it away, then rerun. |
| `hal setup` fails the units step and names unit files | They belong to another install. Back them up, then use `--replace`. |
| `hal doctor` says docker is not on the PATH | Install Docker Engine; `hal code` and `hal chat` need it. |
| `hal tune` exits 3 | No source or no model. Do step 3. |
| `runtime dir` fails | Run `hal doctor --fix`, which sets your own runtime directory to 0700. |

## Remove

Run `hal units list` and `hal units remove MODULE` for each unit first. Then delete
`~/.local/bin/hal` and `~/.local/share/hal`. Your config stays in `~/.config/hal`.
