# Hal — Local AI Assistant + Obsidian Second Brain

A coding assistant and knowledge system built around an Obsidian vault that organises
itself. `setup.sh` picks one of three profiles from your hardware: on `24gb` and `12gb`
Claude Code drives a local model via Ollama and nothing leaves your machine; on `cloud`
there is no local model and Claude Code uses your own login. Searching your notes is
local in every profile.

```
This folder/
├── Vault/              ← your Obsidian vault, created by setup.sh (never committed)
│   ├── CLAUDE.md       ← Hal's permanent memory
│   ├── .mcp.json       ← Obsidian connection config
│   ├── +Inbox/  Areas/  Calendar/  System/
├── claude-commands/    ← the /resume /compress /preserve … skills (installed for you)
├── setup.sh            ← run this once; picks a profile and writes hal.conf
├── hal                 ← the launcher
└── SETUP.md            ← you are here
```

## What you'll end up with

- **A brain:** on the local profiles, a model served by Ollama with a 32K context
  (Qwen3.8-27B on `24gb`, Gemma 4 12B on `12gb`); on `cloud`, Claude Code itself.
- **Claude Code, pointed at it:** the same CLI and tooling, inference wherever you chose.
- **Memory system:** `/resume` reloads context, `/compress` saves a session, `/preserve`
  writes durable facts — so Hal remembers across sessions.
- **Proactive vault:** add a note once with frontmatter; dashboards surface it everywhere.

---

## Step 1 — Run the installer

Open a terminal in this folder and run:

```bash
chmod +x setup.sh hal import-vault.sh extract-pdfs.sh
./setup.sh
```

It reads your GPU, proposes a profile and asks — `--profile 24gb|12gb|cloud` forces one,
`-y` accepts the proposal. On the local profiles it installs Ollama and pulls the model
(~18GB on `24gb`, ~6GB on `12gb`) and builds the 32K-context variant. Every profile
installs Node.js, Claude Code, `uv` and the ~300MB embedding model, puts the slash
commands into `~/.claude/commands/`, and adds `hal` to your PATH. Safe to re-run.

Some steps (installing Ollama/Node, tuning the GPU service) use `sudo` and may prompt for
your password. The GPU-tuning step is optional — if it's skipped, everything still works.

When it finishes, **open a new terminal** (or `source ~/.bashrc`) so the PATH updates apply.

## Step 2 — Obsidian plugins

Open Obsidian, then:

1. **Settings → Community plugins →** enable community plugins.
2. **Browse**, then install + enable these two:
   - **Local REST API** (this is what lets Hal talk to your live vault)
   - **Dataview** (this powers the self-updating dashboards)
3. Open **Local REST API** settings and **copy the API key**.
4. **File → Open vault → Open folder as vault**, and select the **`Vault/`** folder inside
   this directory.
5. Open **`Start Here.md`** and **`System/Dashboards/Home.md`** — pin the dashboard.

## Step 3 — Give Hal the Obsidian key (one time)

```bash
hal obsidian-key PASTE_YOUR_KEY_HERE
```

(Stored locally in `~/.config/hal/`, never committed or sent anywhere.)

## Step 4 — Launch

```bash
hal
```

That opens Claude Code in your vault, on whichever brain your profile selected. Try:

```
/resume
```

The first message is a little slow while the model loads into VRAM; it's snappy after that.
When you're done for the session:

```
/compress
```

---

## Daily use

| Command | What it does |
|---|---|
| `hal` | Open Hal in your vault (the brain) |
| `hal here` | Open Hal in the current folder (for coding projects) |
| `/resume [N] [topic]` | Load memory + last N sessions; optionally search a topic |
| `/compress` | Save this session as a searchable log |
| `/preserve` | Add a permanent fact to `CLAUDE.md` |
| `/daily-note` | Create/open today's note with priorities |
| `/brief` (`hal brief`) | Build today's local morning brief into the daily note |
| `/ask` (`hal ask`) | Answer a question from your own notes, with citations |
| `hal index` | (Re)build the local semantic index `/ask` searches |
| `/meeting-note` | Turn a transcript into a structured meeting note |
| `/inbox-process` | File everything in `+Inbox/` properly |
| `/weekly-review` | Summarise the week |

