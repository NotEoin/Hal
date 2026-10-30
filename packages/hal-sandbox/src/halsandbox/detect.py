"""Backend and runtime detection under a lockdown level (REQ-BOX-01 to 06, RESOLUTIONS I2).

`detect` answers "which backend and runtime will this session use, and why". Decisions:

- Every refusal is a `SandboxError` naming the cause and the fix, never a silent downgrade: an
  explicit backend or runtime that is not usable is an error (REQ-BOX-04), and a stopped docker
  daemon refuses when docker was used before (REQ-BOX-02, D26).
- The lockdown `Policy` gates the result: `bwrap` needs `relaxed`, `none` needs `off`, `strict`
  needs a passing `runsc` probe. A lower level never selects itself; the user sets it.
- Docker facts and the runsc probe are cached for 24 h in `state/sandbox/probe.json`, keyed by the
  docker version, runtimes and bwrap version, because the answer changes only when the host does.
- The runsc probe is injectable: it needs a live daemon and an image, so tests pass a fake.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from halkit import files, paths, proc, timeutil

from .errors import LockdownError, SandboxError
from .lockdown import Policy

log = logging.getLogger(__name__)

DOCKER_ANSWER_S = 5.0  # REQ-BOX-02
BWRAP_PROBE_S = 2.0  # REQ-BOX-02
RUNSC_PROBE_S = 10.0
PROBE_TTL_S = 24 * 3600.0  # REQ-BOX-06
PROBE_IMAGE = "hal-agent:local"
BACKENDS = ("docker", "bwrap", "none")
RUNSC_FIX = (
    'register runsc with "runtimeArgs": ["--host-uds=open"] in /etc/docker/daemon.json '
    "(docs/howto/install-gvisor.md)"
)
BWRAP_PROBE_ARGV = (
    *("--unshare-all", "--unshare-user", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin"),
    *("--proc", "/proc", "--dev", "/dev", "/usr/bin/true"),
)


@dataclass(frozen=True)
class Probe:
    """What detection found for the chosen backend; `detail` holds the cause and fix when not ok."""

    backend: str
    ok: bool
    runtime: str | None
    daemon_mode: str | None
    detail: str
    checked: str  # UTC Z
    peer_uid: int | None = None  # the uid the runsc probe saw on the broker socket


RunscProbe = Callable[[str, str], tuple[bool, str, int | None]]  # (docker, image) -> (ok, detail, uid)


def _sandbox_dir() -> Path:
    return paths.ensure_private_dir(paths.state_dir() / "sandbox")


def docker_binary(cfg: Mapping[str, Any]) -> str | None:
    """The docker CLI named by `HAL_DOCKER` or `[sandbox].docker`, when it exists."""
    return shutil.which(os.environ.get("HAL_DOCKER") or str(cfg.get("docker", "docker")))


def bwrap_binary(cfg: Mapping[str, Any]) -> str | None:
    """The bwrap binary named by `HAL_BWRAP` or `[sandbox].bwrap`, when it exists."""
    return shutil.which(os.environ.get("HAL_BWRAP") or str(cfg.get("bwrap", "bwrap")))


def _run(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        done = proc.run(argv, timeout=timeout, check=False)
    except (proc.ProcTimeout, OSError) as exc:
        return -1, str(exc)
    return done.returncode, done.stdout.decode(errors="replace").strip() or done.stderr.decode(
        errors="replace"
    )


def default_runsc_probe(docker: str, image: str) -> tuple[bool, str, int | None]:
    """Runs a hardened runsc container that connects to a bind-mounted host socket (REQ-BOX-03)."""
    base = paths.ensure_private_dir(paths.runtime_dir())
    tmp = Path(tempfile.mkdtemp(prefix="probe-", dir=base))
    try:
        tmp.chmod(0o700)
        sock_path = paths.check_socket_path(tmp / "probe.sock")
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(sock_path))
        server.listen(1)
        server.settimeout(RUNSC_PROBE_S)
        code = "import socket;s=socket.socket(1);s.connect('/run/hal/probe.sock');s.sendall(b'ok')"
        argv = [docker, "run", "--rm", "--runtime", "runsc", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{tmp}:/run/hal:ro",
                "--entrypoint", "python3", image, "-c", code]  # fmt: skip
        child = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            conn, _ = server.accept()
        except (TimeoutError, OSError):
            child.kill()
            err = child.communicate()[1].decode(errors="replace").strip()
            return False, f"runsc container could not reach the host socket ({err[:200]}); {RUNSC_FIX}", None
        _pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        conn.close()
        child.wait(timeout=RUNSC_PROBE_S)
        return True, "runsc connected to the host socket", uid
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"runsc probe failed: {exc}; {RUNSC_FIX}", None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _docker_facts(docker: str) -> tuple[str, set[str], str]:
    """`(server version, runtimes, daemon mode)`; raises `SandboxError` when docker does not answer."""
    code, out = _run([docker, "version", "--format", "{{.Server.Version}}"], DOCKER_ANSWER_S)
    if code != 0:
        raise SandboxError(
            f"docker does not answer: {out[:200]}. Start the daemon, add yourself to the docker "
            "group, or pass --backend bwrap or --backend none for this run"
        )
    code, info = _run([docker, "info", "--format", "{{json .}}"], DOCKER_ANSWER_S)
    if code != 0:
        raise SandboxError(f"docker info failed: {info[:200]}")
    try:
        data = json.loads(info)
    except ValueError as exc:
        raise SandboxError(f"docker info gave unreadable output: {exc}") from exc
    options = " ".join(data.get("SecurityOptions") or [])
    mode = (
        "rootless"
        if "name=rootless" in options
        else "userns-remap"
        if "name=userns" in options
        else "rootful"
    )
    return out, set(data.get("Runtimes") or {}), mode


def _cache_path() -> Path:
    return _sandbox_dir() / "probe.json"


def _cache_read(key: str, now: float) -> Probe | None:
    try:
        data = json.loads(_cache_path().read_text())
        if data["key"] != key or now - float(data["at"]) > PROBE_TTL_S:
            return None
        return Probe(**data["probe"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _cache_write(key: str, probe: Probe, now: float) -> None:
    files.write_atomic(_cache_path(), json.dumps({"key": key, "at": now, "probe": asdict(probe)}))


def _probe_image(cfg: Mapping[str, Any]) -> str:
    """The image the runsc probe runs: the configured one, or the default when it is "auto"."""
    image = str(cfg.get("image") or "auto")
    return PROBE_IMAGE if image == "auto" else image


def _probe_docker(
    cfg: Mapping[str, Any], docker: str, refresh: bool, runsc_probe: RunscProbe, now: float
) -> Probe:
    version, runtimes, mode = _docker_facts(docker)
    want = str(cfg.get("runtime", "auto"))
    key = f"{version}|{','.join(sorted(runtimes))}|{want}"
    if not refresh and (cached := _cache_read(key, now)) is not None:
        return cached
    if want == "runc":
        runtime, ok, detail, uid = "runc", True, "runc requested", None
    elif want == "runsc" and "runsc" not in runtimes:
        runtime, ok, uid = "runsc", False, None
        detail = "runtime runsc is not registered with docker; see docs/howto/install-gvisor.md"
    elif "runsc" in runtimes:
        passed, why, uid = runsc_probe(docker, _probe_image(cfg))
        if passed:
            runtime, ok, detail = "runsc", True, why
        elif want == "runsc":
            runtime, ok, detail = "runsc", False, why
        else:
            runtime, ok, detail, uid = "runc", True, f"runsc probe failed, using runc: {why}", None
    else:
        runtime, ok, detail, uid = (
            "runc",
            True,
            "runsc is not installed: the box shares the host kernel",
            None,
        )
    probe = Probe("docker", ok, runtime, mode, detail, timeutil.iso_z(), uid)
    _cache_write(key, probe, now)
    return probe


def _probe_bwrap(cfg: Mapping[str, Any]) -> Probe:
    checked = timeutil.iso_z()
    binary = bwrap_binary(cfg)
    if binary is None:
        return Probe("bwrap", False, None, None, "bwrap is not installed (apt install bubblewrap)", checked)
    code, out = _run([binary, *BWRAP_PROBE_ARGV], BWRAP_PROBE_S)
    if code != 0:
        return Probe("bwrap", False, None, None, f"bwrap probe failed: {out[:200]}", checked)
    return Probe("bwrap", True, None, None, "bwrap probe passed", checked)


def _last_backend_file() -> Path:
    return _sandbox_dir() / "last-backend"


def _gate(policy: Policy, probe: Probe) -> None:
    """Raises `LockdownError` when the level forbids what was found."""
    if probe.backend == "bwrap":
        if not policy.allow_bwrap:
            raise LockdownError(
                f"backend bwrap shares the host kernel: needs lockdown relaxed or lower (now {policy.level})"
            )
    elif probe.backend == "none":
        if not policy.allow_backend_none:
            raise LockdownError(
                f"backend none needs lockdown off (now {policy.level}) and a per-project consent"
            )
    elif policy.require_runsc and probe.runtime != "runsc":
        raise LockdownError(
            f"lockdown strict needs the runsc runtime ({probe.detail}); fix runsc or lower the lockdown level"
        )


def detect(
    cfg: Mapping[str, Any],
    policy: Policy,
    *,
    backend: str | None = None,
    refresh: bool = False,
    runsc_probe: RunscProbe = default_runsc_probe,
    now: float | None = None,
) -> Probe:
    """Chooses the backend and runtime for a session; raises `SandboxError` with the fix when none fits.

    `backend` is the per-run flag and beats `[sandbox].backend`; an explicit choice never falls
    back to another one and leaves `last-backend` alone.
    """
    now = time.time() if now is None else now
    name = backend or str(cfg.get("backend", "auto"))
    if name not in (*BACKENDS, "auto"):
        raise SandboxError(f"unknown backend {name!r}; use auto, docker, bwrap or none")
    explicit = name != "auto"
    if name in ("docker", "auto"):
        docker = docker_binary(cfg)
        if docker is None and explicit:
            raise SandboxError("docker is not installed; install it or pick another backend")
        if docker is not None:
            try:
                probe = _probe_docker(cfg, docker, refresh, runsc_probe, now)
            except SandboxError:
                if explicit:
                    raise
                if _last_backend_file().is_file() and _last_backend_file().read_text().strip() == "docker":
                    raise SandboxError(
                        "docker answered before and does not now: start the daemon, or pass "
                        "--backend bwrap or --backend none for this run"
                    ) from None
                probe = None
            if probe is not None:
                if not probe.ok:
                    raise SandboxError(probe.detail)
                _gate(policy, probe)
                return _chosen(probe, explicit)
    if name in ("bwrap", "auto"):
        probe = _probe_bwrap(cfg)
        if probe.ok:
            _gate(policy, probe)
            return _chosen(probe, explicit)
        if explicit:
            raise SandboxError(probe.detail)
    if cfg.get("require"):
        raise SandboxError("no isolating backend is usable and [sandbox].require is set; install docker")
    probe = Probe("none", True, None, None, "no isolation", timeutil.iso_z())
    _gate(policy, probe)
    return _chosen(probe, explicit)


def _chosen(probe: Probe, explicit: bool) -> Probe:
    if not explicit:
        files.write_atomic(_last_backend_file(), probe.backend)
    log.info("sandbox backend %s runtime %s", probe.backend, probe.runtime)
    return probe
