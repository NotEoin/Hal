"""The `subagent` tool: child agents with their own context, run in parallel in the same box.

A child is a plain `Agent` with its own history and a subset of the parent's tools, so a large
search or a side task does not fill the parent's context; only the child's final text comes back.
By default a child is read-only. A child that may write needs the parent in `auto` (auto-edit)
mode, because a child has no way to ask the person (children run in threads and the permission
question belongs to the parent's turn). Children never get the broker, so they reach no host tool,
and they cannot start children of their own (depth one, which also bounds the cost).

A task can name an agent: the built-ins `explore` and `review`, or a `.hal/agents/<name>.md` file whose
frontmatter sets `description`, `tools` (comma list), `model` and `max_turns` and whose body is the
child's instructions. The tools are capped by the parent's own set, so a file cannot grant more than
the parent has. At most `slots` children decode at once, where `slots` is what the gateway reports
for the model (`meta.slots`) or `DEFAULT_SLOTS`: more children than the server has slots only queue.
Each child reports to the parent's `on_update` as `{"kind": "subagent", ...}` for the TUI.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from halagent.skills import parse_frontmatter
from halagent.tools import Tool, Toolset
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
READ_ONLY_TOOLS = ("read", "grep", "glob", "repo_map")
WRITE_TOOLS = ("edit", "patch", "write", "run")
DEFAULT_SLOTS = 2  # a single llama-server runs one or two requests at a time
AGENTS_DIR = ".hal/agents"
MAX_AGENT_FILE_BYTES = 16_000
CHILD_PROMPT = (
    "You are a subagent of another agent. Do only the task below, using your tools, and finish with a "
    "short report of what you found or did. The parent sees only that report."
)


@dataclass(frozen=True)
class AgentDef:
    """A named subagent: its instructions, tool names, optional model and turn cap."""

    name: str
    description: str
    prompt: str
    tools: tuple[str, ...]
    model: str = ""
    max_turns: int = CHILD_MAX_TURNS

    @property
    def writable(self) -> bool:
        """True when the agent may change files or run commands."""
        return any(t in WRITE_TOOLS for t in self.tools)


BUILTIN_AGENTS = {
    "explore": AgentDef(
        "explore",
        "Reads code and answers a question about it, read-only.",
        "Find the answer in the code with grep, glob and read. Quote paths and line numbers. Do not guess.",
        READ_ONLY_TOOLS,
    ),
    "review": AgentDef(
        "review",
        "Reviews the current changes for bugs, read-only.",
        "Review the change described in the task. Use git (diff, status) and read. List concrete bugs with "
        "file and line, most serious first. Say 'no problems found' if there are none.",
        (*READ_ONLY_TOOLS, "git"),
    ),
}


def _load_agent(path: Path) -> AgentDef:
    text = path.read_text(encoding="utf-8", errors="replace")[:MAX_AGENT_FILE_BYTES]
    fm = parse_frontmatter(text)
    body = text.split("---", 2)[2].strip()
    tools = tuple(t.strip() for t in str(fm.get("tools", "")).split(",") if t.strip()) or READ_ONLY_TOOLS
    try:
        turns = int(str(fm.get("max_turns", CHILD_MAX_TURNS)))
    except ValueError as exc:
        raise ValueError("max_turns must be a number") from exc
    return AgentDef(
        str(fm.get("name") or path.stem), str(fm.get("description", "")), body, tools,
        str(fm.get("model", "")), max(1, min(turns, CHILD_MAX_TURNS)),
    )  # fmt: skip


def discover_agents(root: Path) -> dict[str, AgentDef]:
    """The built-ins plus the valid `.hal/agents/*.md` files of `root`; a bad file is logged and skipped."""
    found = dict(BUILTIN_AGENTS)
    folder = root / AGENTS_DIR
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            agent = _load_agent(path)
        except (ValueError, OSError, IndexError) as exc:
            log.warning("agent file %s skipped: %s", path, exc)
            continue
        found[agent.name] = agent
    return found


def describe_agents(agents: dict[str, AgentDef]) -> str:
    """The tool-description suffix that lists the named agents, in a fixed order."""
    rows = "; ".join(f"{n}: {a.description or 'no description'}" for n, a in sorted(agents.items()))
    return f" Named agents: {rows}."


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


def _child_agent(
    parent: Agent, definition: AgentDef | None, writable: bool, on_update: Callable[[dict], None]
) -> Agent:
    from halagent.loop import Agent  # noqa: PLC0415 - loop imports this module

    settings = replace(
        parent._requested, broker="", max_turns=definition.max_turns if definition else CHILD_MAX_TURNS,
        max_tool_calls=CHILD_MAX_TOOL_CALLS, max_wall_s=CHILD_MAX_WALL_S,
    )  # fmt: skip
    if definition and definition.model:
        settings = replace(settings, model=parent.resolve_model(definition.model))
    wants_run = any(t in ("run", "git") for t in (definition.tools if definition else ())) or writable
    toolset = Toolset(parent.toolset.workspace, with_run=wants_run and "run" in parent.toolset.tools)
    allowed = set(parent.toolset.tools) - {"subagent", "todo", "finish"}
    if definition:
        allowed &= set(definition.tools)
    elif not writable:
        allowed &= set(READ_ONLY_TOOLS)
    toolset.tools = {n: t for n, t in toolset.tools.items() if n in allowed}
    extra = CHILD_PROMPT + (f"\n\n{definition.prompt}" if definition else "")
    child = Agent(settings, toolset, system_extra=extra, hooks=parent.hooks, on_update=on_update)
    # plan mode refuses `git`; a read-only child gets the wrapped `git` instead, so it runs in auto
    child.mode = "auto" if writable or "git" in toolset.tools else "plan"
    return child


def _read_only_git(tool: Tool) -> Tool:
    """The `git` tool without `commit`, for a child that may not write."""

    def call(args: dict) -> str:
        if str(args.get("action", "")).strip() == "commit":
            raise ToolError("not_allowed", "this subagent is read-only: git commit is not allowed")
        return tool.fn(args)

    return replace(tool, fn=call)


def _forward(parent: Agent, number: int, name: str) -> Callable[[dict], None]:
    """An `on_update` for child `number` that relays its tool calls to the parent's listener."""

    def relay(update: dict) -> None:
        if update.get("kind") == "tool_call":
            parent.on_update(
                {"kind": "subagent", "id": number, "agent": name, "phase": "tool", "tool": update["name"]}
            )

    return relay


