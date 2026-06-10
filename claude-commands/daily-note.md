---
description: Create or open today's daily note with priorities, meetings, and project links
allowed-tools: Bash, Read, Write, Glob
---

# /daily-note — Daily Note

Target file: `$HAL_VAULT/Calendar/Daily/YYYY-MM-DD.md` (today's date from `date +%F`).

## If it already exists
Open it, summarise what's there, and ask if anything needs updating (priorities, new tasks).

## If it doesn't exist
Create it from `$HAL_VAULT/System/Templates/Daily.md`, filling frontmatter (`type: daily`,
`date`). Then:
- **Top 3 priorities** — ask the user for these.
- **Today's Meetings** — leave the Dataview query (it auto-fills any meeting notes dated
  today). If Dataview isn't installed, instead list meeting notes in `Areas/Work/Meetings/`
  whose frontmatter `date` is today.
- **Active Projects** — keep the query, or list `type: project, status: active` notes.
- Carry forward any unfinished `- [ ]` tasks from yesterday's daily note if one exists.

Keep it clean and skimmable. End by confirming the path and reading back the 3 priorities.
