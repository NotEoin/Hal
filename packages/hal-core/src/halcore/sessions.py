"""Boxed agent sessions for every channel: the host side of RESOLUTIONS I1 (always boxed).

`BoxedAgent` starts a `halsandbox.Session`, the model forwarder and the agent process in the box,
and hands out the agent's stdio as an ACP transport. `open_assistant` adds an `AcpClient` and one
conversation for `hal chat`; `hal acp` uses `BoxedAgent` directly and pumps the transport.
Design decisions:

- The agent is started with flags (`-C`, `--endpoint`, `--broker`) rather than a run file: the
  settings run file (06 step 13) is not built yet.
- The only host path the box sees is the model socket and the broker socket (and `/scratch`,
  `/work` as `halsandbox` mounts them). Backend `none` runs the agent as a host process (the
  `hal-agent` subprocess, not inside `hal`: one transport for both) and an assistant channel
  needs the recorded consent of `halcore.unboxed` first (REQ-CLI-96).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from halsandbox import code as sandbox
from halsandbox.forwarder import ModelForwarder
from halsandbox.session import Session

from . import config, settings, unboxed
from .acp.client import AcpClient, ClientHandler
from .acp.transport import SubprocessTransport

log = logging.getLogger(__name__)

AGENT_CONFIG = "/run/hal/agent.json"  # where the sandbox mounts the run file (halsandbox.session.DEFAULT_CHILD)
BOX_WORK = "/work"
BOX_SCRATCH = "/scratch"
STDERR_LOG = "agent-stderr.log"
SURFACE_ACP = "acp"  # REQ-SEC-93 surface name for editor and channel prompts
SURFACE_TUI = "tui"
SURFACE_HEADLESS = "headless"  # `hal -p`


@dataclass
class BoxedAgent:
    """A started box with its model forwarder and the agent's stdio as `transport`."""

    session: Session
    forwarder: ModelForwarder
    transport: SubprocessTransport

    @classmethod
    async def start(
        cls,
        *,
        kind: str,
        project: Path | None,
        channel: str,
        upstream: str | None = None,
        model: str | None = None,
        image: str = sandbox.DEFAULT_IMAGE,
        runtime: str | None = None,
        direct: bool = False,
        agent_args: Sequence[str] = (),
    ) -> BoxedAgent:
        """Starts the box for a `code` or `chat` session; raises `SandboxError` when it cannot."""
        shared = config.shared()
        upstream = upstream or os.environ.get("HAL_CODE_UPSTREAM") or shared["ollama_url"]
        model = model or os.environ.get("HAL_AGENT_MODEL") or shared["chat_model"]
        coding = kind in ("code", "job") and project is not None
        child = [
            "hal-agent", "--acp", "-C", BOX_WORK if coding else BOX_SCRATCH,
            "--endpoint", sandbox.MODEL_SOCKET, "--broker", sandbox.BROKER_SOCKET,
            "--model", model, "--config", AGENT_CONFIG, *([] if coding else ["--toolset", "assistant"]), *agent_args,
        ]  # fmt: skip
        session = Session.start(
            project, kind=kind, runtime=runtime, isolated_copy=False if direct else None,
            image=image, channel=channel, child_argv=child, interactive=os.isatty(0),
            run_files=settings.run_files(
                mode="code" if coding else "assistant", channel=channel, host_root=str(project or ""), model=model,
                sandbox=f"docker-{runtime}" if runtime else "docker-runsc",
                overrides={"prompt": {"repo_map_auto": bool(config.load().get("agent", {}).get("repo_map_auto", False))}},
            ),
        )  # fmt: skip
        forwarder = None
        try:
            if session.backend == "none" and kind == "chat":
                unboxed.require_consent(channel)
            if session.backend == "docker":
                sandbox._require_image(session.docker, image)
            forwarder = ModelForwarder(session.model_socket, upstream)
            forwarder.start()
            launch = session.launch
            assert launch is not None
            transport = await SubprocessTransport.spawn(
                list(launch.argv),
                env=None if launch.env is None else dict(launch.env),
                stderr_log=session.rd.root / STDERR_LOG,
                cwd=launch.cwd,
                new_session=launch.new_session,
            )
        except BaseException:
            if forwarder is not None:
                forwarder.stop()
            session.close()
            raise
        log.info("%s box %s on %s, model at %s", kind, session.id, session.backend, upstream)
        return cls(session, forwarder, transport)

    async def close(self) -> None:
        """Stops the agent, the forwarder and the session."""
        await self.transport.close()
        self.forwarder.stop()
        self.session.close()


class AssistantSession:
    """One assistant conversation in an assistant box (06-core-cli "Assistant channels")."""

    def __init__(self, box: BoxedAgent, client: AcpClient, acp_session_id: str) -> None:
        self.box, self.client, self.acp_session_id = box, client, acp_session_id

    async def prompt(self, text: str) -> str:
        """Tells the broker what the person said, then runs one turn; returns the ACP stop reason."""
        self.box.session.broker.on_human_message(text, SURFACE_TUI)
        return await self.client.prompt(self.acp_session_id, [{"type": "text", "text": text}])

    async def new_conversation(self) -> str:
        """Starts a fresh conversation in the same box."""
        self.acp_session_id = await self.client.new_session(BOX_SCRATCH, [])
        return self.acp_session_id

    async def cancel(self) -> None:
        """Cancels the running turn."""
        await self.client.cancel(self.acp_session_id)

    async def close(self) -> None:
        """Closes the client, the box and the session."""
        await self.client.close()
        await self.box.close()


async def open_assistant(channel: str, handler: ClientHandler, **box_args: object) -> AssistantSession:
    """Starts an assistant box for `channel` and opens its first conversation."""
    box = await BoxedAgent.start(kind="chat", project=None, channel=channel, **box_args)  # type: ignore[arg-type]
    client = AcpClient(box.transport, handler)
    try:
        await client.initialize()
        sid = await client.new_session(BOX_SCRATCH, [])
    except BaseException:
        await client.close()
        await box.close()
        raise
    return AssistantSession(box, client, sid)
