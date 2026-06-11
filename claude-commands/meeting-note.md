---
description: Turn a transcript or rough notes into a structured meeting note
argument-hint: "[path to transcript | or paste notes]"
allowed-tools: Bash, Read, Write, Glob
---

# /meeting-note — Process a Meeting

Input: `$ARGUMENTS` — a path to a transcript/recording-transcript/notes file, or the user
will paste the raw notes. If neither is given, ask for the source and the project it relates to.

## Build the note
Create `$HAL_VAULT/Areas/Work/Meetings/YYYY-MM-DD <topic>.md` using
`$HAL_VAULT/System/Templates/Meeting.md`. Fill:
- **Frontmatter:** `type: meeting`, `date` (the meeting's date, ask if unclear),
  `project` (must match an existing project name so it surfaces on that project's page),
  `attendees: [...]`, `status: completed`.
- **Summary** — 3–6 sentences, what was discussed and why it matters.
- **Decisions** — concrete decisions made.
- **Action Items** — as `- [ ]` checkboxes, with owner names where known.
- **Notes** — supporting detail.

Getting `project` right is what makes the note auto-appear under that project's
"Related Meetings". Confirm the project name before saving. Report the saved path.