def run_subagents(parent: Agent, args: dict) -> str:
    """Runs the tasks in `args["prompt"]` as parallel children and returns their reports."""
    tasks = split_tasks(args.get("prompt"))
    name = str(args.get("agent") or "").strip()
    definition = None
    if name:
        known = discover_agents(parent.toolset.workspace.root)
        if name not in known:
            raise ToolError("invalid_args", f"unknown agent {name!r}; known: {', '.join(sorted(known))}")
        definition = known[name]
    scope = str(args.get("tools", "read-only")).strip() or "read-only"
    if scope not in ("read-only", "all"):
        raise ToolError("invalid_args", "tools must be 'read-only' or 'all'")
    writable = definition.writable if definition else scope == "all"
    if writable and parent.mode != "auto":
        raise ToolError(
            "not_allowed",
            "a subagent that can write needs auto-edit mode; use tools='read-only' or ask the person",
        )
    children = [_Child(t) for t in tasks]
    slots = threading.BoundedSemaphore(max(1, parent.model_slots))

    def work(number: int, child: _Child) -> None:
        label = definition.name if definition else "subagent"
        with slots:
            parent.on_update(
                {"kind": "subagent", "id": number, "agent": label, "phase": "start", "task": child.task[:200]}
            )
            try:
                child.agent = _child_agent(parent, definition, writable, _forward(parent, number, label))
                if not writable and "git" in child.agent.toolset.tools:
                    child.agent.toolset.tools["git"] = _read_only_git(child.agent.toolset.tools["git"])
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
                parent.on_update({"kind": "subagent", "id": number, "agent": label, "phase": "done"})

    threads = [threading.Thread(target=work, args=(n, c), daemon=True) for n, c in enumerate(children, 1)]
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
