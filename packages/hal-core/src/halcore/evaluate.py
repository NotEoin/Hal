"""`hal eval`: asks Hal the cases in eval/assistant/scenarios.toml and checks every answer.

Runs in a sandbox: a temporary config, calendar, task list, vault and state, built from
your real config's models and persona, so nothing real changes. Extensions that act on
the machine or the outside world (notifications, power, desktop, media, lights, mail,
phone) are switched off in the sandbox; the console covers those live.

    hal eval                              every non-live case with your configured model
    hal eval --model a --model b          compare models
    hal eval --only calendar-add,tasks-list   or  --category calendar
    hal eval --repeat 3                   ask each case several times (models are not deterministic)

Reports go to ~/.local/state/hal/eval/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path

from halkit import proc

from . import config, gpulight

SCENARIOS = config.ROOT / "eval" / "assistant" / "scenarios.toml"
# Extensions with effects outside the sandbox: never run unattended.
UNSAFE = ["notify", "power", "desktop", "media", "rgb", "mail", "phone", "routines", "packages", "shell",
          "devices", "screen", "transcribe", "telegram", "voice", "jobs", "dev", "models", "feeds", "network"]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SEED_NOTES = {
    "Areas/Study/Dissertation decisions.md":
        "---\ntype: study\n---\n# Dissertation decisions\n\n- 2026-09-14: Decided to use Redis as the caching "
        "layer for the dissertation prototype, because it handles the sliding-window rate limiter as well.\n"
        "- Rejected Memcached: no persistence.\n",
    "People/Sam.md": "# Sam\n\nFriend from university. Likes climbing.\n",
}


def load_cases() -> list[dict]:
    return tomllib.loads(SCENARIOS.read_text())["case"]


# ---- placeholders ---------------------------------------------------------------------

def _next_weekday(name: str, today: date) -> date:
    ahead = (WEEKDAYS.index(name) - today.weekday()) % 7 or 7
    return today + timedelta(days=ahead)


def _resolve(word: str, today: date) -> date:
    word = word.lower()
    if word == "today":
        return today
    if word == "tomorrow":
        return today + timedelta(days=1)
    return _next_weekday(word, today)


def fill(pattern: str, today: date | None = None) -> str:
    """Replaces {date:...}, {day:...} and {weekday:...} in a check pattern."""
    today = today or date.today()

    def sub(m: re.Match) -> str:
        kind, word = m.group(1), m.group(2)
        d = _resolve(word, today)
        return {"date": d.isoformat(), "day": str(d.day), "weekday": d.strftime("%A")}[kind]

    return re.sub(r"\{(date|day|weekday):([a-z]+)\}", sub, pattern)


# ---- the sandbox ----------------------------------------------------------------------

def make_sandbox(model: str | None) -> Path:
    """Writes a throwaway config and vault, and points HAL_CONFIG/HAL_DATA/HAL_STATE at them."""
    real = config.load()
    root = Path(tempfile.mkdtemp(prefix="hal-eval-"))
    vault = root / "vault"
    for rel, text in SEED_NOTES.items():
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(text)
    (vault / "+Inbox").mkdir()
    hal = {k: v for k, v in real.get("hal", {}).items() if k != "vault"}
    hal["vault"] = str(vault)
    if model:
        hal["chat_model"] = model
    brain = dict(real.get("brain", {}))
    brain["deep_work"] = "off"
    brain["keep_alive"] = "2m"          # don't hold the GPU after the run
    if model:
        brain["model"] = model
    hub = {"module_paths": real.get("hub", {}).get("module_paths") or
           [str(config.ECOSYSTEM / "core"), str(config.extensions_dir())],
           "disabled": sorted(set(UNSAFE) | set(real.get("hub", {}).get("disabled", [])))}
    calendar = {k: v for k, v in real.get("calendar", {}).items() if k != "caldav"}
    lines = []
    for table, values in (("hal", hal), ("brain", brain), ("hub", hub), ("calendar", calendar)):
        lines.append(f"[{table}]")
        lines += [f"{k} = {json.dumps(v)}" for k, v in values.items() if not isinstance(v, dict)]
        lines.append("")
    (root / "config.toml").write_text("\n".join(lines))
    os.environ.update(HAL_CONFIG=str(root / "config.toml"), HAL_DATA=str(root / "data"),
                      HAL_STATE=str(root / "state"))
    return root


def _fresh_hub():
    """Re-imports hal-hub so it reads the sandbox's environment."""
    ext = str(config.extensions_dir())
    for name, mod in list(sys.modules.items()):
        # Extension code may hold open databases from the previous sandbox.
        if name == "halhub" or name.startswith("halhub.") or str(getattr(mod, "__file__", "") or "").startswith(ext):
            del sys.modules[name]
    return config.hub()


