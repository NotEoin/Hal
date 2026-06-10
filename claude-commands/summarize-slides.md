---
description: Generate lecture-note summaries for slide decks (PDF/DOCX/PPTX/HTML) that don't have one yet
argument-hint: "[course name to limit to]"
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# /summarize-slides — Summarise lecture slides into notes

Fill the gaps: for every slide deck that has **no summary note yet**, create a structured
lecture note from its extracted text. Decks that already have a summary are left alone.
Works for any extracted file type — PDF, DOCX, PPTX, HTML.

**Prerequisite:** run `prepare-import.sh` first (it runs every extractor — PDF, DOCX, PPTX,
HTML — caches the text and writes a combined `SLIDES-REPORT.md`). This command reads that —
it does **not** re-read the source files directly, which keeps it fast and works for the
text-only local model. (Running a single `extract-*.sh` also works if you only have one type.)

## Step 0 — Load the report
Read `$HAL_VAULT/_Import-Staging/SLIDES-REPORT.md`. The work queue = rows marked
**❌ none**. (Optionally also `🟡 maybe` if the user wants those reviewed.) If `$ARGUMENTS`
names a course, filter to that course only. If the report is missing, tell the user to run
`prepare-import.sh` and stop.

## Step 1 — Plan + confirm
Group the gap decks by course and show counts (e.g. "Algorithms: 11 decks, Databases: 7…").
Warn that summarising many decks on a local model takes time, and propose doing it
**course by course**. Get a go-ahead and an optional starting course.

## Step 2 — For each gap deck (batched per course)
1. **Skip if already done** (resumable): if any note in `Areas/Study/` has frontmatter
   `source_file:` equal to this deck, skip it. This makes re-runs safe.
2. Read the cached text from the path in the row's **Cache** column (it points at the right
   per-type cache, e.g. `_Import-Staging/.pdf-text/…` or `.docx-text/…`, `.pptx-text/…`,
   `.html-text/…`).
   - If the text is **very long** (won't fit comfortably in context), summarise in chunks
     (e.g. ~10-page blocks), then synthesise the chunk summaries into one note. Don't dump
     raw slide text into the note.
   - If status was `needs-OCR`/`empty`, skip and add it to a "couldn't read" list (suggest
     re-running `prepare-import.sh --ocr`).
3. **Write the note** to `$HAL_VAULT/Areas/Study/<Course>/<Deck Title>.md` using the
   `System/Templates/Lecture.md` shape. Frontmatter:
   ```yaml
   type: lecture
   date: <source file mtime, via: stat -c %y "<file>">
   course: <Course>
   source_file: <relpath of the deck>
   status: active
   tags: [lecture, <course-slug>, <topic>]
   ```
   Body: **Overview** (2-4 sentences), **Key Concepts** (bulleted, the substance of the
   deck), **Definitions / Formulas** where present, **Takeaways**, and **Follow-ups** as
   `- [ ]`. Write a genuinely useful summary, not a slide-by-slide transcript.
4. **Keep the source with the note:** copy the deck (whatever its extension) into
   `Areas/Study/<Course>/Slides/` and embed/link it at the bottom of the note
   (`![[<deck>.pdf]]` for PDFs, or a link for other types), so the source is one click away
   and `source_file` points to its new in-vault location.
5. After each course: report how many notes were created and pause for a quick check.

## Step 3 — Existing summaries
For `🟡 maybe` decks (a note mentions the file but it's unclear if it's a real summary): show
the candidate note and ask whether to (a) leave it, (b) enrich it with frontmatter +
`source_file`, or (c) regenerate. Never overwrite the user's own notes without confirmation.

## Step 4 — Wrap up
- Refresh `Areas/Study/_Index.md` is automatic (Dataview).
- Note the work in `$HAL_VAULT/CLAUDE.md` (courses + deck counts summarised).
- Report: notes created per course, decks skipped (already done), and any that couldn't be
  read (need OCR). Remind the user the staging copy stays until they confirm cleanup.