The slash commands are **project-aware**: in the vault they use the vault's memory and
`Areas/Work/Session-Logs/`; in any other project they use that project's local `CLAUDE.md`
and a `CC-Session-Logs/` folder — so projects never mix.

### Morning brief on a schedule

`hal brief` (or `/brief`) assembles your day — open tasks & today's deadlines, yesterday's
wins, active-project + last-session status, and a "concept of the day" from your topic graph —
and writes a `☀️ Morning Brief` block into today's daily note. It's a self-contained local
script: it talks only to Ollama on `localhost`, reads/writes files on disk, and **degrades
gracefully** (if the model is down it writes a plain brief). Re-running the same day just
refreshes the block, so it's safe to fire on a timer.

Run it by hand any time:

```bash
hal brief                 # write today's brief into the daily note, print it
hal brief --print-only    # just print it
hal brief --no-llm        # skip the model (deterministic) — handy for a cron smoke test
```

To have it ready before you sit down, schedule it for ~07:00. The scheduled run calls the
script directly — Claude Code isn't needed.

**Linux (cron).** `crontab -e`, then add (point the paths at your install, and export
`HAL_VAULT` so it finds the vault):

```cron
0 7 * * *  HAL_VAULT=$HOME/Hal/Vault /usr/bin/python3 $HOME/Hal/morning-brief.py >> $HOME/.cache/hal-brief.log 2>&1
```

**macOS (launchd).** cron works too, but the native way is a LaunchAgent at
`~/Library/LaunchAgents/com.hal.brief.plist` running `python3 /path/to/morning-brief.py` with
`StartCalendarInterval` set to `Hour 7, Minute 0`, and `HAL_VAULT` in `EnvironmentVariables`.

**Windows (Task Scheduler).** Create a Basic Task → Daily → 07:00 → *Start a program*:
`python` with arguments `C:\path\to\morning-brief.py`, and add `HAL_VAULT` to the task's
environment (or hard-code `--vault C:\path\to\Vault`).

Make sure `ollama serve` is running at that hour (on Linux, `systemctl enable --now ollama`
keeps it up) so the brief gets the model-written version rather than the plain fallback.

---

## Ask your notes (local RAG)

`hal ask` lets you **chat with your brain**: ask a question and get an answer built only from
your own notes, citing each one as a clickable Obsidian `[[wikilink]]`. It is a small
retrieval-augmented pipeline — embeddings + a vector index + a chat model. **Retrieval always
runs on your machine.** On the local profiles the answer is written there too; on `cloud` the
retrieved passages go to Claude Code, so use `--no-llm` if that matters.

How it works: `hal index` walks the vault, splits each note into passages, and embeds them with
the local `nomic-embed-text` model into a tiny on-disk index under `Vault/System/.rag/` (three
plain files; navigation scaffolding like empty topic stubs and dashboards is skipped). `hal ask`
then embeds your question, finds the closest passages, and asks the configured model to answer
**using only those passages** — if your notes don't cover it, it says so instead of guessing.

```bash
hal index                 # build/refresh the index (incremental — only changed notes)
hal index --rebuild       # throw it away and build from scratch
hal ask "what is the Diffie-Hellman key exchange?"
hal ask --k 12 "compare quicksort and mergesort"
hal ask --show-context "..."   # also show which passages were used + scores
hal ask --no-llm "..."         # retrieval only (no model call) — fast sanity check
```

The index is incremental: each note's content hash is remembered, so re-running `hal index`
after editing a few notes is near-instant. It's rebuildable, so it's git-ignored — never commit
`System/.rag/`.

### The same brain, as an MCP server

`hal-mcp.py` exposes that retrieval core over the Model Context Protocol, so **any** MCP client
(Claude Desktop, Claude Code, another agent) can query your vault. `setup.sh` registers it in
`Vault/.mcp.json` as the **hal** server alongside **obsidian**, launched with
`uv run --with mcp` (no global install). It offers three tools:

- `search_vault(query, k)` — semantic search; returns the top passages with title, section and a
  `[[wikilink]]`.
