# 🧠 Hal

![licence](https://img.shields.io/badge/licence-MIT-blue?style=flat-square)
![platform](https://img.shields.io/badge/platform-Linux-lightgrey?style=flat-square)
![python](https://img.shields.io/badge/python-3.11+-blue?style=flat-square)
![runs](https://img.shields.io/badge/runs-100%25%20local-success?style=flat-square)

> *A local AI that runs on your own GPU, talks to you, codes with you, and learns new tricks
> one extension at a time.*

Hal is the core of a desktop assistant. It runs the local models, gives you a conversation with them
(`hal chat`), and points [Claude Code](https://claude.ai/code) at them for coding (`hal here`).
Everything else (a memory in Obsidian, your calendar on your phone, a voice, timers, lights,
the computer itself) is an extension you add when you want it. Raw Hal works on its own.

## 🌟 Highlights

- **One brain for every way in.** Terminal, voice and Telegram all share the same agent loop: a local
  model that streams its reply and calls the tools of whatever extensions are installed.
- **Extensions are just repositories.** `hal ext add calendar` clones one, runs its setup and starts
  its services; `hal ext remove` undoes it. With none installed, Hal says so instead of pretending.
- **It asks before it destroys.** Deleting, killing and shutting down need a yes from you in an
  earlier turn; a model cannot confirm on its own behalf.
- **Measured, not guessed.** `hal eval` asks Hal a set of real requests in a sandbox and checks every
  answer, so a model or prompt change shows up as a number. `hal console` lets you try the same
  requests by text or voice and grade them.
- **The GPU is yours when you want it.** `hal off` (or the desktop launcher) stops everything that uses
  the GPU and unloads the models; reminders keep running. `hal on` brings it back. With gpu-light
  installed, background GPU jobs step aside when Hal starts and come back once it stops
  ([SETUP.md](SETUP.md#sharing-the-gpu-with-background-jobs)).
- **Coding on your own hardware.** Claude Code runs against a local model, chosen for your GPU, from a
  24GB card down to none at all.

## ℹ️ Overview

| Piece | What it does |
|---|---|
| `hal` | The launcher: chat, coding, on/off, extensions, eval and console |
| `halcore/` | The brain (agent loop), extension management, services, eval and console |
| `eval/scenarios.toml` | The requests Hal is tested against, and how each answer is checked |
| `setup.sh` | Installs Ollama, the models for your GPU, Claude Code, and writes the config |
| `hal-serve`, `hal-route.py`, `modes/` | The max profile's llama.cpp server and its five modes |
| `models/` | The model catalogue and the role models (`hal-coder`, `hal-writer`, `hal-fast`) |
| `extensions.toml` | The extensions `hal ext add` knows how to fetch |

Hal finds extensions through [hal-hub](https://github.com/NotEoin/hal-hub), which sits beside it in
the ecosystem's `core/` folder. The whole ecosystem, with an installer that sets up both and the
extensions you choose, is [hal-ecosystem](https://github.com/NotEoin/hal-ecosystem).

### ✍️ Author

Built by [@NotEoin](https://github.com/NotEoin).

## 🚀 Usage

```bash
hal chat                 # talk to Hal; /reset starts over, /tools shows what it just ran
hal chat "what's on tomorrow?"   # one message
hal here                 # Claude Code on the local model, in the current directory
hal ext list             # installed extensions, and the ones you can add
hal ext add calendar     # add one (or --pack assistant for the everyday set)
hal off / hal on         # free the GPU, and take it back
hal status               # what is running
hal eval                 # check Hal's answers, in a sandbox
hal console              # try Hal and grade answers at http://127.0.0.1:8740
```

With the memory extension installed, `hal ask`, `hal index`, `hal brief`, `hal summarize` and
`hal import` work on your Obsidian vault; see
[hal-memory](https://github.com/NotEoin/hal-memory).

## ⬇️ Installation

The easy way is the ecosystem installer, which sets up Hal, hal-hub and the extensions you pick:

```bash
git clone https://github.com/NotEoin/hal-ecosystem.git ~/hal-ecosystem
cd ~/hal-ecosystem && ./install.sh
```

Raw Hal on its own:

```bash
mkdir -p ~/hal-ecosystem/core && cd ~/hal-ecosystem/core
git clone https://github.com/NotEoin/Hal.git hal
git clone https://github.com/NotEoin/hal-hub.git    # optional: needed only for extensions
./hal/setup.sh
```

`setup.sh` reads your GPU, proposes a profile and asks. It installs Ollama and the models that profile
needs, Node and Claude Code, puts `hal` on your PATH and writes `~/.config/hal/config.toml`. It's safe
to re-run.

```bash
./setup.sh --profile cloud    # force a profile: max | 24gb | 12gb | cloud
./setup.sh --profile 12gb -y  # ...and skip the prompt
```

**→ Full setup walkthrough: [SETUP.md](SETUP.md)**

### Requirements

| | |
|---|---|
| **OS** | Linux with systemd (for the background services) |
| **Python** | 3.11 or later; Hal itself needs nothing from PyPI |
| **GPU** | Whatever you have — see the profiles below. None is fine |
| **Disk** | ~160GB for the models on `max`, ~25GB on `24gb`, ~12GB on `12gb`, ~3GB on `cloud` |

#### Setup profiles

Each profile has a coding model (what Claude Code talks to in `hal here`) and a chat brain (what
answers in `hal chat`, the voice and Telegram). The brain is smaller, so it can sit beside the voice
engine and answer within about a second.

| Profile | Picked when | Coding model | Chat brain |
|---|---|---|---|
| `max` | ≥22GB VRAM and ≥60GB RAM | three models in five modes, up to a 177B (~160GB) | Gemma 4 12B, QAT (~7GB) |
| `24gb` | ≥22GB VRAM | Qwen3.8-27B (~18GB) | Gemma 4 12B, QAT (~7GB) |
| `12gb` | ≥11GB VRAM | Gemma 4 12B, QAT (~7GB) | Qwen3.5 4B (~3GB) |
| `cloud` | no usable GPU | none: Claude Code with your own login | Qwen3.5 2B, on the CPU |

The `max` profile serves three models through llama.cpp and lets you pick where they run per launch:
Qwen3.8-27B on the GPU (`gpu`, 80 tokens/s), the 177B Qwen3.8-Flash-Next-Coder in system RAM (`cpu`),
the 177B Qwen3.8-Flash-Next split across both (`hybrid`, 25 tokens/s, or `hybrid-256k` for its full
context), or `combined`, where a small classifier sends each request to the RAM model or the GPU model
and the RAM model delegates its edits to GPU subagents.
**→ Modes, measurements and quantisation choices: [MODES.md](MODES.md)**

The chat brain was picked with `hal eval`; run it with `--model` a few times to compare your own
candidates, and set the winner as `chat_model` in `~/.config/hal/config.toml`.

Originally built on an RTX 3090; developed since on an RTX 4070.

## 🧩 Extensions

`hal ext list` shows what is installed and what else there is. Packs add a set at once:

| Pack | Extensions |
|---|---|
| `assistant` | memory, calendar, tasks, notify, clock, web, briefing, voice |
| `computer` | system, desktop, files, power, packages, devices, network, shell, screen |
| `life` | habits, ledger, contacts, feeds, mail, phone, telegram |

An extension is a folder with a `hal-module.toml` that declares its tools; hal-hub's README explains
the format, and the ecosystem's `scaffold.py` creates one. Its services go in `systemd/` and are
rendered with its own path, so it works wherever it is cloned; a unit marked `X-HalGPU=yes` is one
`hal off` stops.

## 🔒 Privacy

- **On the `max`, `24gb` and `12gb` profiles nothing leaves the machine**: the models answering you
  are on your own GPU.
- **On the `cloud` profile, coding goes to Claude Code with your own login.** The chat brain still runs
  locally.
- **Long jobs** that Hal hands to Claude Code (`deep_work`) use your Claude Code login unless you set
  `[brain].deep_work = "hal"` (the local launcher) or `"off"`.
- Extensions that reach the internet (web search, weather, iCloud sync, ntfy) say so in their READMEs.

## 🧭 Limitations

- **A local model is not a frontier model.** It is quick and good at everyday requests and tool calls,
  and it will lose to a cloud model on hard, many-step reasoning; that is what `deep_work` is for.
- **Small models misread odd phrasing.** `hal eval` keeps a list of what works; add a case when
  something you say is misunderstood.
- Linux only.

## 💭 Feedback and contributing

Questions, bugs and ideas welcome — open an [issue](https://github.com/NotEoin/Hal/issues) or start
a [discussion](https://github.com/NotEoin/Hal/discussions). Run `python3 -m unittest discover -s tests`
and `hal eval` before sending a change to the brain.

## 📄 Licence

MIT — see [LICENSE](LICENSE).
