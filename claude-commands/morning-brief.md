---
description: Build today's local morning brief and drop it into today's daily note
argument-hint: "[--print-only] [--date YYYY-MM-DD]"
allowed-tools: Bash, Read, Write
---

# /brief — Morning Brief

Pull the day together from the vault — open tasks & today's deadlines, yesterday's wins, the
state of active projects and the last work session, and a "concept of the day" from the topic
graph — and write it into today's daily note as a `☀️ Morning Brief` block.

## Step 1 — Run the local brief engine
This is a self-contained, fully-local script (talks only to Ollama on localhost, reads/writes
files on disk). From the vault root, run:

```bash
python3 "$HAL_ROOT/morning-brief.py" $ARGUMENTS
```

If `$HAL_ROOT` isn't set in this shell, it's the folder that holds `hal` and `morning-brief.py`
(the repo root) — the script also falls back to `$HAL_VAULT/../Vault` and to its own directory.
The script:
- assembles a compact **packet** from the vault (so the 32K window stays light),
- asks the local model for a warm, one-minute brief (falls back to a plain brief if the model
  is down — pass `--no-llm` to force that),
- inserts/replaces the brief block right under today's daily-note H1 (idempotent — re-running
  just refreshes it), and prints the brief to the terminal.

Useful flags: `--print-only` (don't touch the note), `--no-llm` (deterministic, no model call),
`--date YYYY-MM-DD` (build a brief for another day).

## Step 2 — Read it back and offer to act
Show the brief, then offer to:
- set the **Top 3 priorities** in today's daily note (ask the user for them),
- carry any **unfinished tasks** forward, and
- open the **concept of the day** stub if they want to flesh it out now.

Keep it tight. The brief is meant to be read in under a minute.

## Scheduling (set once)
This is built to run unattended at 07:00 — see SETUP.md → "Morning brief on a schedule" for the
cron / launchd / Task Scheduler one-liner. Cron runs `morning-brief.py` directly; Claude Code
isn't needed for the scheduled run.
