"""Bubblewrap backend: the argv of one box in new namespaces (02-sandbox "bwrap argv", REQ-BOX-21).

A pure function of a `LaunchSpec` and the host layout facts it is given. It shares the host kernel
and has no seccomp filter, so the lockdown policy allows it at `relaxed` at most. Only the paths
listed here exist in the box: no host `/etc` beyond the generated files, no `$HOME`.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from . import Launch
from .docker import LaunchSpec

SCOPE_ARGV = ("systemd-run", "--user", "--scope", "--quiet", "--collect")
USER_MANAGER_STATES = ("running", "degraded")
USER_MANAGER_TIMEOUT_S = 2.0  # REQ-BOX-21
MERGED_USR_LINKS = (("usr/bin", "/bin"), ("usr/lib", "/lib"), ("usr/lib64", "/lib64"), ("usr/sbin", "/sbin"))
ETC_FILES = ("/etc/ld.so.cache", "/etc/alternatives", "/etc/localtime", "/etc/nsswitch.conf")
BOX_ENTRY = "hal-box-entry"


def user_manager_running(systemctl: str = "systemctl") -> bool:
    """True when `systemctl --user is-system-running` answers `running` or `degraded` in time."""
    try:
        out = subprocess.run(
            [systemctl, "--user", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=USER_MANAGER_TIMEOUT_S,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return out in USER_MANAGER_STATES


def _python_prefixes(prefixes: Sequence[str]) -> list[str]:
    """Prefixes that need their own read-only bind: those not already under `/usr`."""
    seen: list[str] = []
    for p in prefixes:
        if p and not p.startswith("/usr") and p not in seen:
            seen.append(p)
    return seen


def bwrap_launch(
    spec: LaunchSpec,
    *,
    merged_usr: bool = True,
    prefixes: Sequence[str] | None = None,
    venv_bin: str | None = None,
) -> Launch:
    """The bwrap (optionally scoped) argv for `spec`; `merged_usr` says `/bin` is a link to `usr/bin`."""
    root, lim = spec.session_root, spec.limits
    venv_bin = venv_bin or str(Path(sys.executable).parent)
    prefixes = prefixes if prefixes is not None else (sys.base_prefix, sys.prefix)
    argv: list[str] = []
    if spec.scope:
        argv += [*SCOPE_ARGV]
        argv += ["-p", f"MemoryMax={lim.memory_bytes}", "-p", "MemorySwapMax=0", "-p", f"TasksMax={lim.pids}"]
        argv += ["-p", f"CPUQuota={int(lim.cpus * 100)}%", "--"]
    argv += [spec.bwrap, "--unshare-all", "--unshare-user", "--disable-userns", "--assert-userns-disabled"]
    argv += ["--die-with-parent", "--new-session", "--cap-drop", "ALL", "--hostname", "hal-box", "--clearenv"]
    env = dict(spec.env)
    env["PATH"] = f"{venv_bin}:/usr/local/bin:/usr/bin:/bin"  # the venv is bound at its host path
    for key, value in env.items():
        argv += ["--setenv", key, value]
    argv += ["--ro-bind", "/usr", "/usr"]
    if merged_usr:
        for target, link in MERGED_USR_LINKS:
            argv += ["--symlink", target, link]
    else:
        for _, link in MERGED_USR_LINKS:
            argv += ["--ro-bind-try", link, link]
    argv += ["--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache"]
    for path in ETC_FILES[1:]:
        argv += ["--ro-bind-try", path, path]
    argv += ["--ro-bind", str(root / "etc" / "passwd"), "/etc/passwd"]
    argv += ["--ro-bind", str(root / "etc" / "group"), "/etc/group"]
    for prefix in _python_prefixes(prefixes):
        argv += ["--ro-bind", prefix, prefix]
    argv += ["--proc", "/proc", "--dev", "/dev"]
    argv += ["--size", str(lim.tmp_bytes), "--tmpfs", "/tmp"]
    argv += ["--size", str(lim.home_bytes), "--perms", "0700", "--tmpfs", "/home/hal"]
    argv += ["--bind", str(spec.work), "/work"]
    for gm in spec.git_mounts:
        argv += ["--ro-bind", str(gm.src), gm.dst]
    argv += ["--ro-bind", str(root / "run"), "/run/hal"]
    for rel in spec.masked:
        argv += ["--ro-bind", str(root / "empty"), f"/work/{rel}"]
    if spec.scratch is not None:
        argv += ["--bind", str(spec.scratch), "/scratch"]
    argv += ["--chdir", "/work", "--", f"{venv_bin}/{BOX_ENTRY}"]
    if spec.relay_port is not None:
        argv += ["--relay-port", str(spec.relay_port)]
    return Launch([*argv, "--", *spec.child])
