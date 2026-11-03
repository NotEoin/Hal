# Hal

![licence](https://img.shields.io/badge/licence-MIT-blue?style=flat-square)
![platform](https://img.shields.io/badge/platform-Linux%20%7C%20WSL2-lightgrey?style=flat-square)
![python](https://img.shields.io/badge/python-3.11+-blue?style=flat-square)

> *Make the most of your local models.*

Hal turns the models you already run (Ollama, LM Studio, llama.cpp, GGUF files) into a coding
agent, a chat and a desktop assistant, all on your own machine. It reads your hardware and sizes
every model to it, so a laptop and a workstation each get the most context they can hold. And it
never trusts the model: the agent works on a copy of your project, in a box with no network, and
asks you before anything that matters.

- **Bring your own models.** Point Hal at Ollama, an OpenAI-compatible server or a folder of GGUF
  files. It downloads nothing.
- **Tuned to your hardware.** `hal tune` picks the context size, cache type and GPU offload for
  each model, on one GPU, several, or none.
- **A coding agent in a sandbox.** `hal code` edits a copy of your project inside a docker box, and
  you `accept` the changes you want. Switch models and thinking effort mid-session with `/model`
  and `/effort`.
- **One policy for every tool.** Each tool call is checked against your lockdown and paranoia
  levels, and the risky ones wait for your approval.
- **No telemetry.** Nothing leaves your machine unless you add a cloud model or a tool that goes
  online.

## Screenshots

| | |
| --- | --- |
| ![A finished turn: plan, tool rows, an inline diff and a code block](media/hal-code-turn.png) | ![The Ctrl+P palette over commands, sessions, files, models and settings](media/hal-code-palette.png) |
| A turn: the plan, folded tool rows, an inline diff and a code block with `copy` | Ctrl+P: one search over commands, sessions, files, models and settings |
| ![An approval dialog for a shell command that leaves the box](media/hal-code-approval.png) | ![The context popover with its Compact button](media/hal-code-context.png) |
| An approval: every argument shown, Deny focused, allowing takes two steps | A click on the context gauge: what fills the window, and Compact |

The [terminal UI tour](docs/user/hal-code.md) covers every key and menu.

## Requirements

| | |
| --- | --- |
| OS | Linux with systemd, or Windows 11 through WSL2 ([how-to](docs/howto/run-on-windows-with-wsl2.md)) |
| Python | 3.11 or later, and [uv](https://docs.astral.sh/uv/) |
| Docker | Needed for `hal code` and `hal chat`, which run the agent in a box. gVisor (`runsc`) is optional |
| GPU | Any, or none. `hal tune` sizes the models to what it finds |
| Models | Ones you already have. Hal downloads none |

## Install

```sh
git clone https://github.com/NotEoin/Hal.git && cd Hal
scripts/install.sh --dry-run   # print every step, change nothing
scripts/install.sh             # private venv in ~/.local/share/hal, link in ~/.local/bin
hal setup --dry-run            # plan the rest: packs, box image, units, doctor
hal setup                      # run it; add --enable to start the units at login
```

The full walk-through, with options and checks, is in [SETUP.md](SETUP.md).

## Point Hal at your models

```sh
hal models add-source ollama http://127.0.0.1:11434   # or: hal models add-source gguf ~/models
hal tune --auto                                       # show the plan, change nothing
hal tune --auto --write                               # pin the models and save the settings
```

See [Point Hal at your models](docs/howto/point-hal-at-your-models.md) and
[Models](docs/user/models.md).

## Main commands

| Command | What it does |
| --- | --- |
| `hal code [DIR]` | Coding session in a box, on a copy of the project. `hal code accept SID` applies its changes |
| `hal chat [MESSAGE]` | Talk to Hal in a box |
| `hal tune` | Detect hardware and plan model settings; interactive on a terminal, `--write` saves |
| `hal models` | List sources and models, add or remove a source, assign a role |
| `hal setup` | Finish an install: packs, gpu-light, box image, units, doctor |
| `hal doctor` | Read-only health report; exits 1 on a failed check |
| `hal on` / `hal off` | Start the services, or stop those using the GPU and free it |
| `hal lockdown`, `hal paranoia`, `hal policy` | Show the security levels and the approval policy |

`hal --help` lists every command, and `hal help COMMAND` shows one command's options.
Reference: [docs/user/cli.md](docs/user/cli.md).

## How it fits together

Hal is the core: the gateway that serves your models, the hub that runs every tool under one
approval policy, and the sandbox the agent works in. The other repos plug into it. Each one also
works on its own, so you install only what you want. Together they make one modular assistant that
writes code, keeps your calendar, remembers what you tell it, answers out loud, and shares your GPU
with everything else you run. Nothing leaves your machine unless you add a cloud model or turn on a
tool that goes online, and every tool call passes the same policy.

| Repo | What it gives you |
| --- | --- |
| [Hal](https://github.com/NotEoin/Hal) | The core: your models, a sandboxed coding agent and chat, the tool hub and the security policy |
| [hal-tools](https://github.com/NotEoin/hal-tools) | A desktop assistant: calendar, tasks, mail, messages, web, desktop and system control |
| [hal-memory](https://github.com/NotEoin/hal-memory) | A memory in an Obsidian vault, so small models answer from your notes, with citations |
| [hal-voice](https://github.com/NotEoin/hal-voice) | Speech in and out, in the voice of HAL 9000 |
| [gpu-light](https://github.com/NotEoin/gpu-light) | Keeps your assistant ready without taking the GPU from your games, renders and training |
| [hal-employment](https://github.com/NotEoin/hal-employment) | Finds jobs, writes the CV and cover letter and fills in the form; you press Submit |

`hal setup` installs the ones it finds next to this checkout.

## Security

Hal treats its own model as hostile: a model changed to cause damage is contained by the default
settings. Every Hal agent turn runs in a box with no network and no access to your files except a
copy of the project and a scratchpad. The full guide, with the paths that are *not* private, is
[docs/user/security.md](docs/user/security.md).

| Risk | What stops it |
| --- | --- |
| A tool call that deletes, sends or runs something you did not ask for | Tool tiers and the policy: `act`, `send` and `system` tools ask first, and an approval is bound to the exact arguments you saw |
| The model approving itself | An approval is a structured answer from a surface (terminal, phone, editor), never text the model wrote; a tool call cannot carry one |
| Instructions hidden in a web page, file or tool result | Results of untrusted tools come back in a marked envelope, and the session's taint raises what needs approval |
| The agent reading your home directory or secrets | The agent runs in a box with a copy of the project at `/work` and a scratchpad at `/scratch`; nothing else of the host is mounted |
| The agent reaching the network | The box has no network; its only channels out are the tool broker and the model socket |
| The agent changing your real repository | Changes leave the box only through `hal code accept` (diff report, flags, checkpoint) or `hal code export` |
| A risky call that policy allows | The risk classifier rates every boxed call and can only make the decision stricter (see below) |
| Changing its own limits | Lockdown and paranoia are read from host files and the host environment; no tool argument or box setting can change them |

Two settings are yours alone; no tool call can change either. `hal status` and `hal doctor` show them.

**Lockdown** (`[sandbox].lockdown`):

| Level | Meaning |
| --- | --- |
| `strict` | runsc (gVisor) required, Hal refuses to start without it; isolated copy only; resource limits; no host tool upstreams except `hal-hub`; paranoia floor `high` |
| `standard` (default) | runsc when installed, else runc with a doctor warning; isolated copy by default; limits on; host upstreams allowed by policy |
| `relaxed` | also allows `--direct` (the real project mounted read-write) and the dependency-install box with network |
| `off` | allows running without a box after a per-project consent; `hal doctor` reports a problem on every run; never a default |

**Paranoia** (`[policy].paranoia`): how strictly a local classifier rates each call.

| Paranoia | low risk | medium | high | critical |
| --- | --- | --- | --- | --- |
| `off` | policy | policy | policy | policy |
| `low` | policy (+review) | policy | ask | ask |
| `balanced` (default) | policy (+review) | ask | ask | deny |
| `high` | policy | ask | deny | deny |
| `max` | ask | ask | deny | deny |

## No telemetry

No Hal package sends usage data, crash reports or update checks. `tests/test_no_telemetry.py`
fails when a module opens a network client outside a short reviewed list.

## Run the tests

```sh
uv sync --all-packages
uv run pytest -q              # hermetic; gpu, docker, network and live tests are deselected
uv run ruff check .
scripts/check-ascii.sh
```

## Documentation

[docs/README.md](docs/README.md) indexes every guide, how-to and design spec.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md). Report vulnerabilities as
described in [SECURITY.md](SECURITY.md).

## Licence

MIT, see [LICENSE](LICENSE).
