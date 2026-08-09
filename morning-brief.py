#!/usr/bin/env python3
"""
morning-brief.py — Hal's local, private daily brief.

Assembles your day straight from the vault — open tasks & today's deadlines, what you got
done yesterday, the state of your active projects and last work session, and a "concept of the
day" pulled from your topic graph — then (optionally) asks the LOCAL model to write it up as a
warm, tight brief and drops it into today's daily note.

Fully local and cron-friendly: it talks only to Ollama on localhost and reads/writes files on
disk, so you can fire it at 07:00 with cron / launchd / Task Scheduler and read the brief in
today's daily note (and in your terminal) when you sit down. No cloud, nothing leaves the box.

Each run is its OWN single model call on a small packet, so the 32K context window never fills
and there's no drift. Re-running the same day just refreshes the brief block in place.

Usage:
  hal brief                 # build today's brief, write it into the daily note, print it
  hal brief --print-only    # just print it; don't touch the daily note
  hal brief --no-llm        # deterministic packet only (no model call) — great for cron-debug
  hal brief --date 2026-06-11
  hal brief --model NAME     # use a different Ollama model
  hal brief --mock          # alias for --no-llm (wiring test)

You can also run it directly: python3 morning-brief.py [options]
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

# ---- Hal's terminal voice ---------------------------------------------------
BLUE = "\033[1;36m"; YELLOW = "\033[1;33m"; GREEN = "\033[1;32m"
RED = "\033[1;31m"; DIM = "\033[2m"; OFF = "\033[0m"
def hal(m):  print(f"{BLUE}{m}{OFF}", flush=True)
def ok(m):   print(f"{GREEN}  ✓ {m}{OFF}", flush=True)
def tick(m): print(f"  {m}", flush=True)
def warn(m): print(f"{YELLOW}  ! {m}{OFF}", flush=True)
def err(m):  print(f"{RED}  ✗ {m}{OFF}", flush=True)

# Deliberately HAL_OLLAMA_URL, not ANTHROPIC_BASE_URL: in the cloud profile the latter
# points at Anthropic (or is unset), and this endpoint is Ollama's own /api/chat.
OLLAMA_URL = os.environ.get("HAL_OLLAMA_URL", "http://localhost:11434").rstrip("/")

HAL_BRIEF_SYSTEM = (
    "You are Hal, the user's private daily assistant running locally on their machine. You are "
    "given a structured PACKET of facts pulled from their Obsidian vault this morning: "
    "yesterday's wins, open tasks and today's deadlines, the state of their projects and last "
    "work session, and a 'concept of the day' from their study graph. Turn it into a SHORT, warm "
    "morning brief they can read in under a minute. Be concrete and specific — use the real "
    "task text, project names and dates from the packet; never invent items that aren't there. "
    "Lead with a one-line greeting, then tight sections with small headings. Keep Obsidian "
    "[[wiki links]] and `- [ ]` task checkboxes EXACTLY as given so they stay clickable in "
    "Obsidian. If a section of the packet is empty, omit it rather than padding. Write clean "
    "Markdown, no preamble, no sign-off."
)

BRIEF_HDR = "## ☀️ Morning Brief"
BRIEF_START = "<!-- HAL-BRIEF:START -->"
BRIEF_END = "<!-- HAL-BRIEF:END -->"
TASK_RE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
H_RE = re.compile(r"^#{1,6}\s+\S")
H1_RE = re.compile(r"^#\s+\S")


# ---- Talking to the local model ---------------------------------------------
def call_model(messages, model, timeout=300, retries=2):
    """One isolated chat completion against the local Ollama endpoint."""
    body = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": 0.4},
    }).encode("utf-8")
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/chat", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            return (data.get("message", {}).get("content") or "").strip()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = e
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"model call failed after {retries + 1} tries: {last}")


# ---- Small vault helpers ----------------------------------------------------
def read_text(p):
    try:
        return Path(p).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def frontmatter(text):
    """Parse the leading --- YAML block into a flat dict of strings."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fm = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip()
    return fm


