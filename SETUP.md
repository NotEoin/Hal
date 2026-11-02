# Hal, Setup

Hal runs local models on your GPU and gives you two ways to use them: a conversation (`hal chat`,
and through extensions a voice and Telegram) and Claude Code for coding (`hal claude`). `setup.sh`
picks one of four profiles from your hardware and installs what that profile needs.

```
hal-ecosystem/
+-- core/
|   +-- hal/            <- this repository: the launcher, the brain, setup.sh
|   +-- hal-hub/        <- finds extensions and runs their tools
+-- extensions/         <- hal-calendar, hal-memory, hal-voice, ... (each its own repository)
```

## What you'll end up with

- **A coding model** for Claude Code: on `24gb` and `12gb` a model served by Ollama with a 32K
  context (Qwen3.8-27B or Gemma 4 12B); on `max` three models through llama.cpp (see
  [Modes](docs/user/modes.md)); on `cloud`, Claude Code with your own login.
- **A chat brain**: a smaller model that answers in `hal chat`, the voice and Telegram, and calls the
  tools of your extensions.
- **`~/.config/hal/config.toml`**: the settings every part of Hal shares.

---

## Step 1, Run the installer

The ecosystem installer is the simplest route: from the ecosystem folder, `./install.sh` fetches
anything missing, runs this repository's `setup.sh` the first time, adds the assistant extensions
and starts Hal. To set up the core alone, open a terminal in this folder and run:

```bash
./setup.sh
```

It reads your GPU, proposes a profile and asks, `--profile max|24gb|12gb|cloud` forces one, `-y`
accepts the proposal. It installs Ollama and pulls the models, builds the 32K-context coding model,
installs Node.js and Claude Code, writes the config and adds `hal` to your PATH. Safe to re-run.

Some steps (installing Ollama or Node, tuning the GPU service) use `sudo` and may prompt for your
password. The GPU-tuning step is optional, if it's skipped, everything still works.

When it finishes, **open a new terminal** (or `source ~/.bashrc`) so the PATH updates apply.

## Step 2, Talk to it

```bash
hal chat
```

With no extensions Hal answers from the model alone, and tells you when a request needs an
extension. Add some:

```bash
hal ext list
hal ext add --pack assistant      # memory, calendar, tasks, notify, clock, web, briefing, voice
hal install                       # render and start their services
```

Each extension's README covers any one-time step it needs (an iCloud password for the calendar,
a bot token for Telegram, the Obsidian plugins for memory).

## Step 3, Code with it

```bash
cd ~/code/my-project
hal claude
```

That opens Claude Code in the current folder, on your profile's coding model. The first message is
a little slow while the model loads; it's quick after that. `hal` with no arguments opens Hal's
own interface instead (`hal code` in a project folder, `hal chat` elsewhere).

## Step 4, Check it

```bash
hal eval              # ~40 everyday requests in a sandbox, each answer checked
hal console           # the same requests in a web page, against your real setup, for you to grade
```

`hal eval --model a --model b` compares chat models; set the winner as `chat_model` in the config.

## Freeing the GPU

`hal off` (or the **Hal on/off** desktop launcher) stops every Hal service marked as a GPU user,
voice, speech, the memory sorter, the chat bridges, and unloads Ollama's models. Reminder daemons
keep running. `hal on` starts everything again; `hal status` shows the state. The max profile's
llama.cpp server is separate: `hal stop` and `hal serve` control it.

`hal kill` (or the **Hal kill** desktop launcher) stops all of it at once: every service and timer,
the reminder daemons, the llama.cpp server and any Hal command left running in a terminal.

---

## The max profile

On a machine with a 24GB GPU and 64GB of RAM, `./setup.sh --profile max` serves three
models through llama.cpp instead of Ollama, and `hal claude mode <name>` picks one of five ways
to run them. Setup installs a pinned `llama-server` build into
`~/.local/share/hal/llama.cpp/` and downloads about 160GB of GGUFs into the models
directory. Put that directory on a large drive:

```bash
./setup.sh --profile max --models-dir /mnt/big/hal-models
```

Downloads resume where they stopped if you re-run setup. `hal.conf` records the directory
as `HAL_MODELS_DIR`, and the default mode as `HAL_MODE`.

**-> Modes, switching, and the measurements behind each model: [Modes](docs/user/modes.md)**

## Sharing the GPU with background jobs

gpu-light, a separate tool, hands one GPU between long background jobs (a batch runner, a
training queue) and the programs you use yourself: background jobs use the card while its
light is green, and leave within 30 seconds when it turns red. Hal works with it when it finds
`gpu-light` on the PATH, and behaves exactly as before when it does not.

Any program on the GPU turns the light red by itself, but only once it has loaded something.
Ollama, when it finds the VRAM full, quietly puts part of the model in system RAM, where it
stays slow until it is loaded again. So Hal claims the light first and waits for the
background jobs to leave. Hal holds no claim while it idles, so a background job keeps the card
between requests:

