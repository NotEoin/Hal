"""In-process ACP entry point: `serve` runs the same agent server the command line runs.

halcore and `hal-agent --acp` both call it, so the two paths cannot drift (03-agent 3.14.1).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Any

from halagent import boot
from halagent.acp.server import AcpServer
from halagent.assistant import AssistantOptions, build_assistant
from halagent.loop import Agent
from halagent.profiles import Profile
from halagent.sessions import SessionError, SessionStore
from halagent.settings import AgentSettings
from halagent.tools import Toolset, Workspace
from halkit import paths


def serve(
    settings: AgentSettings,
    root: Path,
    rfile: IO[str],
    wfile: IO[str],
    *,
    store: SessionStore | None = None,
    no_session: bool = False,
    profile: Profile | None = None,
    extra: str = "",
    toolset: str = "coding",
    assistant: AssistantOptions | None = None,
    mcp: Mapping[str, Any] | None = None,
    extras: Mapping[str, Any] | None = None,
    permissions: str = "auto",
    first_session_id: str = "",
) -> None:
    """Serves ACP on the two text streams until `rfile` closes; stdout of a process carries only protocol."""
    store = store or SessionStore(paths.state_dir() / "sessions")
    user_skills = tuple(d for d in (paths.config_dir() / "skills",) if d.is_dir())

    def factory(
        cwd: Path, sid: str, on_update: Callable[[dict], None], session=None, mcp_servers=()
    ) -> Agent:  # noqa: ANN001 - a record sink or None
        writer = session or (None if no_session else boot.open_writer(store, "", cwd, settings, sid))
        if toolset == "assistant":  # no project instructions, skills or coding tools (REQ-AGT-102)
            return build_assistant(
                settings, assistant or AssistantOptions(), cwd,
                session=writer, on_update=on_update, profile=profile, system_extra=extra,
            )  # fmt: skip
        user_mcp = boot.start_user_mcp(cwd, acp=mcp_servers, **(mcp or {}))
        return Agent(
            settings,
            Toolset(
                Workspace(cwd, readonly=user_skills, protect=settings.tuning.protect),
                syntax_guard=settings.tuning.syntax_guard,
            ),
            user_mcp=user_mcp,
            session=writer,
            system_extra=boot.join(boot.prompt_extras(cwd, **(extras or {})), extra),
            on_update=on_update,
            profile=profile,
        )

    def load_records(sid: str) -> list[dict]:
        try:
            return store.load(sid)
        except SessionError:
            return []

    AcpServer(
        root, factory, rfile, wfile, load_records=load_records, first_session_id=first_session_id,
        mode=permissions,
    ).serve()  # fmt: skip
