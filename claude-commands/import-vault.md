---
description: Reclassify a staged old vault into Areas/Study (+ other Areas) with frontmatter
argument-hint: "[staged vault name]"
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# /import-vault — Reclassify an imported vault

Turn a raw staged copy of an old vault into properly filed, frontmatter-tagged notes in
the Hal vault. Most content is lecture notes → **`Areas/Study/<Course>/`**; anything
clearly work/personal/health goes to the matching Area.

**Run `import-vault.sh /path/to/OldVault` first** — this command works on the staged copy,
never on your originals.

## Step 0 — Locate the staging + manifest
- `STAGING = $HAL_VAULT/_Import-Staging/<name>` (`<name>` = `$ARGUMENTS`, or auto-detect the
  single folder under `_Import-Staging/`).
- Read `STAGING/IMPORT-MANIFEST.md`. Note the totals and especially the **duplicate
  filenames** list — those are the link-safety risk.
- If no manifest exists, tell the user to run `import-vault.sh` first and stop.

## Step 1 — Propose a taxonomy mapping (confirm before moving anything)
From the manifest's top-level folders + a peek at a few notes in each, propose a table:

| Old folder | → Destination | type | year | module |
|---|---|---|---|---|
| `Algorithms` | `Areas/Study/<Year>/Algorithm Design - CS2010/` | lecture | 2024-25 | CS2010 |
| `Work notes` | `Areas/Work/` | note | — | — |
| `Misc` | (split — review individually) | note | — | — |

Rules of thumb: a folder that's clearly a course/module → `Areas/Study/<Year>/<Module - CODE>/`
with `type: lecture` (work out the **academic year** and **module** from the path / Canvas
`Course structure.md`, never leave things in a `Linked materials` folder); work things →
`Areas/Work/`; life/health → `Areas/Personal` / `Areas/Health`; genuinely loose notes →
`+Inbox/` for later triage. **Show the table and get a yes / edits before moving anything.**

## Step 2 — Resolve filename collisions
For each duplicate filename in the manifest, propose disambiguated names (e.g.
`Intro.md` → `Intro (Algorithms).md`). Obsidian resolves `[[links]]` by filename, so unique
names = links keep working after the move. Confirm the renames.

## Step 3 — Process folder by folder (batched)
Do **one source folder at a time** (keeps the local model fast and lets the user sanity-check
early). For each note in the folder:
1. Read it. Derive: `year`, `course`/module, `section` (week, if a `Course structure.md` says),
   `code`, sensible `tags`, and a `date` (from a date in the note, else mtime — `stat -c %y`).
2. Add/merge YAML frontmatter: `type` (lecture|note|exercise|…), `date`, `year`, `course`,
   `code`, `section`, `status: active`, `tags: [study, <code>, <role>]`. Don't clobber existing
   frontmatter — merge. Add the breadcrumb line under the `# title` (see Study filing & linking
   in `CLAUDE.md`): `*[[<University>]] › [[<Year> Index]] › [[<CODE> Index]] ›
   [[<CODE> Index#<Section>]]*`, where `<University>` is `$HAL_UNIVERSITY`.
3. Move the note (with any disambiguating rename) to `Areas/Study/<Year>/<Module - CODE>/`.
4. Move that note's **attachments** (images/pdfs it embeds) into the same destination so
   `![[embeds]]` keep resolving; flag any attachment-name collisions.
After each folder: report counts moved and pause briefly in case the user wants to adjust.

## Step 4 — Link safety + topic web
If the old vault used **relative/absolute** link format (ask the user, or detect `](../`
style links via grep), rewrite those markdown links to `[[wikilinks]]` so they survive.
Pure `[[wikilink]]` / `![[embed]]` vaults need no rewriting once filenames are unique.
Also wrap genuine reusable concepts in the note bodies as `[[wikilinks]]` and create stubs in
`Areas/Study/Topics/` for any that don't exist yet, so the vault is traversable by topic.

## Step 5 — Surface it
- Build/refresh the index spine so the graph is connected: each module's `<CODE> Index.md`
  (notes grouped by section in teaching order), each `<Year> Index.md`, and the root
  `Areas/Study/<$HAL_UNIVERSITY>.md`. The unattended runner does this automatically; when
  filing by hand, regenerate the indexes for every year/module you touched.
- Ensure `Areas/Study/_Index.md` exists (it ships with Hal). The dashboards auto-populate from
  frontmatter.

## Step 6 — Record + clean up
- Add a line to `$HAL_VAULT/CLAUDE.md` under Current Projects / Decisions noting the import
  (source, date, courses added).
- Leave `_Import-Staging/` in place. **Only after the user confirms** everything looks right
  in Obsidian (links intact), offer to delete the staging copy.

## Step 7 — Hand off to slides
If the import includes lecture-slide PDFs to summarise, tell the user to run
`extract-pdfs.sh` then `/summarize-slides` to fill in summaries for decks that don't have a
note yet.

Report a final summary: notes filed, courses created, collisions handled, anything skipped.
