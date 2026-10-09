"""Ranks tools against a request so an assistant sees only the few it needs.

Plain keyword scoring (BM25) over each tool's name, description and keywords,
with a small synonym table for everyday phrasing. Stdlib only.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from .registry import Tool

SYNONYMS = {
    "remind": "reminder calendar task", "appointment": "event calendar", "meeting": "event calendar",
    "schedule": "calendar event", "todo": "task", "to-do": "task", "chore": "task",
    "remember": "memory note", "note": "memory", "forget": "memory", "recall": "memory search",
    "music": "media play", "song": "media play", "volume": "audio volume", "louder": "volume",
    "quieter": "volume", "pause": "media", "skip": "media next", "lights": "rgb light colour",
    "light": "rgb colour", "color": "colour rgb", "colour": "rgb", "weather": "forecast",
    "rain": "weather", "temperature": "weather sensor", "cpu": "system", "gpu": "system",
    "ram": "memory system", "disk": "storage system", "file": "files", "folder": "files directory",
    "screenshot": "screen capture", "screen": "screen", "clipboard": "copy paste",
    "timer": "timer clock", "alarm": "clock timer", "wake": "alarm", "email": "mail", "inbox": "mail",
    "job": "jobs application", "jobs": "application", "phone": "device", "wifi": "network",
    "internet": "network web", "ip": "network", "search": "find", "google": "web search",
    "news": "headlines", "define": "dictionary", "translate": "language", "convert": "units",
    "calculate": "math", "shutdown": "power", "reboot": "power restart", "sleep": "power suspend",
    "lock": "session power", "battery": "power", "update": "packages", "install": "packages",
    "git": "repo code", "commit": "git", "habit": "habits", "water": "habits", "spend": "ledger money",
    "money": "ledger", "birthday": "contacts people", "person": "contacts", "who": "contacts",
    "window": "desktop", "open": "launch", "app": "launch application", "print": "printer",
    "bluetooth": "device", "headphones": "bluetooth audio", "brief": "briefing", "morning": "briefing",
    "focus": "routine scene", "routine": "automation", "model": "ollama", "running": "process",
}

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("_", " "))


def _expand(query: str) -> list[str]:
    words = _tokens(query)
    out = list(words)
    for w in words:
        out += _tokens(SYNONYMS.get(w, ""))
        if w.endswith("s") and len(w) > 3:
            out.append(w[:-1])
    return out


def rank(query: str, tools: list[Tool], limit: int = 8) -> list[Tool]:
    """Returns up to `limit` tools most relevant to `query`, best first."""
    docs = [_tokens(f"{t.name} {t.name} {t.module.name} {t.description} {t.keywords}") for t in tools]
    if not docs:
        return []
    avg = sum(len(d) for d in docs) / len(docs)
    df = Counter(w for d in docs for w in set(d))
    q = _expand(query)
    scored = []
    for tool, doc in zip(tools, docs):
        tf = Counter(doc)
        score = 0.0
        for w in q:
            if w not in tf:
                continue
            idf = math.log(1 + (len(docs) - df[w] + 0.5) / (df[w] + 0.5))
            score += idf * tf[w] * 2.2 / (tf[w] + 1.2 * (0.25 + 0.75 * len(doc) / avg))
        if score > 0:
            scored.append((score, tool.name, tool))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [t for _, _, t in scored[:limit]]