- `ask_vault(question, k)` — a grounded answer in Hal's voice, citing the notes it used.
- `get_note(path)` — return one note's full Markdown by its vault-relative path.

Build the index once (`hal index`) and the MCP tools work the next time you launch Hal. If you
ever move the repo, re-run `./setup.sh` so the baked script path in `.mcp.json` stays correct.

---

## Importing an old vault + lecture slides

Bringing your old Obsidian vault (and undergrad slide PDFs) into the second brain is a
safe, staged process. Your originals are **never modified** — everything works on a copy.

**Set your institution first.** The study hierarchy is rooted at a note named after your
university, and every breadcrumb starts there. It lives in `hal.conf`, not in the repo:

```bash
./setup.sh --university "My University"   # or edit HAL_UNIVERSITY in hal.conf
```

Set it before the first import — renaming the root later means fixing every breadcrumb.
Re-running `setup.sh` keeps whatever `hal.conf` already says.

**1. Copy the old vault into staging (instant, lossless):**
```bash
./import-vault.sh /path/to/OldVault
```
This copies it into `Vault/_Import-Staging/` and writes `IMPORT-MANIFEST.md` (note counts,
folders, file types, and a **duplicate-filename report** — the one real risk when moving
notes, since Obsidian resolves `[[links]]` by filename). Run it once per source folder
(e.g. again for a separate slides folder). Add `--dry-run` to inventory without copying.

**2. Reclassify the notes into Areas:**
```bash
hal
/import-vault
```
Hal reads the manifest, proposes a folder → destination map (lecture notes →
`Areas/Study/<Course>/`, work/personal elsewhere), and — after you approve — adds
frontmatter (`type`, `course`, `date`, `tags`) and files each note, fixing filename
collisions so links survive. It goes folder-by-folder so you can sanity-check early.

**3. Prepare the slide files (fills the gaps):**
```bash
./prepare-import.sh          # add --ocr for scanned/image-only decks
hal
/summarize-slides
```
`prepare-import.sh` is the one-shot prep step: it runs every extractor (PDF, DOCX, PPTX,
HTML), pulls the text out of every deck (the local model is text-only), inventories your
Markdown notes (which import as-is), and writes a single combined `SLIDES-REPORT.md` showing
which decks **already have a summary note** and which **don't**. (The individual
`extract-pdfs.sh` / `extract-docx.sh` / `extract-pptx.sh` / `extract-html.sh` still exist if
you only want one type.) `/summarize-slides` then generates a structured lecture note only for
the decks that are missing one — so it never duplicates summaries you already wrote. It's
resumable (re-running skips decks it already did) and works course-by-course, which matters
because summarising a whole degree's worth of slides on a local 30B takes a while.

**Summarising a lot of decks? Use `hal summarize` instead.** For hundreds or thousands of
files, don't run `/summarize-slides` interactively — the chat's 32K window fills up and slows
down. `hal summarize` is the **unattended** version: start it once and it grinds through every
outstanding deck in its own fresh model call per file (so the context never fills), writing
each note to disk as it goes and talking to you as Hal while it works. It's fully
**self-resuming** — stop it anytime with Ctrl-C, or let a crash happen, and just run it again:
it skips everything already done and continues, so you never restart it by hand. A run this
size takes hours, so kick it off and walk away; it logs itself to `Areas/Work/Session-Logs/`
when it finishes.

```bash
hal summarize                      # everything outstanding, hands-off
hal summarize --course Algorithms  # one course at a time
hal summarize --limit 5            # a quick trial run first
hal summarize --interactive        # pause for a nod before each course
```

**4. Verify, then clean up:** open Obsidian, confirm links/embeds resolve and the Study
dashboard (`Areas/Study/_Index.md`) is populated. Once you're happy, tell Hal to delete
`_Import-Staging/`.

> Link safety: if your old vault used **relative/absolute** link format (Settings → Files &
> Links) rather than the default "shortest path", mention it to Hal during `/import-vault`
> and it'll rewrite those links to wikilinks so nothing breaks.

## Adding your coding projects

