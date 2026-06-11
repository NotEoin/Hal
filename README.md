# Hal

Claude Code pointed at a local model instead of the cloud, wired into an Obsidian vault that acts as
its long-term memory. Nothing leaves the machine.

The idea is that a coding assistant is much more useful when it remembers what you decided last
week. Hal gets that from a vault: session logs it writes itself, a `CLAUDE.md` of durable facts, and
a semantic index over the whole thing so it can answer questions from your own notes rather than
from its weights.

## What it is

- **A launcher** (`hal`) that starts Claude Code with the environment pointed at Ollama.
- **A set of slash commands** for saving, reloading and searching context, and for the note-taking
  workflows that keep the vault worth searching.
- **A local RAG index** over the vault — `hal index` builds it, `hal ask` queries it, and an MCP
  server exposes the same retrieval to any MCP client.
- **An importer** for dragging an old vault and a pile of lecture PDFs into a consistent structure.

## Why local

Ollama exposes an Anthropic-compatible API, so Claude Code can be pointed straight at it with a few
environment variables — you get the CLI and its tooling with the inference happening on your own
GPU. That means notes, code and session history never leave the machine, and there's no per-token
cost to running an assistant that reads your entire vault.

The trade is capability. A 30B model is genuinely good at code, search, refactors and note wrangling,
and clearly worse than a frontier model at hard multi-file reasoning. The memory system is what
closes some of that gap: a small model with well-organised context beats a small model without it by
a lot.

## Install

```bash
chmod +x setup.sh hal
./setup.sh
```

`setup.sh` installs Ollama, pulls the models, builds a 32K-context variant, installs Node, Claude
Code and `uv`, drops the slash commands into `~/.claude/commands/`, and puts `hal` on your PATH.
Safe to re-run. Open a new shell afterwards so the PATH change takes.

Then follow [SETUP.md](SETUP.md) for the Obsidian side — you need the **Local REST API** and
**Dataview** plugins, and Hal needs the REST API key:

```bash
hal obsidian-key <YOUR_KEY>
```

## Use

```bash
hal              # start in the vault
hal here         # start in the current directory, for a coding project
hal ask "..."    # answer from your notes, with citations
hal index        # build or refresh the semantic index
hal brief        # write today's morning brief into the daily note
hal model <name> # switch to a different Ollama model
```

The vault location defaults to `Vault/` next to the launcher and can be overridden with
`$HAL_VAULT`.

Slash commands are documented in [SLASH-COMMANDS.md](SLASH-COMMANDS.md). The ones that matter most
day to day are `/resume` at the start of a session and `/compress` at the end — that pair is what
makes the memory work.

## How the retrieval works

`vault-index.py` walks the vault, chunks each note, embeds the chunks and writes vectors to
`System/.rag/`. It's incremental — only changed files are re-embedded — and it skips empty stubs,
dashboards and index shells, which otherwise dominate retrieval because they're short and mention
everything.

`hal_rag.py` is the shared core. `ask.py` uses it to retrieve, then constrains the model to answer
from the retrieved passages only, citing each note as an Obsidian `[[wikilink]]` so you can click
through to the source. If the passages don't cover the question it's told to say so rather than
fall back on general knowledge, which is the whole point of asking your own notes.

`hal-mcp.py` exposes the same retrieval as an MCP server, so the vault is queryable from any MCP
client, not just through `hal ask`.

## Hardware

Tested on an RTX 3090 (24GB), i5-8400, 16GB RAM.

The 30B model at 32K context with a Q8 KV cache needs roughly 24GB of VRAM to stay resident — that's
the real floor. Less VRAM works if you drop to a smaller model (`hal model` will take anything Ollama
has) or shorten the context. The 32K window is shared between input and output, which is why the
slash commands are as aggressive as they are about keeping loaded context small.

## Privacy

Everything runs locally: the model, the embeddings, the index. The only network traffic is the
initial model pull.

`/index-repos` reads git metadata and README text from your registered repositories. It doesn't copy
source into the vault, and it skips `.env`, `*.key`, `*.pem`, credentials and `secrets*`.

Your vault is not in this repository and shouldn't be — `.gitignore` excludes `/Vault/`, the local
config under `~/.config/hal/`, and the RAG index. Keep it that way.

## Licence

MIT — see [LICENSE](LICENSE).