def section(text, header):
    """Lines under the first '## header' (case-insensitive, substring) until the next heading."""
    hdr = header.lower()
    out = []
    cap = False
    for ln in text.splitlines():
        s = ln.strip()
        if H_RE.match(s):
            if cap:
                break
            name = re.sub(r"^#{1,6}\s+", "", s).lower()
            cap = hdr in name
            continue
        if cap:
            out.append(ln)
    return "\n".join(out).strip()


def walk_md(root):
    root = Path(root)
    if not root.exists():
        return
    for dp, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".md"):
                yield Path(dp) / f


# ---- The four packet sections ----------------------------------------------
def gather_tasks(vault, today_iso):
    """Unchecked `- [ ]` tasks across Areas/Calendar/+Inbox (excluding Study).
    A task with an embedded date <= today is treated as due/overdue."""
    study = str(vault / "Areas" / "Study")
    open_tasks, due_today = [], []
    for root in (vault / "Areas", vault / "Calendar", vault / "+Inbox"):
        for p in walk_md(root):
            if study in str(p):
                continue
            for ln in read_text(p).splitlines():
                m = TASK_RE.match(ln)
                if not m or m.group(1).lower() == "x":
                    continue
                text = m.group(2).strip()
                if not text:           # empty `- [ ]` template placeholder
                    continue
                item = {"text": text, "path": str(p.relative_to(vault))}
                dates = DATE_RE.findall(ln)
                if any(d <= today_iso for d in dates):
                    due_today.append(item)
                else:
                    open_tasks.append(item)
    return open_tasks, due_today


def yesterday_recap(vault, today):
    p = vault / "Calendar" / "Daily" / f"{(today - timedelta(days=1)).isoformat()}.md"
    if not p.exists():
        return None
    txt = read_text(p)
    done = [l for l in section(txt, "done today").splitlines()
            if l.strip().lstrip("-*").strip() and not l.strip().startswith("<!--")]
    completed, carry = [], []
    for ln in txt.splitlines():
        m = TASK_RE.match(ln)
        if not m:
            continue
        (completed if m.group(1).lower() == "x" else carry).append(m.group(2).strip())
    return {"date": p.stem, "done": done[:10], "completed": completed[:10], "carry": carry[:10]}


def project_session(vault):
    projects = []
    study = str(vault / "Areas" / "Study")
    for p in walk_md(vault / "Areas"):
        if study in str(p):
            continue
        fm = frontmatter(read_text(p))
        if fm.get("type") == "project" and fm.get("status", "").lower() == "active":
            projects.append({"name": p.stem, "status": fm.get("status", ""), "date": fm.get("date", "")})
    last = None
    sdir = vault / "Areas" / "Work" / "Session-Logs"
    if sdir.exists():
        logs = sorted(sdir.glob("*.md"), key=lambda q: q.stat().st_mtime, reverse=True)
        if logs:
            txt = read_text(logs[0])
            mo = re.search(r"\*\*Outcome:\*\*\s*(.*)", txt)
            last = {
                "name": logs[0].stem,
                "outcome": (mo.group(1).strip() if mo else ""),
                "topics": frontmatter(txt).get("topics", ""),
                "quick": section(txt, "quick reference")[:500],
            }
    return projects[:8], last


