---
description: Save the current session as a searchable session log before ending
allowed-tools: Bash, Read, Write, Glob
---

# /compress — Save Session

Capture this session so the next one can pick it up instantly.

## Step 1 — Detect mode
Run `pwd` and `echo "$HAL_VAULT"`.
- **Vault mode** (inside `$HAL_VAULT`): write the log to
  `$HAL_VAULT/Areas/Work/Session-Logs/`.
- **External project mode**: write to `./CC-Session-Logs/` (create the folder if missing).
  This keeps each project's history with the project — no cross-contamination.
  **If this repo is registered** (a hub note in `$HAL_VAULT/Areas/Code/` has `path:` equal to
  this repo), also **mirror a copy** into `$HAL_VAULT/Areas/Code/_Session-Logs/` with the same
  filename and `project:` set to the repo's name, so the session surfaces on the Code
  dashboard and under the repo's hub note. (Mirror only the note — never the code.)

## Step 2 — Ask what to preserve
Offer a quick multi-select (the user can just say "all"): **key learnings · solutions &
fixes · decisions made · files modified · setup & config · pending tasks · errors &
workarounds**. Default to including whatever was clearly significant this session.

## Step 3 — Write the session log
Filename: `YYYY-MM-DD HHMM <short-slug>.md` (get the timestamp from `date "+%Y-%m-%d %H%M"`).
Use the `System/Templates/Session-Log.md` structure. Fill frontmatter
(`type: session`, `date`, `project`, `topics: [...]`). The **Quick Reference** block must be
genuinely scannable — that's what `/resume` reads first. Append the relevant raw conversation
under "Raw Session Log" for searchability.

## Step 4 — Offer to preserve permanent facts
If anything this session is permanent (a convention, an architecture decision, a key path),
ask whether to also run `/preserve` to write it into `CLAUDE.md`. Don't duplicate — session
logs are episodic; `CLAUDE.md` is durable truth.

## Step 5 — Confirm
Report the path written and a one-line summary. Done — nothing's lost.
