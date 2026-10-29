"""`hal code` session: hardened docker run of hal-agent plus the host model forwarder and hub broker.

The container gets no network, a read-only root, no capabilities and the host uid. It reaches the
model and the hub only through two unix sockets in a directory mounted read-only at `/run/hal`.
`runsc` is preferred over `runc` when docker lists it, because it adds a user-space kernel.
The container's `/work/.git` is mounted read-only so the agent cannot rewrite hooks or config.
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

from halkit import paths

from .broker import Broker
from .forwarder import ModelForwarder

log = logging.getLogger(__name__)

DEFAULT_IMAGE = "hal-agent:local"
DEFAULT_UPSTREAM = "http://127.0.0.1:8080"  # llama-server; Ollama is http://127.0.0.1:11434
DOCKER_QUICK_S = 30.0
SESSION_MAX_S = 12 * 3600.0  # upper bound for one interactive session
BUILD_TIMEOUT_S = 1800.0  # a cold pip install on a slow link
TMP_SIZE = "256m"
PACKAGES = ("hal-kit", "hal-agent")


class SandboxError(RuntimeError):
    """The secure path cannot start; the message says what to fix."""


def pick_runtime(docker: str = "docker") -> str:
    """Returns `runsc` when docker has it registered, else `runc`."""
    try:
        out = subprocess.run(
            [docker, "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=DOCKER_QUICK_S,
            check=True,
        ).stdout
        return "runsc" if "runsc" in json.loads(out) else "runc"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SandboxError(f"cannot query docker: {exc}") from exc


def docker_args(
    workdir: Path,
    sockdir: Path,
    *,
    image: str = DEFAULT_IMAGE,
    runtime: str = "runc",
    uid: int | None = None,
    gid: int | None = None,
    tty: bool = False,
    agent_args: Sequence[str] = (),
) -> list[str]:
    """The `docker run` argv for one boxed hal-agent session."""
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    args = [
        "docker",
        "run",
        "--rm",
        "-i",
        *(["-t"] if tty else []),
        "--runtime",
        runtime,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{uid}:{gid}",
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,size={TMP_SIZE}",
        "-e",
        "HOME=/tmp",
        "-v",
        f"{workdir}:/work:rw",
    ]
    if (workdir / ".git").exists():
        args += ["-v", f"{workdir / '.git'}:/work/.git:ro"]
    args += ["-v", f"{sockdir}:/run/hal:ro", "-w", "/work", image]
    args += ["-C", "/work", "--endpoint", "/run/hal/model.sock", "--broker", "/run/hal/hub.sock"]
    return args + list(agent_args)


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
    agent_args: Sequence[str] = (),
) -> int:
    """Runs hal-agent boxed on `workdir`; returns its exit status."""
    workdir = workdir.resolve()
    if not workdir.is_dir():
        raise SandboxError(f"not a directory: {workdir}")
    if shutil.which("docker") is None:
        raise SandboxError("docker is not installed; hal code needs it")
    try:
        have = (
            subprocess.run(
                ["docker", "image", "inspect", image], capture_output=True, timeout=DOCKER_QUICK_S
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxError(f"cannot query docker: {exc}") from exc
    if not have:
        raise SandboxError(f"image {image} not found; run `hal code --build`")
    runtime = runtime or pick_runtime()
    base = paths.ensure_private_dir(paths.runtime_dir())
    sockdir = paths.ensure_private_dir(Path(tempfile.mkdtemp(prefix="code-", dir=base)))
    sockdir.chmod(0o700)
    forwarder = broker = None
    try:
        forwarder = ModelForwarder(paths.check_socket_path(sockdir / "model.sock"), upstream)
        broker = Broker(paths.check_socket_path(sockdir / "hub.sock"))
        forwarder.start()
        broker.start()
        log.info("hal code: %s in %s, model at %s", runtime, workdir, upstream)
        argv = docker_args(
            workdir,
            sockdir,
            image=image,
            runtime=runtime,
            tty=os.isatty(0) and os.isatty(1),
            agent_args=agent_args,
        )
        return subprocess.run(argv, timeout=SESSION_MAX_S).returncode
    finally:
        for server in (forwarder, broker):
            if server is not None:
                server.stop()
        shutil.rmtree(sockdir, ignore_errors=True)
