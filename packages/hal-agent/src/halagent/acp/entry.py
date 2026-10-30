"""In-process ACP entry point: `serve` runs the same agent server the command line runs.

halcore and `hal-agent --acp` both call it, so the two paths cannot drift (03-agent 3.14.1).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import IO

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
) -> None:
    """Serves ACP on the two text streams until `rfile` closes; stdout of a process carries only protocol."""
    store = store or SessionStore(paths.state_dir() / "sessions")
    user_skills = tuple(d for d in (paths.config_dir() / "skills",) if d.is_dir())

    def factory(cwd: Path, sid: str, on_update: Callable[[dict], None], session=None) -> Agent:  # noqa: ANN001 - a record sink or None
        writer = session or (None if no_session else boot.open_writer(store, "", cwd, settings, sid))
        if toolset == "assistant":  # no project instructions, skills or coding tools (REQ-AGT-102)
            return build_assistant(
                settings, assistant or AssistantOptions(), cwd,
                session=writer, on_update=on_update, profile=profile, system_extra=extra,
            )  # fmt: skip
        return Agent(
            settings,
            Toolset(Workspace(cwd, readonly=user_skills)),
            session=writer,
            system_extra=boot.join(boot.prompt_extras(cwd), extra),
            on_update=on_update,
            profile=profile,
        )

    def load_records(sid: str) -> list[dict]:
        try:
            return store.load(sid)
        except SessionError:
            return []

    AcpServer(root, factory, rfile, wfile, load_records=load_records).serve()