def concept_of_the_day(vault, today):
    """Pick a well-linked but still-stubbed topic, rotating by date for variety."""
    tdir = vault / "Areas" / "Study" / "Topics"
    if not tdir.exists():
        return None
    stubs = {p.stem: p for p in tdir.glob("*.md")}
    if not stubs:
        return None
    link_re = re.compile(r"\[\[([^\]|#]+)")
    counts, examples = {}, {}
    for p in walk_md(vault / "Areas" / "Study"):
        if p.parent.name == "Topics":
            continue
        seen = set()
        for raw in link_re.findall(read_text(p)):
            name = raw.strip()
            if name in stubs and name not in seen:
                seen.add(name)
                counts[name] = counts.get(name, 0) + 1
                examples.setdefault(name, [])
                if len(examples[name]) < 5:
                    examples[name].append(p.stem)
    cands = []
    for name, path in stubs.items():
        c = counts.get(name, 0)
        if c < 3:
            continue
        notes = section(read_text(path), "notes").lower()
        if notes and "empty" not in notes:   # already fleshed out — skip
            continue
        cands.append((c, name))
    if not cands:  # nothing stubbed-and-linked; fall back to most-linked overall
        cands = [(counts.get(n, 0), n) for n in stubs if counts.get(n, 0) > 0]
    if not cands:
        return None
    cands.sort(reverse=True)
    top = cands[:30]
    c, name = top[today.toordinal() % len(top)]
    return {"name": name, "inlinks": c, "examples": examples.get(name, [])[:5]}


# ---- Assemble the packet ----------------------------------------------------
def build_packet(vault, today):
    today_iso = today.isoformat()
    open_tasks, due_today = gather_tasks(vault, today_iso)
    recap = yesterday_recap(vault, today)
    projects, last = project_session(vault)
    concept = concept_of_the_day(vault, today)

    L = [f"# Brief packet for {today.strftime('%A %d %B %Y')}"]

    L.append("\n## Tasks & deadlines")
    if due_today:
        L.append("Due today / overdue:")
        for t in due_today[:15]:
            L.append(f"- [ ] {t['text']}  ({t['path']})")
    if open_tasks:
        L.append("Other open tasks:")
        for t in open_tasks[:15]:
            L.append(f"- [ ] {t['text']}  ({t['path']})")
    if not due_today and not open_tasks:
        L.append("(no open tasks found)")

    L.append("\n## Yesterday")
    if recap and (recap["completed"] or recap["done"] or recap["carry"]):
        if recap["completed"]:
            L.append("Completed: " + "; ".join(recap["completed"]))
        if recap["done"]:
            L.append("Done-today notes:")
            L.extend(recap["done"])
        if recap["carry"]:
            L.append("Unfinished (carry forward):")
            L.extend(f"- [ ] {c}" for c in recap["carry"])
    else:
        L.append("(no daily note for yesterday, or it was empty)")

    L.append("\n## Projects & last session")
    if projects:
        for p in projects:
            L.append(f"- {p['name']} — {p['status']}" + (f" (since {p['date']})" if p["date"] else ""))
    else:
        L.append("(no active projects)")
    if last:
        L.append(f"Last session: {last['name']}")
        if last["outcome"]:
            L.append(f"  Outcome: {last['outcome']}")
        elif last["quick"]:
            L.append("  " + last["quick"].replace("\n", " "))

    L.append("\n## Concept of the day")
    if concept:
        ex = ", ".join(f"[[{e}]]" for e in concept["examples"]) or "—"
        L.append(f"[[{concept['name']}]] — linked from {concept['inlinks']} notes; still a "
                 "stub worth a 5-minute review or expand.")
        L.append(f"Appears in: {ex}")
    else:
        L.append("(no topic graph yet)")

    stats = {"open": len(open_tasks), "due": len(due_today),
             "projects": len(projects), "concept": bool(concept)}
    return "\n".join(L), stats


def packet_to_plain_brief(packet, today):
    """Deterministic fallback brief when the model isn't called/available."""
    body = re.sub(r"^# Brief packet.*\n", "", packet).strip()
    return f"Good morning — here's {today.strftime('%A %d %B')}.\n\n{body}"


