"""Shared start-up helpers: the prompt extras and the transcript writer of one agent.

Used by the command line and by the in-process ACP entry point, so both build the same agent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from halagent import __version__
from halagent.instructions import DEFAULT_MAX_CHARS, load_instructions
from halagent.mcp.client import UserMcp, acp_servers, merge_servers, read_project_servers
from halagent.sessions import SessionStore, SessionWriter, new_session_id
from halagent.settings import AgentSettings
from halagent.skills import MAX_LIST_CHARS, MAX_SKILLS, discover_skills, render_skills
from halkit import paths

log = logging.getLogger("halagent.boot")


def join(*parts: str) -> str:
    """Joins the non-empty parts with blank lines."""
    return "\n\n".join(p for p in parts if p)


def prompt_extras(
    root: Path,
    *,
    agents_md: bool = True,
    max_chars: int = DEFAULT_MAX_CHARS,
    user_file: Path | None = None,
    skills_enabled: bool = True,
    skill_dirs: Sequence[Path] = (),
    max_listed: int = MAX_SKILLS,
    skills_chars: int = MAX_LIST_CHARS,
) -> str:
    """The "Project instructions" and "Skills" sections of the system prompt.

    The defaults read the user's config directory; `agent.json` overrides every argument.
    """
    user_file = user_file or paths.config_dir() / "AGENTS.md"
    text = load_instructions(root, root, user_file, max_chars).text if agents_md else ""
    skills = (
        discover_skills(root, tuple(skill_dirs) or (paths.config_dir() / "skills",))[0]
        if skills_enabled
        else []
    )
    parts = [
        f"## Project instructions\n\n{text}" if text else "",
        render_skills(skills, max_listed, skills_chars),
    ]
    return "\n\n".join(p for p in parts if p)


def open_writer(
    store: SessionStore, resumed: str, root: Path, settings: AgentSettings, sid: str = ""
) -> SessionWriter:
    """Opens the session writer for a new or resumed headless session and writes its header."""
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
