---
description: Summarise the week from daily notes + session logs, prep for next week
allowed-tools: Bash, Read, Write, Glob, Grep
---

# /weekly-review — Weekly Review

Produce a summary of the last 7 days and a setup for the week ahead.

## Step 1 — Gather
Compute the date range (last 7 days from `date +%F`). Read:
- `$HAL_VAULT/Calendar/Daily/` notes in range.
- `$HAL_VAULT/Areas/Work/Session-Logs/` entries in range (Quick Reference sections).
- Any meeting notes in range.
- Open `- [ ]` tasks across `Areas/`.

## Step 2 — Synthesise
Write `$HAL_VAULT/Calendar/Weekly/YYYY-Www.md` (ISO week, e.g. `2026-W24.md`) with:
- **Completed** — what actually got done.
- **Decisions made** — pulled from session logs + meetings.
- **Open items** — unfinished tasks, carried forward.
- **Projects status** — one line per active project.
- **Next week** — suggested focus, drawn from open items.
Frontmatter: `type: weekly`, `date`.

## Step 3 — Brief me
Give the highlights in chat (tight), then confirm the saved path and ask what to prioritise
next week.