# ---- Write into today's daily note ------------------------------------------
def ensure_daily_note(vault, today):
    p = vault / "Calendar" / "Daily" / f"{today.isoformat()}.md"
    if p.exists():
        return p
    tpl = vault / "System" / "Templates" / "Daily.md"
    body = read_text(tpl) if tpl.exists() else (
        "---\ntype: daily\ndate: {{date:YYYY-MM-DD}}\ntags: [daily]\n---\n\n# {{date:YYYY-MM-DD}}\n")
    body = body.replace("{{date:YYYY-MM-DD}}", today.isoformat())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def inject_brief(vault, today, brief_md):
    """Insert/replace the Morning Brief block right after the note's H1. Idempotent.

    The block is fenced by HTML-comment markers, so a re-run replaces it cleanly even though
    the brief body itself contains `##` sub-headings."""
    p = ensure_daily_note(vault, today)
    txt = read_text(p)

    # strip any prior brief block between the markers (inclusive)
    txt = re.sub(re.escape(BRIEF_START) + r".*?" + re.escape(BRIEF_END) + r"\n?",
                 "", txt, flags=re.S)
    lines = txt.splitlines()

    block = [BRIEF_START, BRIEF_HDR,
             "<!-- generated by `hal brief` — re-running replaces this block -->"]
    block += brief_md.strip().splitlines()
    block += [BRIEF_END]

    h1 = next((i for i, ln in enumerate(lines) if H1_RE.match(ln.strip())), None)
    if h1 is None:
        result = block + [""] + lines
    else:
        tail = lines[h1 + 1:]
        while tail and not tail[0].strip():   # drop leading blanks so spacing stays tidy
            tail.pop(0)
        result = lines[:h1 + 1] + [""] + block + [""] + tail

    p.write_text("\n".join(result).rstrip() + "\n", encoding="utf-8")
    return p


# ---- Main -------------------------------------------------------------------
def resolve_vault(arg):
    if arg:
        v = Path(arg).expanduser()
        if v.exists():
            return v
    env = os.environ.get("HAL_VAULT", "")
    if env:
        v = Path(env).expanduser()
        if v.exists():
            return v
    guess = Path(__file__).resolve().parent / "Vault"
    return guess if guess.exists() else None


def main():
    ap = argparse.ArgumentParser(description="Hal's local, private morning brief.")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    # Empty in the cloud profile — the brief then falls back to its plain, no-LLM format.
    ap.add_argument("--model", default=os.environ.get("HAL_MODEL", ""))
    ap.add_argument("--no-llm", action="store_true", help="deterministic packet only")
    ap.add_argument("--mock", action="store_true", help="alias for --no-llm")
    ap.add_argument("--print-only", action="store_true", help="don't write the daily note")
    ap.add_argument("--vault", default=None, help="override vault path")
    args = ap.parse_args()

    vault = resolve_vault(args.vault)
    if not vault:
        err("I don't know where your vault is. Set HAL_VAULT or pass --vault.")
        sys.exit(1)

    today = date.fromisoformat(args.date) if args.date else date.today()

    hal(f"Good morning. Pulling your day together for {today.strftime('%A %d %B')}…")
    packet, stats = build_packet(vault, today)

    if args.no_llm or args.mock or not args.model:
        # No model configured = the cloud profile. The plain brief is fully deterministic
        # and needs no LLM at all, so the command still does something useful.
        brief = packet_to_plain_brief(packet, today)
    else:
        try:
            brief = call_model(
                [{"role": "system", "content": HAL_BRIEF_SYSTEM},
                 {"role": "user", "content": packet}],
                args.model)
            if not brief:
                raise RuntimeError("empty reply")
        except Exception as e:  # noqa: BLE001 - any failure falls back gracefully
            warn(f"local model unavailable ({e}); using the plain brief.")
            brief = packet_to_plain_brief(packet, today)

    print("\n" + brief + "\n")

    if not args.print_only:
        p = inject_brief(vault, today, brief)
        ok(f"Written into {p.relative_to(vault)}")

    tick(f"{DIM}{stats['due']} due/overdue · {stats['open']} open · "
         f"{stats['projects']} active project(s){OFF}")


if __name__ == "__main__":
    main()
