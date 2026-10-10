---
description: Load context — project memory + recent session logs (optionally search by topic)
argument-hint: "[N] [topic]"
allowed-tools: Bash, Read, Glob, Grep
---

# /load-context — Load Context

Arguments (all optional): `$ARGUMENTS`
- A leading **number** = how many recent session logs to load (default **3**).
- Any remaining **word(s)** = a topic to also search for across session logs and memory.

## Step 1 — Detect which project I'm in
Run `pwd` and `echo "$HAL_VAULT"`.
- If the current directory is inside `$HAL_VAULT` (or contains a `CLAUDE.md` with a
  `System/` folder) → **Vault mode**:
  - Memory file: `$HAL_VAULT/CLAUDE.md`
  - Session logs: `$HAL_VAULT/Areas/Work/Session-Logs/`
- Otherwise → **External project mode**:
  - Memory file: `./CLAUDE.md` (if present)
  - Session logs: `./CC-Session-Logs/`

## Step 2 — Load permanent memory
Read the memory file for that mode. This is the always-true context (Approach, Key Paths,
conventions, current projects, decisions, pending tasks). Do **not** read `CLAUDE-Archive.md`
unless the topic search below points there.

## Step 3 — Load recent sessions
Find the N newest `.md` files in the session-logs folder (by filename date / mtime). Logs are
summary-first by design (no raw transcripts), so the whole note is cheap to load even on the
local model. Read the **Quick Reference / Decisions / Pending Tasks** sections first; pull in
**Key Learnings / Files Modified / Notes** only when the task or topic search calls for it.

## Step 4 — Topic search (only if a topic was given)
Grep the session logs **and** the memory file (and `CLAUDE-Archive.md` if relevant) for the
topic. Surface the matching sessions and the specific decisions/learnings about it.

## Step 5 — Brief me
Give a tight summary, no fluff:
- **Where we left off** (last session outcome)
- **Open / pending tasks** (as a checklist)
- **Active projects** and their state
- If a topic was searched: **what we know about `<topic>`**
Then ask: "What are we working on today?" and offer to update today's daily note.