| Command | Claim | What Hal does |
|---|---|---|
| Model request through the gateway | `hal-gateway-<class>` | The gateway claims per request and gives the claim back when idle (see the [gateway guide](docs/user/gateway.md#sharing-the-gpu-with-gpu-light)). |
| `hal on`, login | none | `hal on` removes the off flag, unloads any Ollama model already split into RAM and starts the services. It does not wait for the GPU, and no unit claims it at login. |
| `hal claude` on an Ollama model | `hal-session-<pid>` | Claims for the whole session, waits up to 60 s and unloads any model already split into RAM. An open session counts as using the GPU, even while Ollama has no model loaded; the claim ends when Claude Code exits, even after a crash. |
| `hal serve`, and `hal` on the max profile | `hal-serve` | Claims, waits up to 120 s, then starts the llama.cpp server, in every mode (`cpu` mode still uses about 3GB of VRAM for prompt processing). The claim then follows the server, so a server that crashes stops holding the GPU. `hal stop` releases it. |

`hal off` still releases a claim named `hal`, and `hal units install` (and `hal setup`) removes
`hal-gpu-light.service`, so a machine upgraded from the old boot claim is cleaned up.

If a wait times out, Hal starts anyway and says the models may load slowly. A failing
`gpu-light` is reported and never stops Hal. `hal status` adds a line with the light and its
reason; `gpu-light status` shows everything that holds it. `HAL_GPU_LIGHT=off` turns the hook
off, and `HAL_GPU_LIGHT=/path/to/gpu-light` uses another executable.

The background jobs get the GPU back once every claim is gone and the GPU has been free for
gpu-light's clear period (3 minutes by default).

### Deviations from the original plan

The hook speaks gpu-light's file protocol, version 1, through its command line. It was
planned with `boot` claims for `hal-serve` and no claim in `cpu` mode, and differs
in two places, on purpose:

- **`hal-serve` is a `pid` claim once the server runs.** It starts as a `boot` claim, so Hal
  can wait before anything loads, and is then claimed again with `--pid` of the server.
  Otherwise a crashed server would hold the GPU until `hal stop` or a reboot. The protocol
  does not say whether a claim may replace one of the same name; Hal assumes it may. If
  gpu-light refuses, Hal warns and the `boot` claim stays, which only keeps the light red.
- **`cpu` mode claims too.** It keeps the weights in RAM, but llama.cpp still uses about 3GB
  of VRAM for prompt processing, so a background job holding the card would starve it.

## How the local-model wiring works (for the curious)

Ollama exposes an **Anthropic-compatible API**, so Claude Code can talk to it directly with
no proxy. On the local profiles the `hal` launcher just sets:

```
ANTHROPIC_BASE_URL=http://localhost:11434
ANTHROPIC_AUTH_TOKEN=ollama
ANTHROPIC_MODEL=$HAL_MODEL
ANTHROPIC_SMALL_FAST_MODEL=$HAL_FAST_MODEL
```

...and launches `claude`. The big model handles your requests; a small one handles Claude
Code's background bookkeeping so it stays responsive. On the `cloud` profile the launcher
sets none of these and leaves Claude Code's own configuration alone.

---

## Tuning & troubleshooting

**Out of memory / model won't load.** The `24gb` profile's 27B at Q4 (~18GB) plus a 32K
context needs about 22GB free. If you hit VRAM limits, drop to `--profile 12gb`, lower the
context (edit `NUM_CTX` in `setup.sh`, e.g. `16384`, and re-run), or close other GPU apps.
The installer already enables flash attention and q8 KV cache to save VRAM.

**"Response exceeded the 32000 output token maximum."** This is the cap on how much Hal can
write in a *single* reply, and it's deliberately bounded by the model's context window. The
window is 32K tokens **total** and input + output share it, so an output cap anywhere near
32K leaves no room for your prompt and files, and hitting it usually means the local model ran
away into a loop rather than that you need a longer answer. `hal` now sets a sane
`CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192` (~=6,000 words/turn), which leaves ~24K for context.
Raise it for a one-off with `HAL_MAX_OUTPUT_TOKENS=16384 hal`, but don't exceed ~24K. To
*truly* allow longer single replies you must enlarge the whole window: raise `NUM_CTX` in
`setup.sh` (e.g. `49152`) and re-run to rebuild the model, that costs VRAM, so watch
`nvidia-smi`; on a 24GB card the 27B leaves only modest headroom.

**It's slow.** The very first reply after launch includes load time. For more speed, run the
Mixture-of-Experts coder with `hal claude model qwen3-coder:30b` (~3.3B active parameters), at some cost
in code quality. Keep
`OLLAMA_KEEP_ALIVE` high (the installer sets 30m) so it doesn't unload between prompts.

**Claude Code asks me to log in.** With the `hal` env vars set it should use the local
endpoint. If it still prompts, just pick "use your own API key"-style option; the dummy
`ollama` token satisfies it. You never need a real Anthropic account for local use.

**Wrong model selected inside Claude Code.** Type `/model <name>`, using the model named in
`hal.conf` as `HAL_MODEL`.

**`hal chat` answers but never uses a tool.** Check `hal ext doctor` (is hal-hub found, do the
extensions load?) and `hal chat --json "..."`, which shows the tools offered and called. A model
that is too small for tool calling shows up in `hal eval` as many failures.

**Hal says something was done but it wasn't.** Run that request through `hal console` or
`hal chat --json`: the tools list shows whether a call failed. Add it to `eval/assistant/scenarios.toml`
so it stays fixed.

**Change the model later.** Pull anything from Ollama and launch with it:
`hal claude model qwen3-coder:30b` (or set `HAL_MODEL`). The dense 27B default writes the better code;
the MoE 30B answers faster.

---

## A realistic expectation

A local 27B model is genuinely useful for coding, note-taking, search and refactors, and it
is private and free to run. It is **not** as capable as frontier cloud models on the hardest
reasoning or very large multi-file tasks, that is the trade the `cloud` profile exists to
make, and what `deep_work` hands long jobs to. Good context is what makes a local model punch above
its weight: the memory extension gives Hal yours.
