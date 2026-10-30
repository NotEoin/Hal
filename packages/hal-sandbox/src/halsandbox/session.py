"""`Session`: one sandboxed session's lifecycle, the facade halcore uses (02-sandbox "Session").

`start` prepares everything (backend, work tree, masks, runtime dir, checkpoint 0, broker,
lifeline, argv) and does not spawn the box: halcore owns the ACP pipes and the model forwarder.
Per RESOLUTIONS I3 a `code` session works on an isolated copy unless `isolated=False` is asked
for and the lockdown level allows it, and a non-coding session gets `/scratch` and no project.
The `approver` is accepted and kept, but approvals are still served by the `halhub mcp` child
the broker spawns, so it is not consulted here yet.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from halkit import paths

from . import config as sbconfig
from . import detect, gitlayout, isolated, projects, scratch
from .backends import Launch
from .backends.bwrap import bwrap_launch, user_manager_running
from .backends.docker import LaunchSpec, docker_argv
from .backends.none import none_launch
from .broker import DEFAULT_COMMAND, Broker, upstream_command
from .checkpoints import Checkpoint, CheckpointConfig, Checkpoints, RestoreResult
from .diffreport import DiffReport
from .env import box_env
from .errors import SandboxError
from .lifeline import Lifeline
from .limits import Limits
from .lockdown import Policy
from .mask import find_masked, resolve_patterns
from .runtime import RuntimeDir, pidstart, utc_now
from .shadow import open_checkpoints
from .upstreams import parse_upstreams

log = logging.getLogger(__name__)

KINDS = ("code", "chat", "job")  # `box`, `shell` and `deps` belong to `hal box` (steps 28 and 32)
DEFAULT_CHILD = ("hal-agent", "--acp", "--config", "/run/hal/agent.json")
DEFAULT_IMAGE = "hal-agent:local"
ASSISTANT_DIR = "assistant"  # `$STATE/assistant/`: the scratchpad key of a session without a project
WORK_KINDS = ("code", "job")  # kinds that may mount a project; the others get /scratch only
DOCKER_RM_TIMEOUT_S = 30.0
RUN_FILES_MODE = 0o644
RunFiles = Mapping[str, bytes] | Callable[[str], Mapping[str, bytes]]
ArgvBuilder = Callable[[LaunchSpec], Launch]


def _docker_builder(spec: LaunchSpec) -> Launch:
    return Launch(docker_argv(spec))


ARGV_BUILDERS: dict[str, ArgvBuilder] = {
    "docker": _docker_builder,
    "none": none_launch,
    "bwrap": bwrap_launch,
}


def _write_files(root: Path, files: Mapping[str, bytes], forbidden: Sequence[bytes]) -> None:
    """Writes `files` below `root`; rejects absolute paths, `..`, symlinks and the session token."""
    for name, data in files.items():
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise SandboxError(f"run file {name!r} must be a relative path without '..'")
        if any(secret and secret in data for secret in forbidden):
            raise SandboxError(f"run file {name!r} contains a session secret; secrets never enter the box")
        dest = root.joinpath(*rel.parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink() or any(p.is_symlink() for p in dest.parents if root in p.parents):
            raise SandboxError(f"run file {name!r} would pass through a symlink")
        dest.write_bytes(data)
        os.chmod(dest, RUN_FILES_MODE)


@dataclass
class Session:
    """One sandboxed session: runtime dir, broker, lifeline, checkpoints, optional isolated tree."""

    id: str
    kind: str
    backend: str
    runtime: str | None
    project: Path | None
    work: Path | None  # None for chat and job sessions without a project: /scratch only
    model_socket: Path
    broker_socket: Path
    broker: Broker
    masked: tuple[str, ...]
    agent_argv: list[str]
    iso: isolated.Isolated | None
    checkpoints: Checkpoints | None
    rd: RuntimeDir
    lifeline: Lifeline
    container: str
    stop_timeout_s: float
    approver: object
    channel: str
    docker: str = "docker"
    launch: Launch | None = None
    scoped: bool = False
    closed: bool = False

    @classmethod
    def start(
        cls,
        project: Path | None = None,
        *,
        kind: str = "code",
        backend: str = "auto",
        runtime: str | None = None,
        isolated_copy: bool | None = None,
        image: str | None = None,
        approver: object = None,
        channel: str = "cli",
        policy_profile: str | None = None,
        checkpoints: bool = True,
        mask: bool | None = None,
        interactive: bool = False,
        run_files: RunFiles | None = None,
        home_seed: Mapping[str, bytes] | None = None,
        child_argv: Sequence[str] = DEFAULT_CHILD,
        tty: bool = False,
        extra_env: Mapping[str, str] | None = None,
        forbidden_secrets: Sequence[str] = (),
        mcp_servers: Mapping[str, Mapping[str, object]] | None = None,
        mcp_restart_limit: int = 1,
    ) -> Session:
        """Prepares a session and returns it; raises `SandboxError` (with the fix) when it cannot.

        `isolated_copy=None` means the default: an isolated copy for `code` and `job`, no project for the
        other kinds. `False` mounts the real project and needs lockdown `relaxed` or lower.
        """
        if kind not in KINDS:
            raise SandboxError(f"unknown session kind {kind!r}; use one of {KINDS}")
        allcfg = sbconfig.load_all()
        cfg = allcfg["sandbox"]
        policy = Policy.for_level(str(cfg["lockdown"]))
        if kind == "code" and project is None:
            raise SandboxError("a code session needs a project directory")
        use_copy = (kind in WORK_KINDS) if isolated_copy is None else isolated_copy
        if kind in WORK_KINDS and not use_copy:
            policy.check_direct()
        probe = detect.detect(
            {**cfg, "runtime": runtime or cfg["runtime"]},
            policy,
            backend=None if backend == "auto" else backend,
        )
        if probe.backend == "none":
            if project is not None:
                projects.require_none_consent(project, is_tty=lambda: interactive)
        project = project.resolve() if project else None
        docker_path = detect.docker_binary(cfg) or "docker"
        iso = None
        work: Path | None
        if kind == "chat" or (kind == "job" and project is None):
            work = None  # RESOLUTIONS I3: non-coding sessions get /scratch only, no /work
        else:
            assert project is not None
            iso = isolated.create(project, int(allcfg["isolated"]["max_copy_bytes"])) if use_copy else None
            work = iso.work if iso else project
        if kind == "chat" and project is not None:
            raise SandboxError("a chat session has no project directory: it gets /scratch only")
        key_dir = project or paths.ensure_private_dir(paths.state_dir() / ASSISTANT_DIR)
        masked: tuple[str, ...] = ()
        try:
            do_mask = (not projects.is_set(key_dir, "unmask")) if mask is None else mask
            if do_mask:
                masked = find_masked(work, resolve_patterns(cfg["mask"])) if work is not None else ()
            rd = RuntimeDir.create()
        except BaseException:
            if iso is not None:
                isolated.reject(iso)
            raise
        broker = lifeline = None
        try:
            files = run_files(rd.sid) if callable(run_files) else (run_files or {})
            _write_files(rd.run, files, [s.encode() for s in forbidden_secrets])
            if home_seed:
                _write_files(rd.run / "home-seed", home_seed, [s.encode() for s in forbidden_secrets])
            ckpt = None
            if checkpoints and work is not None:
                ckpt = open_checkpoints(work, rd.sid, CheckpointConfig(mask=resolve_patterns(cfg["mask"])))
                ckpt.take("session start")
            command = list(DEFAULT_COMMAND) + (["--profile", policy_profile] if policy_profile else [])
            hosts = parse_upstreams(mcp_servers or {}, restart_limit=mcp_restart_limit)
            if hosts and policy.hub_only_upstreams:
                log.warning(
                    "lockdown strict: host mcp servers not offered: %s", ", ".join(h.name for h in hosts)
                )
            elif hosts:
                ups = rd.root / "upstreams.json"  # outside run/, which is what the box sees
                ups.write_text(
                    json.dumps({"servers": dict(mcp_servers or {}), "restart_limit": mcp_restart_limit})
                )
                os.chmod(ups, 0o600)
                command = upstream_command(ups, command)
            broker = Broker(
                rd.socket("broker.sock"),
                command,
                allowed_uid=probe.peer_uid,
                paranoia_floor=policy.paranoia_floor,
            )
            lifeline = Lifeline(rd.socket("lifeline.sock"), rd.sid)
            limits = Limits.from_config(allcfg["limits"])
            session = cls(
                id=rd.sid, kind=kind, backend=probe.backend, runtime=probe.runtime, project=project,
                work=work, model_socket=rd.socket("model.sock"), broker_socket=rd.socket("broker.sock"),
                broker=broker, masked=masked, iso=iso, checkpoints=ckpt, rd=rd, lifeline=lifeline,
                container=f"hal-{kind}-{rd.sid}", stop_timeout_s=float(limits.stop_timeout_s),
                approver=approver, channel=channel, agent_argv=[],
                docker=docker_path,
            )  # fmt: skip
            builder = ARGV_BUILDERS.get(probe.backend)
            if builder is None:
                raise SandboxError(f"backend {probe.backend} cannot launch a {kind} session yet")
            pid = os.getpid()
            mounts = gitlayout.discover(work) if work is not None and not use_copy else ()
            spec = LaunchSpec(
                kind=kind, sid=rd.sid, image=image or DEFAULT_IMAGE, runtime=probe.runtime or "runc",
                work=work, session_root=rd.root, uid=os.getuid(), gid=os.getgid(), limits=limits,
                env=box_env(kind, rd.sid, probe.backend, os.environ, extra=extra_env),
                child=list(child_argv), pid=pid, pidstart=pidstart(pid), started=utc_now(), tty=tty,
                git_mounts=mounts, masked=masked,
                docker=docker_path,
                bwrap=detect.bwrap_binary(cfg) or "bwrap",
                scope=probe.backend == "bwrap" and user_manager_running(),
                scratch=scratch.prepare(key_dir, int(cfg["scratch_max_bytes"])),
            )  # fmt: skip
            session.scoped = spec.scope
            session.launch = builder(spec)
            session.agent_argv = list(session.launch.argv)
            rd.write_owner(
                kind=kind, backend=probe.backend, container=session.container, work=str(work or "")
            )
            rd.write_session_json(session.capabilities(), kind=kind)
            broker.start()
            lifeline.start()
        except BaseException:
            for part in (broker, lifeline):
                if part is not None:
                    part.stop() if isinstance(part, Broker) else part.close()
            rd.remove()
            if iso is not None:
                isolated.reject(iso)
            raise
        log.info("sandbox session %s: %s on %s in %s", rd.sid, kind, probe.backend, work)
        return session

    def capabilities(self) -> dict[str, object]:
        """The `sandbox` object of `session.json`: what this box can and cannot do."""
        none = self.backend == "none"
        return {
            "backend": self.backend if self.runtime is None else f"{self.backend}-{self.runtime}",
            "run": not none,
            "paths": "confined" if none else "box",
            "isolated": self.iso is not None,
            "masked": list(self.masked),
            "work": "/work" if self.work is not None else None,
            "scratch": "/scratch",
            **({"limits": "scope" if self.scoped else "none"} if self.backend == "bwrap" else {}),
        }

    def _ckpt(self) -> Checkpoints:
        if self.checkpoints is None:
            raise SandboxError("this session was started without checkpoints")
        return self.checkpoints

    def checkpoint(self, label: str = "") -> int:
        """Takes a checkpoint (or reuses the last if nothing changed); returns its number."""
        return self._ckpt().take(label or "turn").n

    def _find(self, n: int) -> Checkpoint:
        for ck in self._ckpt().list():
            if ck.n == n:
                return ck
        raise SandboxError(f"no checkpoint {n} in session {self.id}")

    def undo(self, to: int) -> RestoreResult:
        """Restores the work tree to checkpoint `to`, after a safety checkpoint."""
        return self._ckpt().restore(self._find(to))

    def report(self, since: int | None = None) -> DiffReport:
        """The patch, stat and risk flags from checkpoint `since` (default: the first) to now."""
        return self._ckpt().diff(None if since is None else self._find(since))

    def diff(self, since: int | None = None) -> str:
        """The patch text from checkpoint `since` (default: the first) to now."""
        return self.report(since).patch

    def apply_isolated(self, *, allow_flags: bool = False) -> list[Path]:
        """Applies the isolated copy's changes to the real project; returns the paths applied."""
        if self.iso is None:
            raise SandboxError("this session has no isolated copy")
        return [Path(c.path) for c in isolated.accept(self.iso, allow_flags=allow_flags)]

    def reject_isolated(self) -> None:
        """Discards the isolated copy."""
        if self.iso is None:
            raise SandboxError("this session has no isolated copy")
        isolated.reject(self.iso)

    def close(self, timeout: float | None = None) -> None:
        """Tears down in the documented order; safe to repeat. `timeout` is the lifeline grace."""
        if self.closed:
            return
        self.closed = True
        self.lifeline.request_stop(self.stop_timeout_s if timeout is None else timeout)
        if self.backend == "docker":
            try:
                subprocess.run(
                    [self.docker, "rm", "-f", self.container],
                    capture_output=True,
                    timeout=DOCKER_RM_TIMEOUT_S,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("could not remove container %s: %s", self.container, exc)
        self.broker.stop()
        self.lifeline.close()
        self.rd.remove()
        log.info("sandbox session %s closed", self.id)


__all__ = ["ARGV_BUILDERS", "Session"]
