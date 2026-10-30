"""Per-project sandbox choices: masking off and consent to backend `none` (REQ-BOX-70).

The file lives only on the host (`state_dir()/sandbox/projects.json`), is keyed by the real path of
the project, holds no approval grants, and can be changed only from a terminal after a typed
confirmation, so a tool call or a box can never grant itself either choice. Lockdown (RESOLUTIONS I2)
still decides whether backend `none` is allowed at all; this file only records the user's consent.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from halkit import paths

from .errors import SandboxError

log = logging.getLogger(__name__)

Choice = Literal["unmask", "none_consent"]
CHOICES: tuple[str, ...] = ("unmask", "none_consent")
CONFIRM_WORD = "yes-i-understand"  # typed, not just "y": these choices remove a protection


class NotATerminal(SandboxError):
    """A per-project choice was attempted without an interactive terminal."""


def projects_file() -> Path:
    """The host file holding the choices."""
    return paths.state_dir() / "sandbox" / "projects.json"


def _key(project: Path) -> str:
    return str(project.resolve())


def _read() -> dict[str, dict[str, bool]]:
    try:
        data = json.loads(projects_file().read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxError(f"{projects_file()} is unreadable ({exc}); fix or delete it") from exc
    if not isinstance(data, dict):
        raise SandboxError(f"{projects_file()} must hold a JSON object")
    return {
        k: {c: bool(v) for c, v in val.items() if c in CHOICES}
        for k, val in data.items()
        if isinstance(val, dict)
    }


def _write(data: dict[str, dict[str, bool]]) -> None:
    path = projects_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def is_set(project: Path, choice: Choice) -> bool:
    """True when the user made `choice` for `project`."""
    return _read().get(_key(project), {}).get(choice, False)


def _tty_confirm(prompt: str) -> str:
    return input(prompt)


def set_choice(
    project: Path,
    choice: Choice,
    *,
    is_tty: Callable[[], bool] = lambda: sys.stdin.isatty() and sys.stdout.isatty(),
    ask: Callable[[str], str] = _tty_confirm,
) -> None:
    """Records `choice` after the user types `CONFIRM_WORD`.

    Raises `NotATerminal` without a terminal and `SandboxError` on a wrong confirmation.
    """
    if choice not in CHOICES:
        raise ValueError(f"unknown choice {choice!r}")
    if not is_tty():
        raise NotATerminal(f"{choice} for {project} can only be set from a terminal")
    answer = ask(f"{choice} removes a protection for {_key(project)}. Type {CONFIRM_WORD} to continue: ")
    if answer.strip() != CONFIRM_WORD:
        raise SandboxError("not confirmed; nothing changed")
    data = _read()
    data.setdefault(_key(project), {})[choice] = True
    _write(data)
    log.warning("sandbox choice %s set for %s", choice, _key(project))


def clear(project: Path, choice: Choice | None = None) -> bool:
    """Revokes one choice, or all of them; True when something was removed."""
    data = _read()
    entry = data.get(_key(project))
    if not entry or (choice and choice not in entry):
        return False
    if choice:
        del entry[choice]
    else:
        entry.clear()
    if not entry:
        del data[_key(project)]
    _write(data)
    return True


def forget(project: Path) -> bool:
    """`hal-sandbox forget DIR`: drops every choice for the project."""
    return clear(project)
