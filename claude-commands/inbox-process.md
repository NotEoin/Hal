---
description: Triage the +Inbox folder — file each item to its proper home
allowed-tools: Bash, Read, Write, Edit, Glob
---

# /inbox-process — Process the Inbox

Clear `$HAL_VAULT/+Inbox/` by filing everything to where it belongs.

## Step 1 — List
List items in `$HAL_VAULT/+Inbox/` (ignore `README.md`). For each, read enough to classify it.

## Step 2 — Classify and propose
For each item decide: is it a **meeting**, **project note/update**, **personal/health note**,
**reference note**, **task**, or **trash**? Propose a destination and the frontmatter you'd add.
Present the full plan as a short list and ask for a quick yes / adjustments before moving
anything. Don't delete anything without explicit confirmation.

## Step 3 — File it
On approval:
- Add correct frontmatter (`type`, `date`, `project`, `tags`).
- Move to the right folder (`Areas/...`, `Calendar/...`, etc.) with a clean filename.
- For loose tasks, append them to today's daily note or the relevant project's Tasks section.

## Step 4 — Report
Summarise what went where and confirm the inbox is empty (or what's left and why).
