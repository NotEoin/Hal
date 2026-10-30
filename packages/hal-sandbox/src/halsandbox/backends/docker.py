"""The hardened `docker run` argv for one box (02-sandbox "Docker argv", REQ-BOX-08, REQ-BOX-14).

A pure function of a `LaunchSpec`: it runs nothing and reads no host state, so golden files pin
the exact flags. It emits only the flags listed in the spec and never `--privileged`, devices,
extra capabilities or a host network, because nothing in `LaunchSpec` can ask for them. Mounts use
`--mount` with every field CSV-quoted, because `-v` breaks on a `:` in a path.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import SandboxError
from ..gitlayout import GitMount
from ..labels import labels as make_labels
from ..limits import Limits

DEFAULT_ENTRYPOINT = "/opt/hal/bin/hal-box-entry"
RUNTIMES = ("runc", "runsc")
KINDS = ("box", "shell", "code", "chat", "job", "deps")
BOX_HOSTNAME = "hal-box"
NAME_PREFIX = "hal"
CORE_LIMIT = "core=0"  # a core dump would land in /work


@dataclass(frozen=True)
class LaunchSpec:
    """Everything the docker argv depends on; the caller resolves paths and limits first."""

    kind: str
    sid: str
    image: str
    runtime: str
    work: Path | None  # None for a session without a project: /scratch only (RESOLUTIONS I3)
    session_root: Path  # `$R/s/<sid>`: holds run/, etc/, empty, cid
    uid: int
    gid: int
    limits: Limits
    env: Mapping[str, str]
    child: Sequence[str]
    pid: int
    pidstart: int
    started: str
    tty: bool = False
    git_mounts: Sequence[GitMount] = ()
    masked: Sequence[str] = ()
    scratch: Path | None = None
    uv_cache: Path | None = None
    userns_host: bool = False
    entrypoint: str | None = DEFAULT_ENTRYPOINT  # None keeps the image's own entrypoint
    relay_port: int | None = None
    extra_labels: Mapping[str, str] = field(default_factory=dict)
    docker: str = "docker"  # the docker CLI, from detection (`HAL_DOCKER` or `[sandbox].docker`)
    bwrap: str = "bwrap"  # bwrap backend only
    scope: bool = False  # bwrap backend only: wrap in `systemd-run --user --scope` for limits

    @property
    def container_name(self) -> str:
        """The container name: `hal-<kind>-<sid>`."""
        return f"{NAME_PREFIX}-{self.kind}-{self.sid}"


def _mount(src: Path | str, dst: str, *, readonly: bool = False) -> str:
    """One `--mount` value with each field CSV-quoted."""
    buf = io.StringIO()
    csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="").writerow([f"src={src}", f"dst={dst}"])
    return "type=bind," + buf.getvalue() + (",readonly" if readonly else "")


def _check(spec: LaunchSpec) -> None:
    """Raises `SandboxError` for a spec the builder refuses."""
    if spec.kind not in KINDS:
        raise SandboxError(f"unknown box kind {spec.kind!r}")
    if spec.runtime not in RUNTIMES:
        raise SandboxError(f"unknown docker runtime {spec.runtime!r}; use one of {RUNTIMES}")
    if spec.image.startswith("-"):
        raise SandboxError(f"image {spec.image!r} looks like a flag")
    if spec.work is None and (spec.scratch is None or spec.git_mounts or spec.masked):
        raise SandboxError("a session without a work tree needs a scratchpad and has no git mounts or masks")
    for rel in spec.masked:
        if rel.startswith("/") or ".." in rel.split("/"):
            raise SandboxError(f"masked path {rel!r} must be relative and inside the work tree")
    for key in spec.env:
        if "=" in key or not key:
            raise SandboxError(f"bad environment name {key!r}")


def docker_argv(spec: LaunchSpec) -> list[str]:
    """The `docker run` argv for `spec`, in the order the design lists it."""
    _check(spec)
    root, lim = spec.session_root, spec.limits
    labels = make_labels(
        spec.kind,
        spec.sid,
        uid=spec.uid,
        pid=spec.pid,
        start=spec.pidstart,
        extra={"hal.started": spec.started, **spec.extra_labels},
    )
    argv = [spec.docker, "run", "--rm", "-i"]
    if spec.tty:
        argv.append("-t")
    argv += ["--name", spec.container_name, "--cidfile", str(root / "cid")]
    for key, value in labels.items():
        argv += ["--label", f"{key}={value}"]
    argv += ["--pull", "never", "--runtime", spec.runtime, "--network", "none", "--read-only"]
    argv += ["--cap-drop", "ALL", "--security-opt", "no-new-privileges=true"]
    argv += ["--user", f"{spec.uid}:{spec.gid}"]
    if spec.userns_host:
        argv += ["--userns", "host"]
    argv += ["--init", "--ipc", "private"]
    argv += ["--pids-limit", str(lim.pids)]
    argv += ["--memory", str(lim.memory_bytes), "--memory-swap", str(lim.memory_bytes)]
    argv += ["--cpus", f"{lim.cpus:g}", "--oom-score-adj", str(lim.oom_score_adj)]
    argv += ["--ulimit", CORE_LIMIT, "--ulimit", f"nofile={lim.nofile}:{lim.nofile}"]
    argv += ["--stop-timeout", str(lim.stop_timeout_s), "--log-driver", "none", "--hostname", BOX_HOSTNAME]
    argv += ["--tmpfs", f"/tmp:rw,nosuid,nodev,size={lim.tmp_bytes}"]
    home = f"/home/hal:rw,nosuid,nodev,size={lim.home_bytes},uid={spec.uid},gid={spec.gid},mode=0700"
    argv += ["--tmpfs", home]
    if spec.work is not None:
        argv += ["--mount", _mount(spec.work, "/work")]
    for gm in spec.git_mounts:
        argv += ["--mount", _mount(gm.src, gm.dst, readonly=True)]
    argv += ["--mount", _mount(root / "run", "/run/hal", readonly=True)]
    argv += ["--mount", _mount(root / "etc" / "passwd", "/etc/passwd", readonly=True)]
    argv += ["--mount", _mount(root / "etc" / "group", "/etc/group", readonly=True)]
    for rel in spec.masked:
        argv += ["--mount", _mount(root / "empty", f"/work/{rel}", readonly=True)]
    if spec.scratch is not None:
        argv += ["--mount", _mount(spec.scratch, "/scratch")]
    if spec.uv_cache is not None:
        argv += ["--mount", _mount(spec.uv_cache, "/home/hal/.cache/uv", readonly=True)]
    argv += ["--workdir", "/work" if spec.work is not None else "/scratch"]
    for key, value in spec.env.items():
        argv += ["--env", f"{key}={value}"]
    if spec.entrypoint is not None:
        argv += ["--entrypoint", spec.entrypoint]
    argv.append(spec.image)
    if spec.relay_port is not None:
        argv += ["--relay-port", str(spec.relay_port)]
    if spec.entrypoint is not None and spec.child:
        argv.append("--")  # hal-box-entry rejects a child command without it (found by the end-to-end test)
    return argv + list(spec.child)
