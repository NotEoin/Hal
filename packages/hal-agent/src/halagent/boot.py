"""Shared start-up helpers: the prompt extras and the transcript writer of one agent.

Used by the command line and by the in-process ACP entry point, so both build the same agent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from halagent import __version__
from halagent.instructions import load_instructions
from halagent.mcp.client import UserMcp, acp_servers, merge_servers, read_project_servers
from halagent.sessions import SessionStore, SessionWriter, new_session_id
from halagent.settings import AgentSettings
from halagent.skills import discover_skills, render_skills
from halkit import paths

log = logging.getLogger("halagent.boot")


def join(*parts: str) -> str:
    """Joins the non-empty parts with blank lines."""
    return "\n\n".join(p for p in parts if p)


def prompt_extras(root: Path) -> str:
    """The "Project instructions" and "Skills" sections of the system prompt."""
    instructions = load_instructions(root, root, paths.config_dir() / "AGENTS.md")
    skills, _ = discover_skills(root, (paths.config_dir() / "skills",))
    parts = [
        f"## Project instructions\n\n{instructions.text}" if instructions.text else "",
        render_skills(skills),
    ]
    return "\n\n".join(p for p in parts if p)


def open_writer(
    store: SessionStore, resumed: str, root: Path, settings: AgentSettings, sid: str = ""
) -> SessionWriter:
    sid = resumed or sid or new_session_id()
    header = {"id": sid, "agent": f"halagent {__version__}", "mode": "headless", "toolset": "coding",
              "cwd": str(root), "model": settings.model, "api": "openai"}  # fmt: skip
    return SessionWriter(store.path(sid), header)


def start_user_mcp(
    root: Path,
    *,
    configured: Sequence[Mapping[str, Any]] = (),
    project_servers: str = "never",
    sandbox: str = "none",
    restart_limit: int = 1,
    acp: Sequence[Mapping[str, Any]] = (),
    notice: Callable[[str], None] = log.info,
) -> UserMcp | None:
    """Starts the user's MCP servers for one session, or returns None when there are none.

    A project `.mcp.json` is read only with `project_servers = "box"` and a real box.
    """
    project = read_project_servers(root) if project_servers == "box" and sandbox != "none" else {}
    servers = merge_servers(configured, project, acp_servers(acp), notice)
    if not servers:
        return None
    return UserMcp(servers, sandbox=sandbox, root=root, restart_limit=restart_limit, notice=notice)
