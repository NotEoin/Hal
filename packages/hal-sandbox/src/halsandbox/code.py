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

from . import config as sbconfig
from . import detect, gitlayout, isolated, projects
from . import scratch as scratch_dir
from .backends.docker import LaunchSpec, docker_argv
from .broker import Broker
from .env import box_env
from .errors import SandboxError
from .forwarder import ModelForwarder
from .limits import Limits
from .lockdown import Policy
from .mask import find_masked, resolve_patterns
from .runtime import RuntimeDir, pidstart, utc_now

log = logging.getLogger(__name__)

DEFAULT_IMAGE = "hal-agent:local"
DEFAULT_UPSTREAM = "http://127.0.0.1:8080"  # llama-server; Ollama is http://127.0.0.1:11434
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


def docker_args(
    workdir: Path,
    rd: RuntimeDir,
    *,
    image: str = DEFAULT_IMAGE,
    runtime: str = "runc",
    uid: int | None = None,
    gid: int | None = None,
    tty: bool = False,
    scratch: Path | None = None,
    limits: Limits | None = None,
    masked: Sequence[str] = (),
    agent_args: Sequence[str] = (),
) -> list[str]:
    """The `docker run` argv for one boxed hal-agent session (see `backends.docker`)."""
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    pid = os.getpid()
    spec = LaunchSpec(
        kind="code",
        sid=rd.sid,
        image=image,
        runtime=runtime,
        work=workdir,
        session_root=rd.root,
        uid=uid,
        gid=gid,
        limits=limits or Limits.from_config(),
        env=box_env("code", rd.sid, "docker", os.environ),
        child=[
            "-C",
            "/work",
            "--endpoint",
            "/run/hal/model.sock",
            "--broker",
            "/run/hal/broker.sock",
            *agent_args,
        ],
        pid=pid,
        pidstart=pidstart(pid),
        started=utc_now(),
        tty=tty,
        git_mounts=gitlayout.discover(workdir),
        masked=masked,
        scratch=scratch,
        entrypoint=None,  # the image's own `hal-agent` until hal-box-entry exists
    )
    return docker_argv(spec)


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
    allcfg = sbconfig.load_all()
    cfg = allcfg["sandbox"]
    policy = Policy.for_level(str(cfg["lockdown"]))
    if direct:
        policy.check_direct()
    probe = detect.detect({**cfg, "runtime": runtime or cfg["runtime"]}, policy)
    if probe.backend != "docker":
        raise SandboxError(f"hal code supports the docker backend only for now (detected {probe.backend})")
    for note in (probe.detail,) if probe.runtime == "runc" else ():
        log.warning("hal code: %s", note)
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
    runtime = probe.runtime or "runc"
    scratch = scratch_dir.prepare(workdir, int(cfg["scratch_max_bytes"]))
    iso = None if direct else isolated.create(workdir, int(allcfg["isolated"]["max_copy_bytes"]))
    mount = workdir if iso is None else iso.work
    rd = RuntimeDir.create()
    forwarder = broker = None
    try:
        forwarder = ModelForwarder(rd.socket("model.sock"), upstream)
        broker = Broker(rd.socket("broker.sock"), paranoia_floor=policy.paranoia_floor)
        forwarder.start()
        broker.start()
        masked: tuple[str, ...] = ()
        if not projects.is_set(workdir, "unmask"):
            masked = find_masked(mount, resolve_patterns(cfg["mask"]))
        if masked:
            log.warning(
                "hal code: %d files masked: %s (unmask with hal-sandbox)", len(masked), ", ".join(masked)
            )
        log.info("hal code: %s in %s, model at %s", runtime, workdir, upstream)
        argv = docker_args(
            mount,
            rd,
            image=image,
            runtime=runtime,
            tty=os.isatty(0) and os.isatty(1),
            scratch=scratch,
            limits=Limits.from_config(allcfg["limits"]),
            masked=masked,
            agent_args=agent_args,
        )
        status = subprocess.run(argv, timeout=SESSION_MAX_S).returncode
        if iso is not None:
            log.warning(
                "hal code: changes are in isolated session %s; `hal code accept %s` applies them",
                iso.sid,
                iso.sid,
            )
        return status
    finally:
        for server in (forwarder, broker):
            if server is not None:
                server.stop()
        rd.remove()
