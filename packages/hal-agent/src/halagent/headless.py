"""Headless runs (`hal-agent -p`): one task, rendered as text, one JSON result or JSON lines.

The schema and the exit codes follow docs/design/03-agent.md section 3.15, so `hal -p`, which
sees only ACP, and `hal-agent -p` agree. This module writes to the stream it is given and never
to the process stdout by itself; `cli.py` owns printing.
"""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO

from halagent import __version__
from halagent.loop import Agent, RunResult
from halagent.provider import ProviderError

log = logging.getLogger("halagent.headless")

EXIT_OK, EXIT_ERROR, EXIT_UNREACHABLE, EXIT_LIMIT, EXIT_REFUSAL = 0, 1, 3, 4, 5
EXIT_TIMEOUT, EXIT_CANCELLED = 124, 130
STREAM_RESULT_CHARS = 4_000  # tool results in stream-json are cut to this (03-agent 3.15.3)
GIT_TIMEOUT_S = 10.0
EDIT_TOOLS = frozenset({"edit", "patch", "write"})
# halagent stop reason -> (subtype, exit code); table in 03-agent 3.15.2
OUTCOMES: dict[str, tuple[str, int]] = {
    "finished": ("success", EXIT_OK),
    "done": ("success", EXIT_OK),
    "max_tool_calls": ("error_max_turns", EXIT_LIMIT),
    "max_model_requests": ("error_max_turns", EXIT_LIMIT),
    "max_time": ("error_max_turns", EXIT_LIMIT),
    "loop": ("error_during_execution", EXIT_LIMIT),
    "nudge_limit": ("error_during_execution", EXIT_LIMIT),
    "tool_errors": ("error_during_execution", EXIT_LIMIT),
    "context_overflow": ("error_during_execution", EXIT_LIMIT),
    "refusal": ("error_refusal", EXIT_REFUSAL),
    "error": ("error_during_execution", EXIT_ERROR),
    "model_unreachable": ("error_model_unreachable", EXIT_UNREACHABLE),
    "timeout": ("error_timeout", EXIT_TIMEOUT),
    "cancelled": ("error_cancelled", EXIT_CANCELLED),
}


def utc_now() -> str:
    """The current time as UTC ISO 8601 with milliseconds and a `Z`."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Collector:
    """Gathers usage and edited paths from agent updates."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    peak_prompt_tokens: int = 0
    edited: list[str] = field(default_factory=list)
    first_edit_call: int | None = None
    calls: int = 0
    _args: dict[str, str] = field(default_factory=dict)

    def note(self, update: dict) -> None:
        """Updates the totals from one agent update."""
        kind = update.get("kind")
        if kind == "usage":
            self.prompt_tokens += update["prompt_tokens"]
            self.completion_tokens += update["completion_tokens"]
            self.peak_prompt_tokens = max(self.peak_prompt_tokens, update["prompt_tokens"])
        elif kind == "tool_call":
            self.calls += 1
        elif kind == "tool_result" and update["name"] in EDIT_TOOLS and not update["failed"]:
            path = _path_of(update["id"], self._args)
            if self.first_edit_call is None:
                self.first_edit_call = self.calls
            if path and path not in self.edited:
                self.edited.append(path)

    def remember_call(self, update: dict) -> None:
        """Keeps the raw arguments of a call so its result can name the edited path."""
        self._args[update["id"]] = update["raw"]


def _path_of(call_id: str, args: dict[str, str]) -> str:
    try:
        return str(json.loads(args.get(call_id, "{}")).get("path", ""))
    except (json.JSONDecodeError, AttributeError):
        return ""


