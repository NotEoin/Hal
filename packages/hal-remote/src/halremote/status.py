"""Reads and renders the supervisor's `status.json` for `hal remote status` (REQ-RMT-30).

The file is written by `supervisor.Supervisor`; this module only reads it. A status file whose
supervisor process is gone is reported as `stopped`, so a crash never looks like a running tunnel.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .model import ProfilePaths
from .supervisor import STATUS_FILE, STATUS_SCHEMA

EXIT_HEALTHY = 0
EXIT_NOT_UP = 4  # design 07, CLI synopsis: not running or failing


class StatusError(ValueError):
    """The status file exists but cannot be understood."""


def pid_alive(pid: int) -> bool:
    """True when a process with `pid` exists (signal 0 checks without delivering)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, we may not signal it
    except OSError:
        return False
    return True


def read(paths: ProfilePaths) -> dict[str, Any] | None:
    """The status dict, `None` when the tunnel never ran; a dead supervisor reads as `stopped`."""
    path = paths.state / STATUS_FILE
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise StatusError(f"{path}: {exc}") from None
    if not isinstance(data, dict) or data.get("schema") != STATUS_SCHEMA:
        raise StatusError(f"{path}: unexpected schema")
    if data.get("state") in ("starting", "connecting", "up", "backoff") and not pid_alive(
        int(data.get("supervisor_pid") or 0)
    ):
        data = {
            **data,
            "state": "stopped",
            "ssh_pid": None,
            "last_error": data.get("last_error") or "supervisor exited",
        }
    return data


def render(status: dict[str, Any] | None, profile: str, host: str) -> str:
    """One human line (two when something needs fixing)."""
    if status is None:
        return f"{profile}  not running   (hal remote up)"
    gw = status.get("gateway") or {}
    health = (
        "gateway ok" if gw.get("ok") else ("gateway not checked" if not gw else f"gateway {gw.get('status')}")
    )
    if gw.get("detail") and gw.get("ok"):
        health += f" ({gw['detail']})"
    since = str(status.get("since", ""))[11:16]
    fwd = ", ".join(
        f"{f['local']} -> {host}:{f['remote'].rsplit(':', 1)[1]}" for f in status.get("forwards", [])
    )
    line = f"{profile}  {status['state']} since {since}Z ({status.get('mode')})   {health}   {fwd}"
    if status.get("last_error") and status["state"] != "up":
        line += f"\n  last error: {status['last_error']}"
    if status.get("fix"):
        line += f"\n  fix: {status['fix']}"
    return line


def exit_code(status: dict[str, Any] | None) -> int:
    """0 when `up` and the gateway answered 200, 4 otherwise."""
    if status is None or status.get("state") != "up":
        return EXIT_NOT_UP
    return EXIT_HEALTHY if (status.get("gateway") or {}).get("ok") else EXIT_NOT_UP
