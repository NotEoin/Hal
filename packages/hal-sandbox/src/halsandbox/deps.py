"""Dependency preparation in a separate network-restricted box (02-sandbox REQ-BOX-71 to REQ-BOX-75).

A project's lockfile is installed by `uv sync --frozen`, `npm ci` or `cargo fetch --locked` inside a
`deps` box on docker: no broker and no model socket, `--network none`, and one way out, the host
`ConnectProxy` on `run/egress.sock`, which tunnels only to the registry allowlist. Nothing runs
without the caller's approval of that exact lockfile hash, and a lockfile is offered again only
when its hash differs from the last successful prep.

Design decisions:
- `prepare` takes the tree mounted at `/work` (the isolated copy, RESOLUTIONS I3), so a prep never
  writes into the real project; the "done" record is keyed by the project path.
- The approval is a plain callable, not `halhub`'s `Approver`: the caller (halcore) builds the
  `sandbox_deps_prep` request, the policy key and the audit record, and answers allow or deny.
- The "done" record lives in `deps.json` next to `projects.json`, not inside it, so choices written
  by `projects.set_choice` cannot drop it.
- The prep box needs lockdown `relaxed` or lower (RESOLUTIONS I2, `Policy.allow_egress_prep`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from halkit import paths
from halkit.files import write_atomic

from . import config as sbconfig
from . import detect
from .backends.docker import LaunchSpec, docker_argv
from .code import DEFAULT_IMAGE
from .egress import ConnectProxy
from .env import box_env
from .errors import SandboxError
from .lifeline import Lifeline
from .limits import Limits
from .lockdown import Policy
from .runtime import RuntimeDir, pidstart, utc_now
from .shadow import open_checkpoints

log = logging.getLogger(__name__)

Manager = Literal["uv", "npm", "cargo"]
LOCKFILES: dict[str, tuple[Manager, tuple[str, ...]]] = {
    "uv.lock": ("uv", ("uv", "sync", "--frozen")),
    "package-lock.json": ("npm", ("npm", "ci", "--no-audit", "--no-fund")),
    "Cargo.lock": ("cargo", ("cargo", "fetch", "--locked")),
}
PROXY_PORT = 3128  # the in-box loopback port that `hal-relay` listens on
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
EGRESS_SOCKET = "/run/hal/egress.sock"
LOG_TAIL_CHARS = 4000
KILL_TIMEOUT_S = 30.0
SOCKET_NAMES = ("HAL_BROKER_SOCKET", "HAL_MODEL_SOCKET")  # the prep box has neither socket (RESOLUTIONS F3)
OFFLINE_NAMES = ("UV_OFFLINE", "NPM_CONFIG_OFFLINE", "CARGO_NET_OFFLINE")  # only the prep box drops these
PREP_ENV = {
    "HTTPS_PROXY": PROXY_URL,
    "https_proxy": PROXY_URL,
    "NPM_CONFIG_HTTPS_PROXY": PROXY_URL,
    "CARGO_HTTP_PROXY": PROXY_URL,
    "NO_PROXY": "",
    "UV_CACHE_DIR": "/work/.hal/cache/uv",
    "UV_LINK_MODE": "copy",
    "NPM_CONFIG_CACHE": "/work/.hal/cache/npm",
}  # fmt: skip

Approve = Callable[["Lockfile"], bool]


@dataclass(frozen=True)
class Lockfile:
    """A lockfile in the work root; `sha256` is `sha256:<hex>`."""

    manager: Manager
    path: str
    sha256: str


@dataclass(frozen=True)
class PrepResult:
    """The outcome of one prep; `denied_hosts` are the registries the proxy refused."""

    lockfile: Lockfile
    ok: bool
    exit_code: int | None
    log_tail: str
    denied_hosts: tuple[str, ...]


def detect_lockfiles(work: Path) -> list[Lockfile]:
    """The lockfiles directly in `work`, in a fixed order; symlinks are ignored."""
    found = []
    for name, (manager, _) in LOCKFILES.items():
        path = work / name
        if path.is_file() and not path.is_symlink():
            found.append(Lockfile(manager, name, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()))
    return found


def _done_file() -> Path:
    return paths.state_dir() / "sandbox" / "deps.json"


def _read_done() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(_done_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # a missing or forged record only causes another offer (REQ-BOX-74)
    return data if isinstance(data, dict) else {}


def pending(work: Path, project: Path | None = None) -> list[Lockfile]:
    """Lockfiles whose hash differs from the last successful prep of `project` (default `work`)."""
    done = _read_done().get(str((project or work).resolve()), {})
    return [lf for lf in detect_lockfiles(work) if done.get(lf.path) != lf.sha256]


def _record(project: Path, lockfile: Lockfile) -> None:
    data = _read_done()
    data.setdefault(str(project.resolve()), {})[lockfile.path] = lockfile.sha256
    path = _done_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_atomic(path, json.dumps(data, indent=2, sort_keys=True))


def prep_env(sid: str, backend: str = "docker") -> dict[str, str]:
    """The prep box environment: the box env without the offline switches and socket names, plus the proxy."""
    env = box_env("deps", sid, backend, os.environ)
    for name in (*OFFLINE_NAMES, *SOCKET_NAMES):
        env.pop(name, None)
    env.update(PREP_ENV)
    return env


def command_for(lockfile: Lockfile) -> tuple[str, ...]:
    """The install command for a lockfile's manager."""
    return LOCKFILES[lockfile.path][1]


