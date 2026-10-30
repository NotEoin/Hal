"""Backend `none`: hal-agent as a host process with confined capabilities (02-sandbox "Backend none").

There is no isolation here, only a smaller agent: `session.json` says `run: false` and
`paths: "confined"`, so the agent offers no `run` tool and opens files through `fsafe.open_beneath`
below `HAL_WORKSPACE`, refusing the masked paths. The caller must have the per-project consent
(`projects.require_none_consent`) before it gets here. Box paths in the child argv are rewritten to
the session's host paths, because nothing is mounted.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import Launch
from .docker import LaunchSpec

BOX_RUN = "/run/hal"
SANDBOX_NAME = "none"
HOST_BIN_PATH = "/usr/local/bin:/usr/bin:/bin"


def _host_path(arg: str, run: Path, work: Path) -> str:
    """Maps a box path in a child argument to the host path; other arguments are unchanged."""
    if arg.startswith(BOX_RUN + "/"):
        return str(run / arg[len(BOX_RUN) + 1 :])
    return str(work) if arg == "/work" else arg


def none_launch(spec: LaunchSpec, host_env: dict[str, str] | None = None) -> Launch:
    """The host argv, environment and cwd for a `none` session."""
    run = spec.session_root / "run"
    host = dict(host_env if host_env is not None else os.environ)
    home = spec.session_root / "home"
    tmp = spec.session_root / "tmp"
    for d in (home, tmp):
        d.mkdir(mode=0o700, exist_ok=True)
    env = {
        k: v for k, v in spec.env.items() if k.startswith(("HAL_", "LC_", "LANG")) or k in ("NO_COLOR", "TZ")
    }
    venv_bin = str(Path(sys.executable).parent)
    env.update(
        {
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "PATH": f"{venv_bin}:{host.get('PATH', HOST_BIN_PATH)}",
            "HAL_SANDBOX": SANDBOX_NAME,
            "HAL_WORKSPACE": str(spec.work),
            "HAL_BROKER_SOCKET": str(run / "broker.sock"),
            "HAL_MODEL_SOCKET": str(run / "model.sock"),
        }
    )
    if spec.scratch is not None:
        env["HAL_SCRATCH"] = str(spec.scratch)
    argv = [_host_path(a, run, spec.work) for a in spec.child]
    exe = Path(venv_bin) / argv[0]
    if exe.is_file():
        argv[0] = str(exe)
    return Launch(argv, env, str(spec.work), new_session=True)
