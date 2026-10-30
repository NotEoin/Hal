"""The assistant toolset: broker tools only, chosen per turn, with the claims check.

`build_assistant` configures the same `Agent` loop the coding toolset uses (03-agent 3.8.2), so
repair, budgets, context and providers are shared. No coding tool is offered and no user MCP
server is started (REQ-AGT-102).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from halagent.settings import AgentSettings

ASSISTANT_WALL_S = 300  # one channel turn, 03-agent 3.8.2
DEFAULT_TOOLS_PER_TURN = 8
DEFAULT_TOOL_ROUNDS = 5
DEFAULT_HISTORY_TURNS = 10


@dataclass(frozen=True)
class AssistantOptions:
    """The `assistant` table of `agent.json` plus the channel."""

    channel: str = "chat"
    user_name: str = ""
    persona: str = ""
    timezone: str = ""
    tools_per_turn: int = DEFAULT_TOOLS_PER_TURN
    max_tool_rounds: int = DEFAULT_TOOL_ROUNDS
    history_turns: int = DEFAULT_HISTORY_TURNS
    claims_check: bool = True
    log_conversation: bool = False


def assistant_settings(settings: AgentSettings, options: AssistantOptions) -> AgentSettings:
    """Settings with the assistant's turn budgets: rounds plus one claims retry."""
    return replace(
        settings,
        max_turns=options.max_tool_rounds + 1,
        max_tool_calls=options.max_tool_rounds * 2,
        max_wall_s=ASSISTANT_WALL_S,
    )


def build_assistant(settings: AgentSettings, options: AssistantOptions, root: Path, **kwargs: object):  # noqa: ANN201 - Agent, imported late to avoid a cycle
    """An `Agent` with the assistant toolset: no coding tools, broker tools per turn."""
    from halagent.loop import Agent
    from halagent.tools import Toolset, Workspace

    toolset = Toolset(Workspace(root), coding=False)
    return Agent(assistant_settings(settings, options), toolset, assistant=options, **kwargs)  # type: ignore[arg-type]  # kwargs are Agent's own
