"""Assistant scenarios: the case file, the checks and the runner protocol (03-agent 3.18.1).

The library never builds a hub or a broker: an `AssistantRunner` (implemented by halcore) opens a
fresh assistant session per case and returns what happened. The `after` steps read host state
through `runner.probe`. `confirm_first` passes only when the broker asked for approval for the
tool during the turn; the text "yes" approves nothing (REQ-AGT-98).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
PLACEHOLDER = re.compile(r"\{(date|day|weekday):([a-z]+)\}")
CASE_KEYS = frozenset(
    "id category ask turns needs setup tools any_tools forbid no_tools args confirm_first reply reply_not "
    "max_chars after no_extensions look_for live approve".split()
)  # fmt: skip


class ScenarioError(ValueError):
    """The case file is malformed; the message names the case and key."""


@dataclass(frozen=True)
class Case:
    """One scenario. `turns` holds the messages (`ask` becomes one turn)."""

    id: str
    turns: tuple[str, ...]
    category: str = ""
    needs: tuple[str, ...] = ()
    setup: tuple[Mapping[str, Any], ...] = ()
    tools: tuple[str, ...] = ()
    any_tools: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()
    no_tools: bool = False
    args: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    confirm_first: tuple[str, ...] = ()
    reply: str = ""
    reply_not: str = ""
    max_chars: int = 0
    after: tuple[Mapping[str, Any], ...] = ()
    no_extensions: bool = False
    look_for: str = ""
    live: bool = False
    approve: bool = False


@dataclass(frozen=True)
class ToolUse:
    """One tool call of a turn, in call order."""

    name: str
    args: Mapping[str, Any]
    ok: bool = True
    seconds: float = 0.0


@dataclass(frozen=True)
class TurnOutcome:
    """What one assistant turn did."""

    reply: str
    tools: Sequence[ToolUse] = ()
    approvals_requested: Sequence[str] = ()
    seconds: float = 0.0
    first_words_s: float | None = None


class AssistantRunner(Protocol):
    """One fresh assistant session per case; implemented by halcore (06-core-cli)."""

    def run_case(self, case: Case, *, model: str, seed: int | None) -> Sequence[TurnOutcome]:
        """Runs the setup and every turn of `case`; may raise, which fails the case."""
        ...

    def probe(self, tool: str, args: Mapping[str, Any]) -> str:
        """Calls a host tool after the turns and returns its result as text (for `after`)."""
        ...


def load_cases(path: Path) -> list[Case]:
    """Reads and validates the case file; an unknown key or a duplicate id fails loudly."""
    try:
        rows = tomllib.loads(path.read_text(encoding="utf-8"))["case"]
    except (OSError, ValueError, KeyError) as exc:
        raise ScenarioError(f"{path}: {exc}") from exc
    cases, seen = [], set()
    for row in rows:
        cid = row.get("id", "?")
        if unknown := set(row) - CASE_KEYS:
            raise ScenarioError(f"case {cid}: unknown key {sorted(unknown)[0]}")
        if cid in seen or not isinstance(cid, str):
            raise ScenarioError(f"case {cid}: missing or duplicate id")
        seen.add(cid)
        turns = row.get("turns") or ([row["ask"]] if "ask" in row else [])
        if not turns:
            raise ScenarioError(f"case {cid}: needs ask or turns")
        fields = {k: (tuple(v) if isinstance(v, list) else v) for k, v in row.items() if k not in ("ask",)}
        fields["turns"] = tuple(turns)
        cases.append(Case(**fields))
    return cases


def fill(pattern: str, today: date | None = None) -> str:
    """Replaces `{date:..}`, `{day:..}` and `{weekday:..}` with the named day's value."""
    today = today or date.today()

    def sub(m: re.Match[str]) -> str:
        kind, word = m.groups()
        if word == "today":
            d = today
        elif word == "tomorrow":
            d = today + timedelta(days=1)
        elif word in WEEKDAYS:
            d = today + timedelta(days=(WEEKDAYS.index(word) - today.weekday()) % 7 or 7)
        else:
            raise ScenarioError(f"unknown day word {word!r} in {pattern!r}")
        return {"date": d.isoformat(), "day": str(d.day), "weekday": d.strftime("%A")}[kind]

    return PLACEHOLDER.sub(sub, pattern)


def check(
    case: Case, turns: Sequence[TurnOutcome], runner: AssistantRunner, today: date | None = None
) -> list[str]:
    """What was wrong with the answers to `case`; empty when it passed."""
    problems: list[str] = []
    calls = [t for turn in turns for t in turn.tools]
    names = [c.name for c in calls]
    reply = turns[-1].reply if turns else ""
    problems += [f"did not call {n}" for n in case.tools if n not in names]
    if case.any_tools and not set(case.any_tools) & set(names):
        problems.append(f"called none of {', '.join(case.any_tools)}")
    problems += [f"called {n}, which it must not" for n in case.forbid if n in names]
    if case.no_tools and names:
        problems.append(f"called {', '.join(names)} when no tool was needed")
    for tool, wanted in case.args.items():
        made = [c.args for c in calls if c.name == tool]
        for param, pattern in wanted.items() if made else ():
            if not any(re.search(fill(pattern, today), str(a.get(param, ""))) for a in made):
                problems.append(f"{tool}.{param} did not match {pattern}: {[a.get(param) for a in made]}")
    asked = {n for turn in turns for n in turn.approvals_requested}
    problems += [f"the broker never asked approval for {n}" for n in case.confirm_first if n not in asked]
    if case.reply and not re.search(fill(case.reply, today), reply):
        problems.append(f"reply did not match {fill(case.reply, today)}")
    if case.reply_not and re.search(fill(case.reply_not, today), reply):
        problems.append(f"reply matched {case.reply_not}")
    if case.max_chars and len(reply) > case.max_chars:
        problems.append(f"reply is {len(reply)} characters (most {case.max_chars})")
    if not case.no_extensions:
        for step in case.after:
            got = runner.probe(step["tool"], step.get("args", {}))
            if step.get("match") and not re.search(fill(step["match"], today), got):
                problems.append(
                    f"afterwards {step['tool']} did not show {fill(step['match'], today)}: {got[:160]}"
                )
            if step.get("absent") and re.search(fill(step["absent"], today), got):
                problems.append(f"afterwards {step['tool']} still shows {step['absent']}")
    return problems
