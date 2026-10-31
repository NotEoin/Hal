"""`hal box`: runs an agent CLI (claude, qwen, opencode, codex, crush, goose) in a box.

The profile in `boxcli` writes the CLI's config into the session's home seed; the box reaches the
model only through `hal-relay` and the broker through `hal-mcp-bridge`. The host starts the model
forwarder through `start_model`, so halsandbox never imports the gateway (REQ-BOX-32).
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from . import boximages
from .boxcli import BoxContext, profile
from .code import SESSION_MAX_S
from .errors import SandboxError
from .session import Session

log = logging.getLogger(__name__)

DEFAULT_MODEL = "hal-code"  # the role alias of RESOLUTIONS D1
RELAY_PORT = 8741  # REQ-BOX-37


class Closeable(Protocol):
    """What `start_model` returns: something the caller closes after the box exits."""

    def close(self) -> None:
        """Stops the model forwarder."""


def run(
    cli: str,
    cli_args: Sequence[str],
    *,
    project: Path,
    start_model: Callable[[Path, str], Closeable],
    backend: str = "auto",
    direct: bool = False,
    image: str | None = None,
    model: str = DEFAULT_MODEL,
    ask: bool = False,
    mcp: bool = True,
    checkpoints: bool = True,
    context_window: int | None = None,
    runtime: str | None = None,
    approver: object = None,
) -> int:
    """Runs `cli` boxed in `project` and returns its exit code (REQ-BOX-59, REQ-BOX-62).

    The copy of `project` is isolated unless `direct` (which needs lockdown `relaxed`). Docker is the
    only backend; any other choice is an error.
    """
    if backend not in ("auto", "docker"):
        raise SandboxError(f"hal box runs on docker only, not {backend!r}; see `hal-sandbox doctor`")
    writer = profile(cli)
    tag = image or boximages.newest(cli)
    if tag is None:
        raise SandboxError(f"no image for {cli}; run `hal box image build {cli}`")
    ctx = BoxContext(
        model=model,
        auto_approve=not ask,
        mcp=mcp,
        user_args=tuple(cli_args),
        **({"context_window": context_window} if context_window else {}),
    )
    launch = writer.launch(ctx)
    session = Session.start(
        project,
        kind="box",
        backend="docker",
        runtime=runtime,
        isolated_copy=not direct,
        image=tag,
        approver=approver,
        checkpoints=checkpoints,
        home_seed=launch.home_seed,
        extra_env=launch.env,
        child_argv=launch.argv,
        tty=os.isatty(0) and os.isatty(1),
        relay_port=RELAY_PORT,
    )
    forwarder: Closeable | None = None
    try:
        forwarder = start_model(session.model_socket, session.id)
        spec = session.launch
        assert spec is not None
        status = subprocess.run(
            list(spec.argv),
            env=None if spec.env is None else dict(spec.env),
            cwd=spec.cwd,
            start_new_session=spec.new_session,
            timeout=SESSION_MAX_S,
        ).returncode
        if session.checkpoints is not None:
            session.checkpoint("session end")
            report = session.report()
            log.warning("hal box: %s", report.stat.strip() or "no changes")
        if session.iso is not None:
            log.warning("hal box: changes are in isolated session %s; `hal code accept %s` applies them",
                        session.iso.sid, session.iso.sid)  # fmt: skip
        return status
    finally:
        if forwarder is not None:
            forwarder.close()
        session.close()
