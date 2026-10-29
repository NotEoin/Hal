"""`hal console`: a local web page for trying Hal and grading the answers.

Lists every case in eval/scenarios.toml (the live ones too) with what to look for. Each
can be sent to Hal as text from the page, or said aloud to hal-voice and then pulled in
with "use my last voice turn". The page runs the automatic checks it can against the real
setup, and you mark each answer pass or fail with a note. Grades are kept in
~/.local/state/hal/console-results.jsonl.

Unlike `hal eval`, this talks to your REAL calendar, tasks and vault; each card has an
"undo" that removes what the case created.

    hal console [--port 8740]     then open http://127.0.0.1:8740
"""
from __future__ import annotations

import argparse
import json
import re
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, evaluate
from .brain import Brain

PAGE = Path(__file__).with_name("console.html")
_lock = threading.Lock()
_brain: Brain | None = None
_created: dict[str, list[tuple[str, dict]]] = {}     # case id -> undo calls


def results_file() -> Path:
    return config.state_dir() / "console-results.jsonl"


def grades() -> dict[str, dict]:
    """Returns the latest grade for each case."""
    out = {}
    try:
        for line in results_file().read_text().splitlines():
            g = json.loads(line)
            out[g["id"]] = g
    except (OSError, json.JSONDecodeError):
        pass
    return out


def brain() -> Brain:
    global _brain
    if _brain is None:
        _brain = Brain(channel="voice")
    return _brain


def auto_check(case: dict, turn: dict) -> dict:
    """Runs the checks that make sense against real data; cases with sandbox setup skip data checks."""
    live_case = dict(case)
    if case.get("setup"):
        for key in ("reply", "after", "args"):
            live_case.pop(key, None)
    if case.get("no_extensions"):
        return {"ran": False, "problems": [], "note": "raw-hal case: try it with `hal chat --no-tools`"}
    hub = config.hub()
    problems = evaluate.check(live_case, [turn], hub, turn.get("reply", ""))
    note = "data checks skipped: this case seeds a sandbox" if case.get("setup") else ""
    return {"ran": True, "problems": problems, "note": note}


def _remember_undo(case_id: str, turn: dict) -> None:
    """Records how to remove what a turn created, from the uids in its tool results."""
    for t in turn.get("tools", []):
        try:
            result = json.loads(t["result"]).get("result")
        except (json.JSONDecodeError, AttributeError):
            continue
        text = json.dumps(result)
        if t["name"] == "calendar_add":
            for uid in re.findall(r'"uid": "([^"]+)"', text):
                _created.setdefault(case_id, []).append(("calendar_delete", {"event": uid, "_confirmed": True}))
        elif t["name"] == "tasks_add":
            for tid in re.findall(r'"id": (\d+)', text)[:1]:
                _created.setdefault(case_id, []).append(("tasks_delete", {"task": tid, "_confirmed": True}))
        elif t["name"] == "clock_timer" or t["name"] == "clock_alarm":
            _created.setdefault(case_id, []).append(("clock_cancel", {"which": "all"}))


def _sync_calendar(turn: dict) -> str:
    """Pushes calendar changes to CalDAV at once, so they reach the phone without waiting."""
    if not any(t["name"].startswith("calendar_") for t in turn.get("tools", [])):
        return ""
    if not config.load().get("calendar", {}).get("caldav", {}).get("url"):
        return "CalDAV is not set up, so nothing was synced"
    got = config.hub().call("calendar_sync", {})
    return f"synced: {json.dumps(got.get('result', got))[:200]}"


def ask(case_id: str, text: str) -> dict:
    with _lock:
        b = brain()
        b.reply(text)
        turn = dict(b.last_turn)
    return _finish(case_id, turn)


def last_voice_turn(case_id: str) -> dict:
    path = config.state_dir() / "turns.jsonl"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        turn = json.loads(line)
        if turn.get("channel") == "voice":
            return _finish(case_id, turn)
    return {"error": "no voice turn yet: is hal-voice running? (hal status)"}


def _finish(case_id: str, turn: dict) -> dict:
    case = next((c for c in evaluate.load_cases() if c["id"] == case_id), None)
    _remember_undo(case_id, turn)
    out = {"turn": turn, "sync": _sync_calendar(turn)}
    out["check"] = auto_check(case, turn) if case else {"ran": False, "problems": []}
    return out


def undo(case_id: str) -> dict:
    hub = config.hub()
    done = [f"{name}: {'ok' if 'error' not in hub.call(name, args) else 'failed'}"
            for name, args in _created.pop(case_id, [])]
    return {"undone": done or ["nothing recorded to undo"], "sync": _sync_calendar({"tools": [{"name": "calendar_"}]})}


def grade(body: dict) -> dict:
    entry = {"id": body["id"], "verdict": body["verdict"], "note": body.get("note", ""),
             "at": datetime.now().isoformat(timespec="seconds"), "turn": body.get("turn"),
             "check": body.get("check")}
    with results_file().open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return {"saved": True}


def info() -> dict:
    hub = config.hub()
    b = brain()
    return {"model": b.model, "tools": len(hub.all_tools()) if hub else 0,
            "modules": sorted({t.module.name for t in hub.all_tools().values()}) if hub else [],
            "caldav": bool(config.load().get("calendar", {}).get("caldav", {}).get("url")),
            "voice": bool(config.hub().get("voice_say"))}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, kind: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # quiet
        pass

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/cases":
            cases = [{k: evaluate.fill(v) if isinstance(v, str) and k in ("ask", "look_for") else v
                      for k, v in c.items()} for c in evaluate.load_cases()]
            self._send(200, {"cases": cases, "grades": grades(), "info": info()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            routes = {
                "/api/ask": lambda: ask(body.get("id", ""), body["text"]),
                "/api/voice": lambda: last_voice_turn(body.get("id", "")),
                "/api/grade": lambda: grade(body),
                "/api/undo": lambda: undo(body["id"]),
                "/api/reset": lambda: (brain().reset(), {"reset": True})[1],
                "/api/speak": lambda: config.hub().call("voice_say", {"text": body["text"]}),
            }
            if self.path not in routes:
                self._send(404, {"error": "not found"})
                return
            self._send(200, routes[self.path]())
        except Exception as e:  # noqa: BLE001 - shown on the page
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="hal console")
    ap.add_argument("--port", type=int, default=int(config.load().get("console", {}).get("port", 8740)))
    args = ap.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Hal console on http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
