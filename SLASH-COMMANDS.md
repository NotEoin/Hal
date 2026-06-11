# Hal slash commands

Every command works in two modes and figures out which one it's in by checking whether the working
directory is inside `$HAL_VAULT`:

- **Vault mode** — memory is `$HAL_VAULT/CLAUDE.md`, session logs go to
  `$HAL_VAULT/Areas/Work/Session-Logs/`.
- **Project mode** — memory is `./CLAUDE.md` if there is one, session logs go to
  `./CC-Session-Logs/`. If the repo has been registered with `register-repos.sh`, logs are mirrored
  into the vault as well.

---

## Memory

### `/resume [N] [topic]`

Reload context at the start of a session. A leading number sets how many recent session logs to
pull (default 3); anything after it is treated as a topic to search for across the logs and memory.

Loads the right `CLAUDE.md` for the mode, pulls the N most recent logs by filename date, and if a
topic was given, greps the rest for it. Comes back with open tasks, active projects and whatever it
found on the topic — deliberately terse, because anything it loads is context the actual work
doesn't get.

### `/compress`

Save the session before you end it. Asks what's worth keeping, writes a log with the standard
structure, then offers to lift anything durable up into `CLAUDE.md`.

Logs are summary-first, not transcripts. Each one is a Quick Reference block, decisions made,
pending tasks, and optionally a few verbatim snippets. A full transcript would eat the context
window on the next `/resume`, which defeats the point.

### `/preserve`

Write one durable fact, decision or convention into `CLAUDE.md`. Archives the file automatically
once it passes about 280 lines, so permanent memory can't quietly grow until it fills the context.

---

## Daily

### `/daily-note`

Create or open `Calendar/Daily/YYYY-MM-DD.md` with priorities and links.

### `/brief` (`hal brief`)

Build today's morning brief and drop it into today's daily note.

Runs `morning-brief.py`, which assembles open tasks and today's deadlines, yesterday's wins, active
project and last session status, and a "concept of the day" pulled from the topic graph. It writes
a marker-fenced `☀️ Morning Brief` block under the daily note's H1, so re-running it refreshes the
block rather than stacking another copy. If the model is unavailable it falls back to a plain,
unsummarised brief. Cron-friendly — a 07:00 entry has it waiting before you sit down.

### `/inbox-process`

Triage `+Inbox/`. For each item, work out what it actually is and file it into the right area, with
frontmatter and links added on the way.

### `/weekly-review`

Summarise the week from the daily notes and session logs — what got done, what stalled, what's
carrying into next week.

---

## Notes and knowledge

### `/ask` (`hal ask`)

Answer a question using only your own notes, with `[[wikilink]]` citations.

Embeds the question, retrieves the closest passages from the local semantic index, and asks the
model to answer *from those passages only* — citing each source note as an Obsidian wikilink, or
saying plainly that the notes don't cover it. Built on the shared RAG core in `hal_rag.py`, and the
same retrieval is exposed through the MCP server.

Run `hal index` (`vault-index.py`) first to build the index. It's incremental, and it skips empty
stubs, dashboards and index shells so they don't pollute retrieval. Flags: `--k N`,
`--show-context`, `--no-llm`, `--mock`.

Retrieval runs on-device in every profile. The answer does too, unless you are on the
`cloud` profile, where Claude Code writes it from the retrieved passages.

### `/meeting-note`

Turn a transcript or a page of scrappy notes into a structured meeting note — YAML frontmatter so
it shows up in Dataview queries, and wikilinks out to the people and topics mentioned.

### `/summarize-slides [course]`

Find slide PDFs in a course folder that don't have lecture notes yet, run them through
`extract-pdfs.sh`, and write notes against each one.

### `/import-vault [name]`

Reclassify a staged old vault into `Areas/`. `import-vault.sh` copies the old vault into staging and
writes an `IMPORT-MANIFEST.md`; the command reads the manifest, proposes a destination for each
folder, and rewrites the notes with consistent frontmatter as it moves them.

### `/index-repos [repo]`

Flesh out the hub notes under `Areas/Code/`. Summarises each registered repo from its README, sets
up per-repo memory, and wires session-log mirroring.

Reads git metadata and README text only. It does not copy source into the vault, and it skips
`.env`, `*.key`, `*.pem`, credentials and anything matching `secrets*`.

---

## Obsidian integration

Live vault access goes through the **Local REST API** plugin, over `https://127.0.0.1:27124` with
the plugin's own certificate. That gives search, read, append and patch against the *running*
Obsidian instance, so notes update in front of you rather than on next reload. It only works while
Obsidian is open — everything else in Hal reads the vault straight off disk and doesn't care.

## Vault layout

```
Vault/
├── +Inbox/       quick capture, emptied by /inbox-process
├── Areas/        Work, Study, Personal, Health
├── Calendar/     Daily, Weekly, Monthly
├── System/       templates, dashboards, the RAG index
└── CLAUDE.md     permanent memory
```
