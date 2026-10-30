"""The `subagent` tool: child agents with their own context, run in parallel in the same box.

A child is a plain `Agent` with its own history and a subset of the parent's tools, so a large
search or a side task does not fill the parent's context; only the child's final text comes back.
By default a child is read-only. A child that may write needs the parent in `auto` (auto-edit)
mode, because a child has no way to ask the person (children run in threads and the permission
question belongs to the parent's turn). Children never get the broker, so they reach no host tool,
and they cannot start children of their own (depth one, which also bounds the cost).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from halagent.tools import Toolset
from halagent.tools.paths import ToolError

if TYPE_CHECKING:
    from halagent.loop import Agent

log = logging.getLogger("halagent.subagents")

MAX_PARALLEL = 4
TASK_SEPARATOR = "\n---\n"
CHILD_MAX_TURNS = 20
CHILD_MAX_TOOL_CALLS = 40
CHILD_MAX_WALL_S = 600
POLL_S = 0.2
MAX_REPORT_CHARS = 6_000
READ_ONLY_TOOLS = ("read", "grep", "glob")
CHILD_PROMPT = (
    "You are a subagent of another agent. Do only the task below, using your tools, and finish with a "
    "short report of what you found or did. The parent sees only that report."
)


@dataclass
class _Child:
    task: str
    agent: Agent | None = None
    report: str = ""


def split_tasks(prompt: object) -> list[str]:
    """The tasks in `prompt`, separated by a line of three dashes; raises `ToolError` when none."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ToolError("invalid_args", "prompt must be the task for the subagent")
    tasks = [t.strip() for t in ("\n" + prompt + "\n").split(TASK_SEPARATOR) if t.strip()]
    if len(tasks) > MAX_PARALLEL:
        raise ToolError("invalid_args", f"at most {MAX_PARALLEL} subagent tasks per call")
    return tasks


def _child_agent(parent: Agent, task: str, writable: bool) -> Agent:
    from halagent.loop import Agent  # noqa: PLC0415 - loop imports this module

    settings = replace(
        parent._requested, broker="", max_turns=CHILD_MAX_TURNS, max_tool_calls=CHILD_MAX_TOOL_CALLS,
        max_wall_s=CHILD_MAX_WALL_S,
    )  # fmt: skip
    toolset = Toolset(parent.toolset.workspace, with_run=writable and "run" in parent.toolset.tools)
    allowed = set(parent.toolset.tools) - {"subagent", "todo", "finish"}
    if not writable:
        allowed &= set(READ_ONLY_TOOLS)
    toolset.tools = {n: t for n, t in toolset.tools.items() if n in allowed}
    child = Agent(settings, toolset, system_extra=CHILD_PROMPT, hooks=parent.hooks)
    child.mode = "auto" if writable else "plan"
    return child


def run_subagents(parent: Agent, args: dict) -> str:
    """Runs the tasks in `args["prompt"]` as parallel children and returns their reports."""
    tasks = split_tasks(args.get("prompt"))
    scope = str(args.get("tools", "read-only")).strip() or "read-only"
    if scope not in ("read-only", "all"):
        raise ToolError("invalid_args", "tools must be 'read-only' or 'all'")
    writable = scope == "all"
    if writable and parent.mode != "auto":
        raise ToolError(
            "not_allowed",
            "a subagent that can write needs auto-edit mode; use tools='read-only' or ask the person",
        )
    children = [_Child(t) for t in tasks]

    def work(child: _Child) -> None:
        try:
            child.agent = _child_agent(parent, child.task, writable)
            result = child.agent.run(child.task)
            child.report = (
                result.text if result.stopped == "done" else f"(stopped: {result.stopped}) {result.text}"
            )
        except Exception as exc:  # noqa: BLE001 - one failed child is a report, not a failed parent turn
            log.warning("subagent failed", exc_info=True)
            child.report = f"(failed: {exc})"
        finally:
            if child.agent:
                child.agent.close()

    threads = [threading.Thread(target=work, args=(c,), daemon=True) for c in children]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        if parent.cancel.is_set():
            for c in children:
                if c.agent:
                    c.agent.cancel.set()
        for t in threads:
            t.join(POLL_S)
    parts = [f"[subagent {n}] {c.report.strip()[:MAX_REPORT_CHARS]}" for n, c in enumerate(children, 1)]
    return "\n\n".join(parts)