def _prepare_work_dir(work: Path) -> None:
    hal = work / ".hal"
    hal.mkdir(mode=0o777, exist_ok=True)  # the box user is the caller's uid; kept open for rootless docker
    gitignore = hal / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")


def prepare(
    work: Path,
    *,
    approve: Approve,
    project: Path | None = None,
    lockfiles: Sequence[Lockfile] | None = None,
    timeout_s: float | None = None,
    image: str = DEFAULT_IMAGE,
    runtime: str | None = None,
) -> list[PrepResult]:
    """Installs the dependencies of each lockfile in `work`, after `approve(lockfile)` allows it.

    A denied lockfile runs nothing and yields no result. On exit 0 the lockfile hash is recorded for
    `project`. Raises `SandboxError` when the backend is not docker or lockdown forbids the prep box.
    """
    work = work.resolve()
    project = project or work
    allcfg = sbconfig.load_all()
    cfg = allcfg["sandbox"]
    policy = Policy.for_level(str(cfg["lockdown"]))
    if not policy.allow_egress_prep:
        raise SandboxError(
            f"dependency prep needs lockdown relaxed or lower (now {cfg['lockdown']}): "
            "set [sandbox].lockdown on the host"
        )
    probe = detect.detect(
        {**cfg, "runtime": runtime or cfg["runtime"], "image": image}, policy, backend="docker"
    )
    if probe.backend != "docker":
        raise SandboxError("dependency prep needs the docker backend")
    wanted = list(lockfiles) if lockfiles is not None else pending(work, project)
    timeout = float(timeout_s if timeout_s is not None else allcfg["deps"]["timeout_s"])
    results = []
    for lockfile in wanted:
        if not approve(lockfile):
            log.info("dependency prep of %s denied", lockfile.path)
            continue
        results.append(_run_prep(work, project, lockfile, allcfg, probe, timeout, image))
    return results


def _run_prep(work, project, lockfile, allcfg, probe, timeout, image) -> PrepResult:  # noqa: ANN001 - private
    """Checkpoints `work`, runs one prep box and records the hash on success."""
    cfg = allcfg["sandbox"]
    _prepare_work_dir(work)
    try:
        open_checkpoints(work, "deps").take(f"before deps prep {lockfile.path}")
    except SandboxError as exc:
        log.warning("no checkpoint before dependency prep: %s", exc)
    rd = RuntimeDir.create()
    docker = detect.docker_binary(cfg) or "docker"
    proxy = ConnectProxy(
        rd.socket("egress.sock"),
        registries=allcfg["deps"]["registries"],
        on_event=lambda event: log.info("egress %s", event),
    )
    code = None
    text = ""
    try:
        lifeline = Lifeline(rd.socket("lifeline.sock"), rd.sid)
        proxy.start()
        lifeline.start()
        pid = os.getpid()
        spec = LaunchSpec(
            kind="deps", sid=rd.sid, image=image, runtime=probe.runtime or "runc", work=work,
            session_root=rd.root, uid=os.getuid(), gid=os.getgid(),
            limits=Limits.from_config(allcfg["limits"]),
            env=prep_env(rd.sid), child=command_for(lockfile), pid=pid, pidstart=pidstart(pid),
            started=utc_now(), relay_port=PROXY_PORT, relay_socket=EGRESS_SOCKET, docker=docker,
        )  # fmt: skip
        argv = docker_argv(spec)
        try:
            done = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
            code, text = done.returncode, (done.stdout + done.stderr).decode("utf-8", "replace")
        except subprocess.TimeoutExpired as exc:
            subprocess.run(
                [docker, "kill", spec.container_name],
                capture_output=True,
                timeout=KILL_TIMEOUT_S,
                check=False,
            )
            text = f"timed out after {timeout:.0f} s\n" + ((exc.stdout or b"") + (exc.stderr or b"")).decode(
                "utf-8", "replace"
            )
        lifeline.close()
    finally:
        proxy.close()
        rd.remove()
    ok = code == 0
    if ok:
        _record(project, lockfile)
    return PrepResult(lockfile, ok, code, text[-LOG_TAIL_CHARS:], tuple(sorted(set(proxy.denied))))
