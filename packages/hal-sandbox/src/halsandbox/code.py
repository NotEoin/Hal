"""`hal code`: a `Session` running hal-agent in the box, plus the host model forwarder.

The box gets no network, a read-only root, no capabilities and the host uid, and reaches the model
and the hub only through two unix sockets in a directory mounted read-only at `/run/hal`. The
session (backend, isolated copy, masks, checkpoints, broker) lives in `session.py`; this module
adds the model forwarder, the image build and the process.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .errors import SandboxError
from .forwarder import ModelForwarder
from .session import Session

log = logging.getLogger(__name__)

DEFAULT_IMAGE = "hal-agent:local"
DEFAULT_UPSTREAM = "http://127.0.0.1:8080"  # llama-server; Ollama is http://127.0.0.1:11434
MODEL_SOCKET = "/run/hal/model.sock"
BROKER_SOCKET = "/run/hal/broker.sock"
DOCKER_QUICK_S = 30.0
SESSION_MAX_S = 12 * 3600.0  # upper bound for one interactive session
BUILD_TIMEOUT_S = 1800.0  # a cold pip install on a slow link
PACKAGES = ("hal-kit", "hal-agent")


def docker_runtimes(docker: str = "docker") -> set[str]:
    """The runtimes the docker daemon has registered."""
    try:
        out = subprocess.run(
            [docker, "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=DOCKER_QUICK_S,
            check=True,
        ).stdout
        return set(json.loads(out))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SandboxError(f"cannot query docker: {exc}") from exc


def build_image(repo_root: Path, image: str = DEFAULT_IMAGE) -> None:
    """Builds the agent image from a staged copy of the two packages (not the whole checkout)."""
    dockerfile = repo_root / "packages" / "hal-sandbox" / "docker" / "agent.Dockerfile"
    if not dockerfile.is_file():
        raise SandboxError(f"missing {dockerfile}")
    with tempfile.TemporaryDirectory(prefix="hal-build-") as tmp:
        ctx = Path(tmp)
        for name in PACKAGES:
            shutil.copytree(
                repo_root / "packages" / name,
                ctx / name,
                ignore=shutil.ignore_patterns("__pycache__", "tests", "*.pyc", ".venv"),
            )
        boxside = repo_root / "packages" / "hal-sandbox" / "src" / "halsandbox" / "boxside"
        shutil.copytree(boxside, ctx / "boxside", ignore=shutil.ignore_patterns("__pycache__", "__init__.py"))
        shutil.copy(dockerfile, ctx / "Dockerfile")
        try:
            subprocess.run(["docker", "build", "-t", image, str(ctx)], check=True, timeout=BUILD_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxError(f"docker build failed: {exc}") from exc


def run_code(
    workdir: Path,
    *,
    upstream: str = DEFAULT_UPSTREAM,
    image: str = DEFAULT_IMAGE,
    runtime: str | None = None,
    direct: bool = False,
    agent_args: Sequence[str] = (),
) -> int:
    """Runs hal-agent boxed; returns its exit status.

    By default the box gets an isolated copy of `workdir` and the real project is never mounted
    (RESOLUTIONS I3); review the result with `isolated.accept`. `direct` mounts `workdir` itself and
    needs lockdown `relaxed` or lower.
    """
    workdir = workdir.resolve()
    if not workdir.is_dir():
        raise SandboxError(f"not a directory: {workdir}")
    child = [
        "hal-agent",
        "-C",
        "/work",
        "--endpoint",
        "/run/hal/model.sock",
        "--broker",
        "/run/hal/broker.sock",
    ]
    session = Session.start(
        workdir,
        kind="code",
        runtime=runtime,
        isolated_copy=not direct,
        image=image,
        child_argv=[*child, *agent_args],
        tty=os.isatty(0) and os.isatty(1),
    )
    forwarder = None
    try:
        if session.backend == "docker":
            _require_image(session.docker, image)
        if session.masked:
            log.warning("hal code: %d files masked: %s", len(session.masked), ", ".join(session.masked))
        forwarder = ModelForwarder(session.model_socket, upstream)
        forwarder.start()
        log.info("hal code: %s in %s, model at %s", session.backend, workdir, upstream)
        launch = session.launch
        assert launch is not None
        status = subprocess.run(
            list(launch.argv),
            env=None if launch.env is None else dict(launch.env),
            cwd=launch.cwd,
            start_new_session=launch.new_session,
            timeout=SESSION_MAX_S,
        ).returncode
        if session.iso is not None:
            log.warning(
                "hal code: changes are in isolated session %s; `hal code accept %s` applies them",
                session.iso.sid,
                session.iso.sid,
            )
        return status
    finally:
        if forwarder is not None:
            forwarder.stop()
        session.close()


def _require_image(docker: str, image: str) -> None:
    """Raises `SandboxError` when docker has no `image`; the fix is `hal code --build`."""
    try:
        have = (
            subprocess.run(
                [docker, "image", "inspect", image], capture_output=True, timeout=DOCKER_QUICK_S
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxError(f"cannot query docker: {exc}") from exc
    if not have:
        raise SandboxError(f"image {image} not found; run `hal code --build`")
