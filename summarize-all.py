#!/usr/bin/env python3
"""
summarize-all.py — Hal's unattended, self-resuming lecture-note runner.

Reads `_Import-Staging/SLIDES-REPORT.md`, then for every deck/paper/exercise that
still needs a note it loads the cached text, asks the LOCAL model (Ollama) to write
a self-contained study note, and files it under
`Areas/Study/<Year>/<Course>/<Title>.md`.

What makes the notes useful (not a transcript, not a shallow blurb):
  * Each note is **self-contained study notes** — a short Summary up top, then the
    slide content *condensed into concise, interpretable notes* you can revise from
    without opening the original deck. The source is LINKED at the bottom, never
    pasted in.
  * Notes are organised **by year → module → section**, derived from the file's real
    Canvas path and `Course structure.md` (not the "Linked materials" folder it sat in).
  * Every note carries a breadcrumb that wikilinks the whole spine —
    [[University]] › [[<Year> Index]] › [[<CODE> Index]] › [[<CODE> Index#<Section>]] —
    and every reusable concept it mentions is linked inline as [[Topic]], with a matching
    stub auto-created under `Areas/Study/Topics/`. The module index (grouped by section in
    teaching order), the year hub, and the root MOC are rebuilt as it goes, so Obsidian's
    graph wires the whole degree together by wikilinks. The root's name comes from
    HAL_UNIVERSITY.

Why this is the right tool for hundreds/thousands of files:
  * Each deck is summarised in its OWN fresh model call — the 32K context window
    never fills up, so there's no auto-compaction and no drift.
  * Every finished note is written to disk immediately, and decks that already
    have a note are skipped. So the run is fully RESUMABLE: stop it whenever,
    start it again, and it picks up exactly where it left off.

It talks to you as Hal while it works, and writes itself a short session log at
the end so the job shows up in your second brain.

Usage:
  hal summarize                  # do everything outstanding, unattended (default)
  hal summarize --course NAME     # limit to one course (name, code, or folder)
  hal summarize --interactive     # pause for a nod before each course
  hal summarize --limit N         # only the next N decks (good for a trial run)
  hal summarize --include-maybe   # also revisit rows flagged 'maybe'
  hal summarize --model NAME       # use a different Ollama model
  hal summarize --mock            # wiring test: no model call, placeholder notes

You can also run it directly: python3 summarize-all.py [options]
"""

import argparse
import os
import re
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import hal_chat

# ---- Hal's terminal voice ---------------------------------------------------
BLUE = "\033[1;36m"; YELLOW = "\033[1;33m"; GREEN = "\033[1;32m"
RED = "\033[1;31m"; DIM = "\033[2m"; OFF = "\033[0m"
def hal(msg):  print(f"{BLUE}{msg}{OFF}", flush=True)
def ok(msg):   print(f"{GREEN}  ✓ {msg}{OFF}", flush=True)
def tick(msg): print(f"  {msg}", flush=True)
def warn(msg): print(f"{YELLOW}  ! {msg}{OFF}", flush=True)
def err(msg):  print(f"{RED}  ✗ {msg}{OFF}", flush=True)

HAL_SYSTEM = (
    "You are Hal, a concise, private study assistant running locally on the user's machine. "
    "You turn raw lecture material into self-contained study notes: a short summary, then the "
    "ideas condensed into clear, interpretable notes a student can revise from WITHOUT the "
    "original slides. You explain ideas in your own words — never a slide-by-slide transcript, "
    "and never a vague high-level blurb. Write clean Markdown. Stay grounded: if the source is "
    "thin, keep the note short rather than padding, and never invent facts the text doesn't support.\n\n"
    "The notes live in an Obsidian vault, so LINKING IS ESSENTIAL. Whenever you name a real, "
    "reusable concept, technique, algorithm, theorem, protocol, data structure or named tool "
    "(e.g. Von Neumann architecture, RSA, TCP handshake, dynamic programming), wrap that concept "
    "in [[wiki link]] brackets the FIRST time it appears in the note, using its plain canonical "
    "name (e.g. [[Dynamic programming]], not [[dynamic programming algorithms we saw]]). This lets "
    "the whole degree wire itself together by topic. Do NOT link ordinary words, the course name, "
    "or generic phrases — only genuine, page-worthy concepts. Link each concept once, not every time."
)

# Set by the SIGINT handler so we stop cleanly between decks (not mid-note).
STOP = False
def _on_sigint(signum, frame):
    global STOP
    STOP = True
    print()
    warn("Caught Ctrl-C — I'll stop after this deck. Everything finished is already saved.")
signal.signal(signal.SIGINT, _on_sigint)


