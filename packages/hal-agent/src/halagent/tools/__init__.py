"""The coding toolset: `read`, `grep`, `glob`, `edit`, `write`, `run`, `todo`, `subagent`, `finish`.

`Toolset` binds the tools to a `Workspace`. Schemas are flat (string and integer properties only)
and offered in a fixed order. `finish` is offered in task mode only; `run` is left out when the
caller has no sandbox for it. `Agent.__init__` binds `subagent` to its agent.
Tools raise `ToolError`; the loop renders it for the model.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from halagent.tools import files, plan, run, search
from halagent.tools.paths import ToolError, Workspace

__all__ = ["Tool", "ToolError", "Toolset", "Workspace", "render_error"]


@dataclass(frozen=True)
class Tool:
    """A callable tool; `properties` is the JSON schema of its flat arguments."""

    name: str
    kind: str  # Hal metadata for ACP: read, search, edit, execute, think or other
    description: str
    properties: dict[str, dict]
    required: tuple[str, ...]
    fn: Callable[[dict], str]
    free: bool = False

    def spec(self) -> dict:
        """The OpenAI function-tool definition."""
        params = {"type": "object", "properties": self.properties, "required": list(self.required)}
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": params},
        }


def _unbound_subagent(args: dict) -> str:
    raise ToolError("unavailable", "subagents are not available here")


def render_error(exc: ToolError) -> str:
    """The error as the JSON the model reads."""
    return json.dumps({"status": "error", "error_type": exc.error_type, "message": exc.message})


class Toolset:
    """The coding tools of one workspace, in the order the model sees them."""

    def __init__(
        self, workspace: Workspace | Path | str, *, task_mode: bool = False, with_run: bool = True,
        syntax_guard: bool = True, coding: bool = True,
    ) -> None:  # fmt: skip
        self.workspace = workspace if isinstance(workspace, Workspace) else Workspace(Path(workspace))
        ws = self.workspace
        bound: dict[str, Callable[[dict], str]] = {
            "read": lambda a: files.read(ws, a),
            "grep": lambda a: search.grep(ws, a),
            "glob": lambda a: search.glob(ws, a),
            "edit": lambda a: files.edit(ws, a, syntax_guard=syntax_guard),
            "write": lambda a: files.write(ws, a),
            "run": lambda a: run.run(ws, a),
            "subagent": _unbound_subagent,
            "todo": plan.todo,
            "finish": plan.finish,
        }
        skip = {"finish"} if not task_mode else set()
        skip |= {"run"} if not with_run else set()
        rows = json.loads(resources.files("halagent.tools").joinpath("schemas.json").read_text("utf-8"))
        tools = [
            Tool(
                r["name"],
                r["kind"],
                r["description"],
                r["properties"],
                tuple(r["required"]),
                bound[r["name"]],
                r["free"],
            )
            for r in rows
            if r["name"] not in skip
        ]
        # the assistant toolset offers no coding tool (REQ-AGT-102)
        self.tools: dict[str, Tool] = {t.name: t for t in tools} if coding else {}
