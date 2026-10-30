"""Consent for an unboxed assistant channel (REQ-CLI-96).

Only backend `none` (lockdown `off`) can run an assistant channel without a box. The first
unboxed start per channel needs a typed `y` on a terminal; the answer is kept in
`state_dir()/unboxed-ok.json` so later starts of that channel do not ask again. A channel with
no terminal and no record is refused: nobody could have agreed to it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from halkit import timeutil
from halsandbox.errors import SandboxError

from . import config

CONSENT_FILE = "unboxed-ok.json"
BANNER = "UNSANDBOXED: this agent runs on the host with no box (lockdown off)"


def _file() -> Path:
    return config.state_dir() / CONSENT_FILE


def _load() -> dict[str, str]:
    try:
        data = json.loads(_file().read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SandboxError(f"{_file()} is unreadable ({exc}); fix or delete it") from exc
    if not isinstance(data, dict):
        raise SandboxError(f"{_file()} must hold a JSON object")
    return {str(k): str(v) for k, v in data.items()}


def has_consent(channel: str) -> bool:
    """True when the user already confirmed an unboxed start for `channel`."""
    return channel in _load()


def require_consent(
    channel: str,
    *,
    interactive: bool | None = None,
    ask: Callable[[str], str] = input,
    now: str | None = None,
) -> None:
    """Returns when `channel` may run unboxed; raises `SandboxError` (exit 3) otherwise."""
    if has_consent(channel):
        return
    if interactive is None:
        interactive = os.isatty(0) and os.isatty(1)
    if not interactive:
        raise SandboxError(
            f"channel {channel!r} would run unboxed and has no consent in {_file()}; "
            "start `hal chat` once on a terminal and type y, or use a boxed backend"
        )
    answer = ask(f"{BANNER}\nRun the {channel} channel unboxed? Type y to confirm: ")
    if answer.strip() != "y":
        raise SandboxError(f"unboxed start of channel {channel!r} was not confirmed")
    records = _load()
    records[channel] = now or timeutil.iso_z()
    path = _file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=2, sort_keys=True))
    tmp.replace(path)