# ---- checking -------------------------------------------------------------------------

def check(case: dict, turns: list[dict], hub, reply: str) -> list[str]:
    """Returns what was wrong with Hal's answer to a case; empty when it passed."""
    problems = []
    calls = [t for turn in turns for t in turn.get("tools", [])]
    names = [c["name"] for c in calls]
    for name in case.get("tools", []):
        if name not in names:
            problems.append(f"did not call {name}")
    if case.get("any_tools") and not set(case["any_tools"]) & set(names):
        problems.append(f"called none of {', '.join(case['any_tools'])}")
    for name in case.get("forbid", []):
        if name in names:
            problems.append(f"called {name}, which it must not")
    if case.get("no_tools") and names:
        problems.append(f"called {', '.join(names)} when no tool was needed")
    for tool, wanted in case.get("args", {}).items():
        made = [c["args"] for c in calls if c["name"] == tool]
        if not made:
            continue
        for param, pattern in wanted.items():
            if not any(re.search(fill(pattern), str(a.get(param, ""))) for a in made):
                problems.append(f"{tool}.{param} did not match {pattern}: {[a.get(param) for a in made]}")
    for tool in case.get("confirm_first", []):
        made = [c for c in calls if c["name"] == tool]
        if made and not made[0]["confirm"]:
            problems.append(f"ran {tool} without asking first")
        if not made and len(turns) == 1 and "reply" not in case:
            problems.append(f"did not try {tool} (so never asked)")
    if case.get("reply") and not re.search(fill(case["reply"]), reply):
        problems.append(f"reply did not match {fill(case['reply'])}")
    if case.get("reply_not") and re.search(fill(case["reply_not"]), reply):
        problems.append(f"reply matched {case['reply_not']}")
    if case.get("max_chars") and len(reply) > case["max_chars"]:
        problems.append(f"reply is {len(reply)} characters (most {case['max_chars']})")
    if hub:
        for step in case.get("after", []):
            got = json.dumps(hub.call(step["tool"], step.get("args", {})).get("result"), default=str)
            if step.get("match") and not re.search(fill(step["match"]), got):
                problems.append(f"afterwards {step['tool']} did not show {fill(step['match'])}: {got[:160]}")
            if step.get("absent") and re.search(fill(step["absent"]), got):
                problems.append(f"afterwards {step['tool']} still shows {step['absent']}")
    return problems


def missing_needs(case: dict, hub) -> list[str]:
    if case.get("no_extensions"):
        return []
    have = {t.module for t in hub.all_tools().values()} if hub else set()
    return [n for n in case.get("needs", []) if n not in have]


def run_case(case: dict, model: str | None, hub) -> dict:
    from .brain import Brain
    if not case.get("no_extensions"):
        for step in case.get("setup", []):
            hub.call(step["tool"], step.get("args", {}))
    brain = Brain(channel="voice", model=model, tools=not case.get("no_extensions"), log_turns=False)
    turns, started = [], time.time()
    for text in case.get("turns") or [case["ask"]]:
        try:
            brain.reply(text)
        except Exception as e:  # noqa: BLE001 - a broken turn fails the case, not the run
            return {"id": case["id"], "passed": False, "problems": [f"error: {e}"], "reply": "", "tools": [],
                    "seconds": round(time.time() - started, 1), "first_words": 0}
        turns.append(brain.last_turn)
    reply = turns[-1]["reply"]
    problems = check(case, turns, None if case.get("no_extensions") else hub, reply)
    return {"id": case["id"], "category": case.get("category", ""), "passed": not problems, "problems": problems,
            "reply": reply, "tools": [f"{t['name']}({json.dumps(t['args'])[:120]})" for x in turns for t in x["tools"]],
            "seconds": round(time.time() - started, 1), "first_words": turns[0].get("first_words", 0)}


