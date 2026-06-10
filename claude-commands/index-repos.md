---
description: Flesh out the Code hub notes — summarise repos from their READMEs and wire up per-repo memory
argument-hint: "[repo name to limit to]"
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# /index-repos — Turn registered repos into a useful linked layer

`register-repos.sh` already created a hub note per repo in `Areas/Code/` (path, language,
remote, last commit). This command fills in the *understanding*: a real summary from each
repo's README, a how-to-run, and the wiring for per-repo memory + session-log mirroring.
**Never copies code into the vault** and **never reads secret files** (`.env`, `*.key`,
`*.pem`, credentials, `secrets*`).

## Step 0 — Load the queue
Read `$HAL_VAULT/Areas/Code/CODE-REPORT.md` (or glob `$HAL_VAULT/Areas/Code/*.md` for
`type: code-project`). The work queue = hub notes whose **What it is** still contains the
`<!-- /index-repos fills this -->` placeholder. If `$ARGUMENTS` names a repo, limit to it.
If there are no hub notes, tell the user to run `register-repos.sh` first and stop.

## Step 1 — Plan + confirm
List the repos to process (and their `path`). Note that local repos with a README get a
real summary; GitHub-only cards and repos without a README will be flagged for a manual
line. Get a go-ahead (offer to do just the active ones, or one language group, first).

## Step 2 — Summarise each repo (from its own files, read-only)
For each queued hub note, using its `path`:
1. Read the README (and only if helpful: `package.json`/`pyproject.toml`/`Cargo.toml`/
   `go.mod` for name+scripts, a top-level `docs/` index). **Skip anything that looks like a
   secret.** Read contents, never copy them into the vault.
2. Edit the hub note in place:
   - **What it is:** 2–4 plain sentences — what the project does and why it exists.
   - **Stack / Key files:** refine the auto-detected list into the real stack.
   - **How to run:** the actual install/run/test commands from the README (keep the
     `hal here` line too).
   - Set a better `status:` if obvious (`maintenance`/`archived`/`idea`) and tighten `tags`.
   Leave the user's own edits and the Dataview block untouched.
3. If there's no README/it's a GitHub card: add one honest line ("No README; appears to be
   …") and a `- [ ]` to document it. Don't invent details.

## Step 3 — Per-repo deep memory (`/init`)
The richest memory comes from Claude Code's built-in `/init`, run **inside** each repo
(it reads the codebase and writes a code-aware `CLAUDE.md` there). For each repo missing a
`CLAUDE.md`, tell the user the one-liner:

```bash
hal here   # in <repo path>, then:
/init
```

Offer to do it now for the repo the user is currently in (if `pwd` is one of them). Don't
try to `/init` every repo from the vault session — it must run in each repo's own directory,
and on the local model it's best done deliberately, one repo at a time. Mark done repos
(those that already have `CLAUDE.md`) so re-runs skip them.

## Step 4 — Session-log mirroring (so coding work shows on the brain dashboard)
For each local repo:
1. Ensure `CC-Session-Logs/` is git-ignored: if `.gitignore` exists and lacks it, append a
   line `CC-Session-Logs/`; if there's no `.gitignore`, create one with that line. (Confirm
   before touching a repo's tracked files.)
2. Confirm the mirror target exists: `$HAL_VAULT/Areas/Code/_Session-Logs/`.

From now on, when the user runs `/compress` inside a repo, the log is saved to the repo's
`CC-Session-Logs/` **and** copied into `Areas/Code/_Session-Logs/` with `project:` set to the
repo name — so it surfaces under that repo's hub note and on `Areas/Code/_Index.md`.

## Step 5 — Wrap up
- `Areas/Code/_Index.md` refreshes itself (Dataview) — no action needed.
- Update `$HAL_VAULT/CLAUDE.md` **Current Projects** with the active repos (name — one line —
  path), so they load with `/resume`.
- Report: notes summarised, repos still needing `/init`, anything without a README, and any
  GitHub-only cards. Remind the user they can re-run anytime — it's idempotent.
