"""Architect/editor split: a strong model plans read-only, then the editor model applies the plan.

With `[agent.models]` naming an `architect` and an `editor`, a split turn first runs the architect
as a read-only child (it can look, never change) and then hands its plan to the agent itself,
running on the editor model. The planner thinks hard once and the fast model does the typing, which
suits one consumer GPU where the big model is slow. The architect shows up in the TUI as a
`subagent` update named `architect`. The agent returns to its own model when the turn ends.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from halagent.subagents import CHILD_MAX_TOOL_CALLS, CHILD_MAX_WALL_S, READ_ONLY_TOOLS
from halagent.tools import Toolset

if TYPE_CHECKING:
    from halagent.loop import Agent, RunResult

log = logging.getLogger("halagent.architect")

ARCHITECT_TURNS = 12
ARCHITECT_PROMPT = (
    "You are the architect. Study the code with your tools, then write a numbered plan of the exact "
    "edits to make: file, what to change, and why. Do not edit anything and do not write the full "
    "code. An editor will carry out your plan."
)
EDITOR_TEMPLATE = "{task}\n\nPlan from the architect (follow it, check it against the code):\n{plan}"


def split_ready(agent: Agent) -> bool:
    """True when both the `architect` and `editor` roles are configured."""
    models = agent._requested.models
    return bool(models.get("architect") and models.get("editor"))


def run_split(agent: Agent, prompt: str, images: list[dict]) -> RunResult:
    """Runs `prompt` as architect then editor; raises `ValueError` when a role is missing."""
    from halagent.loop import Agent as AgentClass  # noqa: PLC0415 - loop imports this module

    if not split_ready(agent):
        raise ValueError("architect/editor split needs [agent.models] architect and editor")
    models = agent._requested.models
    settings = replace(
        agent._requested, model=models["architect"], broker="", max_turns=ARCHITECT_TURNS,
        max_tool_calls=CHILD_MAX_TOOL_CALLS, max_wall_s=CHILD_MAX_WALL_S,
    )  # fmt: skip
    toolset = Toolset(agent.toolset.workspace, with_run=False)
    toolset.tools = {n: t for n, t in toolset.tools.items() if n in READ_ONLY_TOOLS}
    planner = AgentClass(settings, toolset, system_extra=ARCHITECT_PROMPT)
    planner.mode = "plan"
    agent.on_update(
        {"kind": "subagent", "id": 0, "agent": "architect", "phase": "start", "task": prompt[:200]}
    )
    try:
        plan = planner.run(prompt).text.strip()
    finally:
        planner.close()
        agent.on_update({"kind": "subagent", "id": 0, "agent": "architect", "phase": "done"})
    main = agent._requested.model
    agent.switch_model(models["editor"])
    try:
        task = EDITOR_TEMPLATE.format(task=prompt, plan=plan) if plan else prompt
        return agent._run(task, images)
    finally:
        agent.switch_model(main)