def run(model: str | None, cases: list[dict], repeat: int = 1, verbose: bool = True) -> dict:
    real_state = config.state_dir()
    box = make_sandbox(model)
    hub = _fresh_hub()
    label = model or config.brain()["model"] or config.shared()["chat_model"]
    if verbose:
        print(f"\n== {label}  ({len(cases)} cases x {repeat}, sandbox {box})")
    results = []
    for case in cases:
        gap = missing_needs(case, hub)
        if gap:
            results.append({"id": case["id"], "skipped": f"needs {', '.join(gap)}"})
            if verbose:
                print(f"  skip  {case['id']:28} needs {', '.join(gap)}")
            continue
        for _ in range(repeat):
            r = run_case(case, model, hub)
            results.append(r)
            if verbose:
                mark = "PASS" if r["passed"] else "FAIL"
                print(f"  {mark}  {case['id']:28} {r['seconds']:5.1f}s  {r['reply'][:90]!r}")
                for p in r["problems"]:
                    print(f"          - {p}")
    ran = [r for r in results if "skipped" not in r]
    summary = {"model": label, "at": datetime.now().isoformat(timespec="seconds"),
               "passed": sum(r["passed"] for r in ran), "total": len(ran),
               "median_seconds": statistics.median([r["seconds"] for r in ran]) if ran else 0,
               "median_first_words": statistics.median([r["first_words"] for r in ran if r["first_words"]] or [0]),
               "results": results}
    out = real_state / "eval" / f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^a-z0-9.]+', '-', label.lower())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=1, default=str))
    shutil.rmtree(box, ignore_errors=True)
    if verbose:
        print(f"  {summary['passed']}/{summary['total']} passed, median {summary['median_seconds']}s a case, "
              f"first words after {summary['median_first_words']}s. Report: {out}")
    return summary


EVAL_TIMEOUT_S = 6 * 3600.0  # a batch eval may wait behind chat turns and then run many cases
UNDER_GPU_LIGHT = "HAL_EVAL_UNDER_GPU_LIGHT"  # set in the child so it does not wrap itself again


def batch_argv(argv: list[str], env: dict[str, str] | None = None) -> list[str] | None:
    """The `gpu-light run --class batch` command that runs this eval, or None to run it directly.

    Evals are throughput work: wrapped as class `batch` they yield the GPU to a chat or voice turn.
    Without gpu-light (or inside the wrapped child) the eval just runs.
    """
    env = os.environ if env is None else env
    exe = gpulight.binary()
    if exe is None or env.get(UNDER_GPU_LIGHT):
        return None
    return [exe, "run", "--class", "batch", "--name", "hal-eval", "--label", "app=hal", "--",
            sys.executable, "-m", "halcore", "eval", *argv]  # fmt: skip


def main(argv: list[str]) -> int:
    wrapped = batch_argv(argv)
    if wrapped is not None:
        done = proc.run(wrapped, timeout=EVAL_TIMEOUT_S, env={**os.environ, UNDER_GPU_LIGHT: "1"}, check=False)
        sys.stdout.write(done.stdout.decode(errors="replace"))
        sys.stderr.write(done.stderr.decode(errors="replace"))
        return done.returncode
    ap = argparse.ArgumentParser(prog="hal eval", description="Ask Hal the test cases and check the answers.")
    ap.add_argument("--model", action="append", help="Ollama model; repeat to compare")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--category")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args(argv)
    cases = [c for c in load_cases() if not c.get("live")]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    if args.category:
        cases = [c for c in cases if c.get("category") == args.category]
    saved = {k: os.environ.get(k) for k in ("HAL_CONFIG", "HAL_DATA", "HAL_STATE")}
    summaries = []
    for model in args.model or [None]:
        os.environ.update({k: v for k, v in saved.items() if v})
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
        summaries.append(run(model, cases, args.repeat))
    if len(summaries) > 1:
        print("\nmodel                               passed   median s   first words s")
        for s in sorted(summaries, key=lambda s: (-s["passed"], s["median_seconds"])):
            print(f"{s['model']:36} {s['passed']:3}/{s['total']:<3}   {s['median_seconds']:7.1f}   "
                  f"{s['median_first_words']:7.1f}")
    return 0 if all(s["passed"] == s["total"] for s in summaries) else 1
