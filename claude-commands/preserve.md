---
description: Add a durable fact/decision/convention to CLAUDE.md (with auto-archive)
argument-hint: "[what to remember]"
allowed-tools: Bash, Read, Write, Edit
---

# /preserve — Update Permanent Memory

Add something Hal should **always** know to the project memory file.

Input: `$ARGUMENTS` (if empty, ask the user what to preserve).

## Step 1 — Pick the right memory file
Run `pwd` / `echo "$HAL_VAULT"`. Vault mode → `$HAL_VAULT/CLAUDE.md`. External project mode →
`./CLAUDE.md` (create from a minimal template if it doesn't exist).

## Step 2 — Add the fact in the right section
Slot it into the correct existing section — **Current Projects**, **Decisions Log**,
**Conventions**, **Key Paths**, or **Pending / Carry-Forward**. Keep it to one or two tight
lines. Don't restate things already in the file; update in place instead of duplicating.

## Step 3 — Auto-archive check (important)
Count the lines of the memory file (`wc -l`). **If it exceeds 280 lines:**
1. Identify safely-archivable content: completed projects, resolved pending items, old
   decisions superseded by newer ones, anything explicitly marked archivable.
2. **Never move** sections marked `[CORE — never archive]` (Approach, Key Paths,
   Skills/Commands, MCP Tools).
3. Move the archivable content to `CLAUDE-Archive.md` (in the same folder), prepended with a
   `## Archived YYYY-MM-DD` heading. Append, don't overwrite.
4. Leave the main file lean and current.

## Step 4 — Confirm
Show the one line you added and, if archiving ran, what moved and the new line count.