class Renderer:
    """Writes the chosen output format to `out`."""

    def __init__(self, fmt: str, out: IO[str], session_id: str) -> None:
        if fmt not in ("text", "json", "stream-json"):
            raise ValueError(f"unknown output format {fmt!r}")
        self.fmt, self.out, self.session_id = fmt, out, session_id

    def _line(self, obj: dict) -> None:
        self.out.write(json.dumps({**obj, "session_id": self.session_id, "ts": utc_now()}) + "\n")
        self.out.flush()

    def init(self, cwd: str, model: str, toolset: str, tools: list[str]) -> None:
        """The `system/init` line of stream-json."""
        if self.fmt == "stream-json":
            self._line({"type": "system", "subtype": "init", "cwd": cwd, "model": model,
                        "toolset": toolset, "tools": tools, "hal_version": __version__})  # fmt: skip

    def update(self, update: dict) -> None:
        """Renders one agent update (stream-json only)."""
        if self.fmt != "stream-json":
            return
        kind = update.get("kind")
        if kind == "text":
            content = [{"type": "text", "text": update["text"]}]
            self._line({"type": "assistant", "message": {"role": "assistant", "content": content}})
        elif kind == "tool_call":
            try:
                args = json.loads(update["raw"])
            except json.JSONDecodeError:
                args = {"raw": update["raw"]}
            block = {"type": "tool_use", "id": update["id"], "name": update["name"], "where": "box",
                     "input": args}  # fmt: skip
            self._line({"type": "assistant", "message": {"role": "assistant", "content": [block]}})
        elif kind == "tool_result":
            text = update["text"][:STREAM_RESULT_CHARS]
            block = {"type": "tool_result", "tool_use_id": update["id"], "content": text,
                     "is_error": update["failed"]}  # fmt: skip
            self._line({"type": "user", "message": {"role": "user", "content": [block]}})
        elif kind == "usage":
            self._line({"type": "system", "subtype": "usage", "used": update["prompt_tokens"],
                        "size": update["window"]})  # fmt: skip

    def result(self, obj: dict) -> None:
        """Writes the final result in the chosen format."""
        if self.fmt == "text":
            self.out.write(obj["result"] + "\n")
        elif self.fmt == "json":
            self.out.write(json.dumps({**obj, "ts": utc_now()}) + "\n")
        else:
            self._line(obj)
        self.out.flush()


def files_changed(cwd: str, edited: list[str]) -> list[str]:
    """Changed paths from `git status` when `cwd` is a repository, else the edited paths."""
    try:
        done = subprocess.run(
            ["git", "--no-optional-locks", "status", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return edited
    if done.returncode != 0:
        return edited
    return [line[3:] for line in done.stdout.splitlines() if len(line) > 3]


def run_headless(
    agent: Agent,
    prompt: str,
    *,
    fmt: str,
    out: IO[str],
    session_id: str,
    cwd: str,
    timeout: float | None = None,
    install_signals: bool = True,
) -> int:
    """Runs `prompt` as one task, prints the result in `fmt` and returns the exit code.

    A SIGINT or SIGTERM cancels the run (exit 130); `timeout` seconds cancel it too (exit 124).
    """
    render, stats = Renderer(fmt, out, session_id), Collector()
    why: list[str] = []  # why the run was cancelled: "timeout" or "cancelled"

    def listener(update: dict) -> None:
        if update.get("kind") == "tool_call":
            stats.remember_call(update)
        stats.note(update)
        render.update(update)

    agent.on_update = listener
    render.init(cwd, agent.settings.model, "coding", list(agent.toolset.tools))

    def cancel(reason: str) -> None:
        why.append(reason)
        agent.cancel.set()

    timer = threading.Timer(timeout, cancel, ("timeout",)) if timeout else None
    old: dict[int, object] = {}
    if install_signals and threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            old[sig] = signal.signal(sig, lambda *_: cancel("cancelled"))
    if timer:
        timer.daemon = True
        timer.start()
    started = time.monotonic()
    result, error = _run(agent, prompt)
    if timer:
        timer.cancel()
    for sig, handler in old.items():
        signal.signal(sig, handler)  # type: ignore[arg-type]  # the saved handler is restorable as is
    stop = result.stopped if result else error[0]
    if stop == "cancelled" and why:
        stop = why[0]
    subtype, code = OUTCOMES.get(stop, OUTCOMES["error"])
    text = result.text if result else error[1]
    render.result(
        {
            "type": "result", "subtype": subtype, "is_error": code != EXIT_OK, "exit_code": code,
            "result": text, "stop_reason": stop, "session_id": session_id, "model": agent.settings.model,
            "num_turns": 1, "num_model_requests": result.turns if result else 0,
            "num_tool_calls": result.tool_calls if result else 0,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "usage": {"input_tokens": stats.prompt_tokens, "output_tokens": stats.completion_tokens},
            "files_changed": files_changed(cwd, stats.edited),
            "stats": {
                "peak_prompt_tokens": stats.peak_prompt_tokens,
                "first_edit_call": stats.first_edit_call,
                "counters": dict(agent.counters),
            },
            "hal_version": __version__,
        }
    )  # fmt: skip
    return code


def _run(agent: Agent, prompt: str) -> tuple[RunResult | None, tuple[str, str]]:
    """Runs the agent; a provider failure becomes an (error kind, message) pair."""
    try:
        return agent.run(prompt), ("", "")
    except ProviderError as exc:
        log.error("model request failed: %s", exc)
        return None, ("model_unreachable" if exc.status == 0 else "error", str(exc))


__all__ = ["OUTCOMES", "Renderer", "run_headless", "utc_now"]