Bring your repos into the second brain as a **linked layer** — the code stays where it lives
(local folders / GitHub); the vault just gets a lightweight hub note per repo that points to
it, summarises it, and surfaces its coding sessions on your dashboards. **No code is copied
into the vault, and file contents/secrets are never read** — only safe git metadata and a
file-type tally, with any credentials stripped from remote URLs.

**1. Register repos (scan for metadata, write hub notes):**
```bash
./register-repos.sh ~/code ~/projects     # scan these folders for git repos
./register-repos.sh ~/code/oneproject     # or a single repo
./register-repos.sh --github URL [Name]   # add a GitHub-only repo as a card
./register-repos.sh                        # no args = scan common dev folders
```
This writes one note per repo into `Areas/Code/` (language, remote, branch, last commit) plus
`Areas/Code/CODE-REPORT.md`. It's idempotent — re-run anytime; add `--refresh` to update the
git metadata on existing notes (your edits are preserved).

**2. Flesh them out + wire up memory:**
```bash
hal
/index-repos
```
Hal reads each repo's README to write a real summary and how-to-run into its hub note, then
helps you set up deep per-repo memory. The richest memory comes from Claude Code's built-in
`/init` run **inside** each repo (`hal here` in the repo, then `/init`) — that writes a
code-aware `CLAUDE.md` there. When you later `/compress` a coding session, the log is saved in
the repo's `CC-Session-Logs/` **and** mirrored into `Areas/Code/_Session-Logs/`, so your
coding work shows up on the Code dashboard (`Areas/Code/_Index.md`) next to everything else.

## How the local-model wiring works (for the curious)

Ollama exposes an **Anthropic-compatible API**, so Claude Code can talk to it directly with
no proxy. On the local profiles the `hal` launcher just sets:

```
ANTHROPIC_BASE_URL=http://localhost:11434
ANTHROPIC_AUTH_TOKEN=ollama
ANTHROPIC_MODEL=$HAL_MODEL
ANTHROPIC_SMALL_FAST_MODEL=$HAL_FAST_MODEL
```

…and launches `claude`. The big model handles your requests; a small one handles Claude
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
window is 32K tokens **total** and input + output share it — so an output cap anywhere near
32K leaves no room for your prompt and files, and hitting it usually means the local model ran
away into a loop rather than that you need a longer answer. `hal` now sets a sane
`CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192` (≈6,000 words/turn), which leaves ~24K for context.
Raise it for a one-off with `HAL_MAX_OUTPUT_TOKENS=16384 hal`, but don't exceed ~24K. To
*truly* allow longer single replies you must enlarge the whole window: raise `NUM_CTX` in
`setup.sh` (e.g. `49152`) and re-run to rebuild the model — that costs VRAM, so watch
`nvidia-smi`; on a 24GB card the 27B leaves only modest headroom.

**It's slow.** The very first reply after launch includes load time. For more speed, run the
Mixture-of-Experts coder with `hal model qwen3-coder:30b` (~3.3B active parameters), at some cost
in code quality. Keep
`OLLAMA_KEEP_ALIVE` high (the installer sets 30m) so it doesn't unload between prompts.

**Claude Code asks me to log in.** With the `hal` env vars set it should use the local
endpoint. If it still prompts, just pick "use your own API key"-style option; the dummy
`ollama` token satisfies it. You never need a real Anthropic account for local use.

**Wrong model selected inside Claude Code.** Type `/model <name>`, using the model named in
`hal.conf` as `HAL_MODEL`.

**Obsidian tools don't appear.** Obsidian must be **running** with the Local REST API plugin
enabled, and you must have run `hal obsidian-key`. Plain note reading/writing works even
without it, because the vault is Hal's working directory.

**Change the model later.** Pull anything from Ollama and launch with it:
`hal model qwen3-coder:30b` (or set `HAL_MODEL`). The dense 27B default writes the better code;
the MoE 30B answers faster.

---

## A realistic expectation

A local 27B model is genuinely useful for coding, note-taking, search and refactors, and it
is private and free to run. It is **not** as capable as frontier cloud models on the hardest
reasoning or very large multi-file tasks — that is the trade the `cloud` profile exists to
make. The memory system and a tidy vault are what make a local model punch above its weight:
good context in, good answers out.