# ---- Talking to the local model ---------------------------------------------
def call_model(messages, model, timeout=900, retries=2):
    """One isolated chat completion against the local model server."""
    return hal_chat.chat(messages, model, temperature=0.3, timeout=timeout,
                         retries=retries)


def clean_body(text):
    """Strip stray code fences / leading 'markdown' the model sometimes adds."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n", "", t)
        t = re.sub(r"\n```$", "", t.rstrip())
    return t.strip()


def summarise_text(title, course, role, text, model, mock=False):
    """Produce the note body. Map-reduce for long decks so nothing overflows."""
    if mock:
        return (
            f"## Summary\nConcise study notes for **{title}** ({course}). "
            "(--mock: no model was called — this is placeholder structure.)\n\n"
            "## Notes\n- The slide content, condensed into interpretable points in Hal's words, "
            "touching on [[Example Concept]] and [[Another Topic]].\n"
            "  - Sub-points expand on each idea so it's revisable without the deck.\n\n"
            "## Key terms & formulas\n- **[[Example Concept]]** — a short, grounded definition.\n\n"
            "## Takeaways\n- A few revision-ready bullets.\n"
            "- Re-run without --mock for the real note."
        )

    chunks = chunk_text(text, 11000)
    if len(chunks) > 1:
        partials = []
        for i, ch in enumerate(chunks, 1):
            tick(f"{DIM}· reading part {i}/{len(chunks)}…{OFF}")
            partials.append(call_model([
                {"role": "system", "content": HAL_SYSTEM},
                {"role": "user", "content":
                    f"This is part {i} of {len(chunks)} of the material for "
                    f'"{title}". Pull out the teaching points and condense them into clear '
                    f"study-note bullets in your own words (definitions and formulas too, if "
                    f"any). Don't transcribe:\n\n{ch}"},
            ], model))
        text = "\n".join(partials)  # synthesise from the condensed bullets

    body = call_model([
        {"role": "system", "content": HAL_SYSTEM},
        {"role": "user", "content": note_prompt(title, course, role, text)},
    ], model)
    return clean_body(body)


def note_prompt(title, course, role, text):
    role_hint = {
        "exercise": ("This is an exercise / problem sheet. Capture the problems it sets and the "
                     "method to approach them; include worked steps only where the text gives them."),
        "solution": ("This is a set of solutions. Capture each problem and its worked answer "
                     "concisely, so it reads as a study aid."),
        "exam": ("This is a past exam paper. Turn it into a revision checklist: the topics and "
                 "the style of questions asked. Do NOT invent answers the paper doesn't contain."),
    }.get(role, ("These are lecture slides. Condense them into clear, interpretable study notes — "
                 "explain the ideas in your own words, don't transcribe the slides."))
    return (
        f'Source material titled "{title}" from the course "{course}". {role_hint}\n\n'
        "Write a self-contained revision note in Markdown (no YAML front matter, no surrounding "
        "code fences) with these sections, in this order:\n\n"
        "## Summary\n2–4 sentences: what this covers and why it matters.\n\n"
        "## Notes\nThe actual content, condensed into concise, interpretable notes a student can "
        "revise from WITHOUT the original slides. Use short bullets and sub-bullets; explain each "
        "idea in your own words; keep a worked example only where it genuinely aids understanding. "
        "Do NOT transcribe slide-by-slide and do NOT pad. Wrap each genuine, reusable concept in "
        "[[wiki links]] the first time it appears (see the linking rule above).\n\n"
        "## Key terms & formulas\nDefinitions and formulas that appear in the source, each as "
        "`**term** — meaning`. Wrap the term in [[wiki links]] when it is a reusable concept. "
        "Omit this heading entirely if there are none.\n\n"
        "## Takeaways\n3–6 bullets worth remembering for revision.\n\n"
        "Ground everything in the source text below; never invent facts it doesn't support. If the "
        "source is thin, write a short note rather than padding it out.\n\n"
        f'Source text:\n"""\n{text}\n"""'
    )


def chunk_text(text, size):
    if len(text) <= size:
        return [text]
    out, buf = [], ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 > size and buf:
            out.append(buf); buf = ""
        buf += para + "\n\n"
        while len(buf) > size:                 # a single huge paragraph
            out.append(buf[:size]); buf = buf[size:]
    if buf.strip():
        out.append(buf)
    return out


# ---- Path → Year / Course / Code / Role -------------------------------------
_NAV_FOLDERS = {"linked materials", "pages", "slides", "files", "resources", "attachments"}

def parse_year_course(relpath):
    """Derive (year, course_folder) from a staging-relative path.

    Shape A: canvas_downloads/<year>/<Course - CODE>/Linked materials/file.pdf
    Shape B: .../year N/<Course>/...
    Fallback: the deepest meaningful folder above the file.
    """
    parts = Path(relpath).parts
    if "canvas_downloads" in parts:
        i = parts.index("canvas_downloads")
        year = parts[i + 1] if len(parts) > i + 1 else "Misc"
        course = parts[i + 2] if len(parts) > i + 2 else "Misc"
        return year, course
    for j, p in enumerate(parts):
        if re.match(r"(?i)^year\s*\d+$", p.strip()):
            year = p.strip()
            course = parts[j + 1] if len(parts) > j + 1 else "Misc"
            return year, course
    folders = [p for p in parts[:-1] if p.lower() not in _NAV_FOLDERS]
    course = folders[-1] if folders else "Misc"
    return "Misc", course


def course_code(course):
    m = re.search(r"\b([A-Z]{2,4}\d{3,4})\b", course)
    return m.group(1) if m else ""


def clean_course_name(course):
    name = re.sub(r"\s*[-–]\s*[A-Z]{2,4}\d{3,4}\s*$", "", course).strip()
    return name or course


def course_slug(course):
    return re.sub(r"[^a-z0-9]+", "-", course.lower()).strip("-") or "misc"


def classify_role(filename):
    n = filename.lower()
    if "exam" in n:
        return "exam"
    if "solution" in n or "answers" in n or "sample answer" in n:
        return "solution"
    if any(k in n for k in ("exercise", "sheet", "tutorial", "lab ", " lab", "practical",
                            "worksheet", "problem")):
        return "exercise"
    if "handout" in n:
        return "handout"
    return "lecture"


def dedup_base(filename):
    """Normalised stem used to spot a handout that duplicates a fuller deck."""
    n = Path(filename).stem.lower()
    n = re.sub(r"(?i)hand\s*outs?", "", n)
    n = re.sub(r"\(\s*\d+\s*\)", "", n)        # "(1)" copy markers
    n = re.sub(r"[\s_-]+\d+$", "", n)          # trailing copy/version number
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n


def clean_title(stem, code):
    t = stem
    if code and t.lower().startswith(code.lower()):
        t = t[len(code):]
    t = re.sub(r"[_]+", " ", t)
    t = re.sub(r"(?i)\bhand\s*outs?\b", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -_")
    return t or stem


def index_link(code, cname):
    return f"{code} Index" if code else f"{cname} Index"


# Root of the whole course hierarchy: University → Year → Module → Section → Note.
# Set HAL_UNIVERSITY in hal.conf to your own institution; it names the root MOC and
# the first hop of every breadcrumb. Kept out of the repo because it is yours, not the
# project's.
UNIVERSITY = os.environ.get("HAL_UNIVERSITY", "University")


# ---- Inline topic links → auto-created stub topic notes ----------------------
# Wikilinks that are part of the navigation spine, not real concepts. We never
# turn these into "topic" stubs.
def _is_structural_link(target):
    t = target.strip()
    low = t.lower()
    return (
        t == UNIVERSITY
        or low.endswith(" index")
        or "#" in t                      # heading link into an index
        or re.match(r"(?i)^\d{4}-\d{2}$", t)        # a year like 2025-26
        or re.match(r"(?i)^year\s*\d+$", t)
    )


def extract_topic_links(body):
    """Pull the concept names out of [[Topic]] / [[Topic|alias]] in a note body."""
    found = []
    for m in re.finditer(r"\[\[([^\]]+)\]\]", body):
        target = m.group(1).split("|", 1)[0].split("#", 1)[0].strip()
        if target and not _is_structural_link(target):
            found.append(target)
    return found


def _topic_title(name):
    """Canonicalise a concept link to a stable note title (sentence case-ish)."""
    n = re.sub(r"\s+", " ", name).strip().strip(".,;:")
    n = n.replace("/", "-").replace("\\", "-")          # filesystem-safe
    return n


def ensure_topic_stub(vault, name):
    """Create a lightweight stub topic note if one doesn't already exist.

    Stubs make every inline [[concept]] resolve, so Obsidian's graph + backlinks
    panel surface everywhere the concept is used across the whole degree. The
    stub is intentionally tiny — Hal (or the user) can flesh it out later, and
    the backlinks do the heavy lifting for traversal.
    """
    title = _topic_title(name)
    if not title:
        return None
    topics_dir = vault / "Areas" / "Study" / "Topics"
    topics_dir.mkdir(parents=True, exist_ok=True)
    # case-insensitive existence check so we don't make near-duplicate stubs
    want = title.lower()
    for existing in topics_dir.glob("*.md"):
        if existing.stem.lower() == want:
            return existing
    path = topics_dir / f"{title}.md"
    path.write_text(
        "\n".join([
            "---", "type: topic", f"date: {datetime.now():%Y-%m-%d}",
            f"tags: [topic, {course_slug(title)}]", "status: stub", "---", "",
            f"# {title}", "",
            "> Concept stub, auto-created by Hal. Every note that references "
            f"**{title}** links here, so the backlinks panel below is the live "
            "map of where it shows up across your modules.", "",
            "## Notes", "",
            "_(empty — Hal or you can flesh this out; the links already work.)_", "",
            "## Appears in", "",
            "```dataview",
            "LIST",
            'FROM [[]]',
            "SORT file.name ASC",
            "```", "",
        ]) + "\n",
        encoding="utf-8",
    )
    return path


def sync_topic_stubs(vault, body):
    """Create stubs for any concept links found in a freshly written note body."""
    made = 0
    for name in dict.fromkeys(extract_topic_links(body)):   # dedup, keep order
        p = ensure_topic_stub(vault, name)
        if p and p.stat().st_mtime >= time.time() - 2:      # just created
            made += 1
    return made


# ---- Reading the report -----------------------------------------------------
def parse_report(report_path, vault):
    """Return (scan_dir, [rows]). Each row: dict(file, course, pages, text, summary, cache)."""
    lines = report_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    scan_dir = None
    rows = []
    for ln in lines:
        if scan_dir is None and "Scanned:" in ln:
            m = re.search(r"Scanned:\s*`([^`]+)`", ln)
            if m:
                scan_dir = m.group(1)
        if not ln.startswith("| "):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) != 6:
            continue
        if cells[0] in ("File", "HTML Page") or set(cells[0]) <= {"-", " ", ":"}:
            continue
        if cells[0].startswith("_(") or cells[0].startswith("*("):
            continue
        rows.append({
            "file": cells[0], "course": cells[1], "pages": cells[2],
            "text": cells[3], "summary": cells[4],
            "cache": cells[5].strip("`"),
        })
    return scan_dir, rows


# ---- Resume index: what's already summarised --------------------------------
def read_frontmatter(md_path):
    out = {}
    try:
        head = md_path.read_text(encoding="utf-8", errors="ignore")[:2000]
    except OSError:
        return out
    if not head.startswith("---"):
        return out
    end = head.find("\n---", 3)
    fm = head[3:end] if end != -1 else head
    for line in fm.splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip().strip('"').strip("'")
    return out


def build_done_index(vault):
    """Decks already summarised, by source basename, from existing Study notes."""
    done = set()
    study = vault / "Areas" / "Study"
    if not study.is_dir():
        return done
    for md in study.rglob("*.md"):
        src = read_frontmatter(md).get("source_file")
        if src:
            done.add(Path(src).name.lower())
    return done


# ---- Writing one note -------------------------------------------------------
def safe_mtime_date(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return datetime.now().strftime("%Y-%m-%d")


def write_note(vault, row, scan_dir, body):
    year = row["year"]
    course_folder = row["course_folder"]
    code = row["code"]
    cname = clean_course_name(course_folder)
    role = row["role"]
    src_name = Path(row["file"]).name
    title = clean_title(Path(row["file"]).stem, code)

    dest_dir = vault / "Areas" / "Study" / year / course_folder
    slides_dir = dest_dir / "Slides"
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Copy the source alongside the note (best effort) and LINK it (never embed/paste).
    src_abs = Path(scan_dir) / row["file"] if scan_dir else None
    in_vault_src = None
    if src_abs and src_abs.is_file():
        slides_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src_abs, slides_dir / src_name)
            in_vault_src = f"Areas/Study/{year}/{course_folder}/Slides/{src_name}"
        except OSError:
            in_vault_src = None

    date = safe_mtime_date(src_abs) if (src_abs and src_abs.is_file()) \
        else datetime.now().strftime("%Y-%m-%d")
    note_type = "lecture" if role == "lecture" else "note"
    tagslug = code.lower() if code else course_slug(course_folder)
    section = row.get("section", "")
    idx = index_link(code, cname)

    note_path = dest_dir / f"{title}.md"
    n = 2
    while note_path.exists():
        note_path = dest_dir / f"{title} ({n}).md"; n += 1

    fm = [
        "---",
        f"type: {note_type}",
        f"date: {date}",
        f"year: {year}",
        f"course: {cname}",
        f"code: {code}",
        f"section: {section}",
        f"role: {role}",
        f"source_file: {in_vault_src or row['file']}",
        "status: active",
        f"tags: [study, {tagslug}, {role}]",
        "---",
        "",
        f"# {title}",
    ]
    # Breadcrumb: University › Year › Module › Section — every hop a wikilink, so
    # the whole degree is traversable from any note.
    crumb = f"*[[{UNIVERSITY}]] › [[{year} Index|{year}]] › [[{idx}|{cname}]]"
    if section:
        crumb += f" › [[{idx}#{section_anchor(section)}|{section}]]"
    crumb += f" · {role}*"
    fm += [crumb, ""]

    footer = f"\n\n---\n*Part of [[{idx}|{cname}]]"
    if section:
        footer += f" · [[{idx}#{section_anchor(section)}|{section}]]"
    footer += "*"
    if in_vault_src:
        footer += f"  ·  **Source:** [[{src_name}]]"
    footer += "\n"
    note_path.write_text("\n".join(fm) + body.strip() + footer, encoding="utf-8")

    # Auto-create stub topic notes for every concept the body links to.
    sync_topic_stubs(vault, body)
    return note_path


# ---- Course map: sections + which file/page belongs to each -----------------
def _norm_member(name):
    """Normalise a filename/page title for matching (lowercase, no ext, alnum only)."""
    n = Path(name).stem if "." in name else name
    return re.sub(r"[^a-z0-9]+", "", n.lower())


def parse_course_map(scan_dir, year, course_folder):
    """Parse the Canvas `Course structure.md` into section info.

    Returns dict with:
      order   — ordered list of section names (the `## ...` headings)
      members — { normalised file/page name : section name }
    Sections give us the year→module→SECTION dimension the user wants, in the
    real Canvas teaching order, mapping each deck/page to where it belongs.
    """
    empty = {"order": [], "members": {}}
    if not scan_dir:
        return empty
    p = Path(scan_dir) / "canvas_downloads" / year / course_folder / "Course structure.md"
    if not p.is_file():
        return empty
    order, members = [], {}
    current = None
    for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        h = re.match(r"^##\s+(.*\S)\s*$", ln)
        if h:
            current = h.group(1).strip()
            if current not in order:
                order.append(current)
            continue
        # item lines like:  - [File] Foo.pdf   /   - [Page] Some Title
        m = re.search(r"\[(File|Page|Quiz)\]\s+(.*\S)\s*$", ln)
        if m and current:
            key = _norm_member(m.group(2))
            if key and key not in members:
                members[key] = current
    return {"order": order, "members": members}


def section_for(course_map, filename):
    """Best-effort: which section does this file belong to? '' if unknown."""
    members = course_map.get("members", {})
    if not members:
        return ""
    key = _norm_member(filename)
    if key in members:
        return members[key]
    # Containment either way (Canvas often references a deck by a shortened name).
    # Require a long shared stem so we don't false-match on a short common word —
    # prefer the longest such member so the most specific reference wins.
    best = ("", "")
    for mkey, sect in members.items():
        if len(mkey) >= 8 and (mkey in key or key in mkey):
            shared = min(len(mkey), len(key))
            if shared > len(best[0]):
                best = (mkey, sect)
    return best[1]


def section_anchor(section):
    """An Obsidian heading-link anchor target for a section name."""
    return section.replace("[", "(").replace("]", ")").replace("|", "-").replace("#", "")


_ROLE_GROUPS = [
    ("lecture", "Lectures"),
    ("exercise", "Exercises & sheets"),
    ("solution", "Solutions"),
    ("exam", "Past exams"),
    ("handout", "Handouts"),
    ("note", "Other notes"),
]

def build_course_index(vault, year, course_folder, scan_dir):
    """(Re)generate the course's master link page from disk + Course structure.md.

    Notes are grouped by SECTION in real Canvas teaching order (the section
    headings double as wikilink targets for each note's breadcrumb), with a
    role-grouped fallback for anything that didn't map to a section.
    """
    course_dir = vault / "Areas" / "Study" / year / course_folder
    if not course_dir.is_dir():
        return None
    code = course_code(course_folder)
    cname = clean_course_name(course_folder)
    idx_name = f"{index_link(code, cname)}.md"
    idx_path = course_dir / idx_name

    cmap = parse_course_map(scan_dir, year, course_folder)
    section_order = cmap["order"]

    by_section = {}                       # section name -> [stems]
    no_section = {key: [] for key, _ in _ROLE_GROUPS}
    total = 0
    for md in sorted(course_dir.glob("*.md")):
        if md.name == idx_name:
            continue
        fm = read_frontmatter(md)
        if fm.get("type") == "index":
            continue
        total += 1
        sect = (fm.get("section") or "").strip()
        if sect:
            by_section.setdefault(sect, []).append(md.stem)
        else:
            role = fm.get("role", "lecture")
            no_section.setdefault(role, no_section.get("note")).append(md.stem)

    lines = [
        "---", "type: index", f"date: {datetime.now():%Y-%m-%d}",
        f"year: {year}", f"course: {cname}", f"code: {code}",
        f"tags: [index, study, {code.lower() if code else course_slug(course_folder)}]",
        "---", "",
        f"# {cname}" + (f" ({code})" if code else "") + " — Index", "",
        f"*[[{UNIVERSITY}]] › [[{year} Index|{year}]]*", "",
        f"**{total}** note(s) filed in this module.", "",
    ]

    # Sections in Canvas teaching order first, then any extra sections we saw.
    ordered = [s for s in section_order if s in by_section]
    ordered += [s for s in by_section if s not in ordered]
    if ordered:
        lines += ["## By section (teaching order)", ""]
        for sect in ordered:
            lines += [f"### {section_anchor(sect)}", ""]
            lines += [f"- [[{stem}]]" for stem in sorted(by_section[sect])]
            lines += [""]

    leftover = sum(len(v) for v in no_section.values())
    if leftover:
        lines += ["## Other / unsorted", ""]
        for key, heading in _ROLE_GROUPS:
            items = no_section.get(key) or []
            if not items:
                continue
            lines += [f"### {heading}", ""]
            lines += [f"- [[{stem}]]" for stem in sorted(items)]
            lines += [""]

    if total == 0:
        lines += ["_No notes yet — run `hal summarize` to populate this course._", ""]

    idx_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return idx_path


def build_year_index(vault, year):
    """(Re)generate a year hub that links to each course index under it."""
    ydir = vault / "Areas" / "Study" / year
    if not ydir.is_dir():
        return None
    idx_path = ydir / f"{year} Index.md"
    courses = []
    for cdir in sorted(p for p in ydir.iterdir() if p.is_dir()):
        code = course_code(cdir.name)
        cname = clean_course_name(cdir.name)
        courses.append((index_link(code, cname), cname, code))
    lines = [
        "---", "type: index", f"date: {datetime.now():%Y-%m-%d}",
        f"year: {year}", "tags: [index, study, year]", "---", "",
        f"# {year} — Courses", "",
        f"*[[{UNIVERSITY}]] · [[_Index|Study]]*", "",
    ]
    for link, cname, code in courses:
        label = f"{cname} ({code})" if code else cname
        lines.append(f"- [[{link}|{label}]]")
    if not courses:
        lines.append("_No courses yet._")
    idx_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return idx_path


def build_university_index(vault):
    """(Re)generate the root MOC that ties every academic year together.

    This is the top of the spine: the institution named by HAL_UNIVERSITY → each
    Year Index → each Module Index → Section → Note. Linking up to it from the year
    hubs makes the whole degree one connected graph.
    """
    study = vault / "Areas" / "Study"
    if not study.is_dir():
        return None
    years = sorted(
        p.name for p in study.iterdir()
        if p.is_dir() and re.match(r"(?i)^(year\s*\d+|\d{4}-\d{2}|\d{4})$", p.name.strip())
    )
    idx_path = study / f"{UNIVERSITY}.md"
    lines = [
        "---", "type: index", f"date: {datetime.now():%Y-%m-%d}",
        "tags: [index, study, university]", "---", "",
        f"# {UNIVERSITY}", "",
        "*Root of the study hierarchy: University › Academic year › Module › Section › Note.*",
        "", "## Academic years", "",
    ]
    for y in years:
        lines.append(f"- [[{y} Index|{y}]]")
    if not years:
        lines.append("_No years yet — run `hal summarize` to populate._")
    lines += [
        "", "## All modules",
        "", "```dataview",
        "TABLE code AS Code, year AS Year",
        'FROM "Areas/Study"',
        'WHERE type = "index" AND code',
        "SORT year ASC, code ASC", "```",
        "", "## Topic web",
        "", "```dataview",
        "LIST",
        'FROM "Areas/Study/Topics"',
        "SORT file.name ASC", "```", "",
    ]
    idx_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return idx_path


# ---- Session log so the work lands in the second brain ----------------------
def write_session_log(vault, created, skipped, failed, unreadable, deduped, per_course, started):
    try:
        logdir = vault / "Areas" / "Work" / "Session-Logs"
        logdir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        path = logdir / f"{now:%Y-%m-%d} Slide summarisation {now:%H%M}.md"
        lines = [
            "---", "type: session", f"date: {now:%Y-%m-%d}",
            "tags: [session, study, slides]", "---", "",
            "# Slide summarisation run", "",
            f"Ran the unattended summariser from {started:%H:%M} to {now:%H:%M}.", "",
            f"- Notes created: **{created}**",
            f"- Skipped (already done): **{skipped}**",
            f"- Handout duplicates skipped: **{deduped}**",
            f"- Couldn't read (need OCR): **{unreadable}**",
            f"- Failed: **{failed}**", "",
            "## Per course", "",
        ]
        for c, n in sorted(per_course.items()):
            lines.append(f"- {c}: {n} new")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
    except OSError:
        return None


# ---- Main -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(add_help=True, description="Hal's self-resuming slide summariser.")
    ap.add_argument("report", nargs="?", help="path to SLIDES-REPORT.md")
    ap.add_argument("--course", help="limit to one course (name, code, or folder)")
    ap.add_argument("--interactive", action="store_true", help="pause before each course")
    ap.add_argument("--limit", type=int, default=0, help="only the next N decks")
    ap.add_argument("--include-maybe", action="store_true", help="also do 'maybe' rows")
    # Empty in the cloud profile; main() rejects that below rather than calling Ollama
    # with a blank model name.
    ap.add_argument("--model", default=os.environ.get("HAL_MODEL", ""))
    ap.add_argument("--mock", action="store_true", help="no model call; write placeholder notes")
    args = ap.parse_args()

    if not args.model and not args.mock:
        err("No local model configured — 'hal summarize' grinds through hundreds of slide "
            "decks and is built for a local model.")
        hal("Either run ./setup.sh with a local profile (24gb/12gb), or pass --model NAME "
            "to point at an Ollama model yourself. Use --mock to test the wiring.")
        sys.exit(1)

    vault = Path(os.environ.get("HAL_VAULT", "")).expanduser()
    if args.report:
        report = Path(args.report).expanduser().resolve()
        if not vault:
            vault = report.parent.parent  # …/_Import-Staging/REPORT.md -> vault
    else:
        if not vault:
            err("I don't know where your vault is. Set HAL_VAULT or pass the report path.")
            sys.exit(1)
        report = vault / "_Import-Staging" / "SLIDES-REPORT.md"
    vault = vault.resolve()

    if not report.is_file():
        err(f"No report at {report}.")
        hal("Run  ./prepare-import.sh  first so I have something to work from.")
        sys.exit(1)

    scan_dir, rows = parse_report(report, vault)

    # Annotate every row with its real year/course/code/role/section from the PATH
    # and the course's Canvas structure map (cached per course).
    _map_cache = {}
    for r in rows:
        year, course_folder = parse_year_course(r["file"])
        r["year"] = year
        r["course_folder"] = course_folder
        r["code"] = course_code(course_folder)
        r["role"] = classify_role(Path(r["file"]).name)
        key = (year, course_folder)
        if key not in _map_cache:
            _map_cache[key] = parse_course_map(scan_dir, year, course_folder)
        r["section"] = section_for(_map_cache[key], Path(r["file"]).name)

    # Build the work queue: gap rows with usable text.
    queue = []
    unreadable = 0
    for r in rows:
        is_gap = "❌" in r["summary"]
        is_maybe = "🟡" in r["summary"]
        # A "maybe" that only points at the structure/report files isn't a real
        # human summary — treat it as a gap.
        false_maybe = is_maybe and ("Course structure.md" in r["summary"]
                                    or "SLIDES-REPORT.md" in r["summary"])
        if not (is_gap or false_maybe or (args.include_maybe and is_maybe)):
            continue
        if args.course:
            hay = f"{r['course_folder']} {clean_course_name(r['course_folder'])} {r['code']}".lower()
            if args.course.lower() not in hay:
                continue
        if r["text"] not in ("text", "ocr"):
            unreadable += 1
            continue
        # Skip pure navigation/pointer HTML pages (they only link to the real deck).
        if r["file"].lower().endswith(".html") and "lecture slides" in r["file"].lower():
            continue
        queue.append(r)

    # Skip handouts that just duplicate a fuller deck in the same course.
    nonhandout_bases = {}
    for r in queue:
        if r["role"] != "handout":
            nonhandout_bases.setdefault((r["year"], r["course_folder"]), set()).add(
                dedup_base(r["file"]))
    deduped = 0
    pruned = []
    for r in queue:
        if r["role"] == "handout":
            twins = nonhandout_bases.get((r["year"], r["course_folder"]), set())
            if dedup_base(r["file"]) in twins:
                deduped += 1
                continue
        pruned.append(r)
    queue = pruned

    done = build_done_index(vault)
    todo = [r for r in queue if Path(r["file"]).name.lower() not in done]

    # Greeting.
    print()
    hal("Hal here. Let me pick up the slide summaries.")
    touched_courses = sorted({clean_course_name(r["course_folder"]) for r in todo})
    if not todo:
        ok("Nothing outstanding — every readable deck already has a note.")
        if deduped:
            tick(f"{DIM}({deduped} handout duplicate(s) skipped.){OFF}")
        if unreadable:
            warn(f"({unreadable} deck(s) are image-only — re-run prepare-import.sh --ocr to read them.)")
        return
    tick(f"{len(todo)} item(s) still need a note, across {len(touched_courses)} course(s). "
         f"{len(queue) - len(todo)} already done.")
    if deduped:
        tick(f"{DIM}Skipping {deduped} handout duplicate(s) of fuller decks.{OFF}")
    if unreadable:
        warn(f"{unreadable} deck(s) are image-only (need OCR) — I'll list them at the end.")
    tick(f"{DIM}I save each note as I go, so you can stop me anytime (Ctrl-C) and just "
         f"run me again to continue.{OFF}")

    if args.limit:
        todo = todo[:args.limit]
        tick(f"{DIM}(--limit {args.limit}: I'll do the next {len(todo)} and stop.){OFF}")

    created = failed = 0
    per_course = {}
    started = datetime.now()

    # Group by (year, course) so it reads nicely and indexes build cleanly.
    by_course = {}
    for r in todo:
        by_course.setdefault((r["year"], r["course_folder"]), []).append(r)

    touched_years = set()
    for (year, course_folder), decks in by_course.items():
        if STOP:
            break
        cname = clean_course_name(course_folder)
        touched_years.add(year)
        print()
        hal(f"→ {cname}  ({year}) — {len(decks)} item(s).")
        if args.interactive:
            try:
                ans = input("   Start this course? [Y/n] ").strip().lower()
            except EOFError:
                ans = "y"
            if ans in ("n", "no", "q", "quit"):
                tick("Skipping for now."); continue

        for r in decks:
            if STOP:
                break
            title = clean_title(Path(r["file"]).stem, r["code"])
            cache_abs = (vault / r["cache"]).resolve()
            if not cache_abs.is_file():
                warn(f"{title}: cached text missing — skipping."); failed += 1; continue
            text = cache_abs.read_text(encoding="utf-8", errors="ignore").strip()
            if len(text) < 20:
                warn(f"{title}: barely any text — skipping (needs OCR)."); unreadable += 1; continue
            try:
                t0 = time.time()
                body = summarise_text(title, cname, r["role"], text, args.model, mock=args.mock)
                if not body or len(body) < 20:
                    raise RuntimeError("model returned almost nothing")
                note = write_note(vault, r, scan_dir, body)
                created += 1; per_course[cname] = per_course.get(cname, 0) + 1
                ok(f"{title}  {DIM}({time.time()-t0:.0f}s → {note.relative_to(vault)}){OFF}")
            except Exception as e:                       # noqa: BLE001 — keep grinding
                failed += 1
                err(f"{title}: {e}")

        # Refresh this course's master index from whatever's now on disk (resumable).
        idx = build_course_index(vault, year, course_folder, scan_dir)
        if idx:
            tick(f"{DIM}index → {idx.relative_to(vault)}{OFF}")

    # Refresh the year hubs we touched, then the university root MOC on top.
    for year in sorted(touched_years):
        build_year_index(vault, year)
    if touched_years:
        build_university_index(vault)

    # Wrap-up, in Hal's voice.
    print()
    elapsed = datetime.now() - started
    mins = int(elapsed.total_seconds() // 60)
    if STOP:
        hal("Paused. Here's where we got to:")
    else:
        hal("All done. Here's the tally:")
    tick(f"Notes written this run: {GREEN}{created}{OFF}")
    tick(f"Already done (skipped): {len(queue) - len(todo) if not args.limit else '—'}")
    if deduped:
        tick(f"Handout duplicates skipped: {deduped}")
    if unreadable:
        tick(f"Couldn't read (need OCR): {YELLOW}{unreadable}{OFF}")
    if failed:
        tick(f"Failed (will retry next run): {RED}{failed}{OFF}")
    tick(f"{DIM}Time: ~{mins} min{OFF}")

    log = write_session_log(vault, created, len(queue) - len(todo), failed, unreadable,
                            deduped, per_course, started)
    if log:
        tick(f"{DIM}Logged this run to {log.relative_to(vault)}{OFF}")

    remaining = len(todo) - created - failed if not args.limit else None
    if STOP or failed or (remaining and remaining > 0):
        hal("Run me again whenever — I'll skip everything that's done and carry on.")
    else:
        hal("Your Study dashboard (Areas/Study/_Index.md) is up to date. — Hal")


if __name__ == "__main__":
    main()
