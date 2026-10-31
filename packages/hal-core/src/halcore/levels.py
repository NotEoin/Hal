"""`hal lockdown [LEVEL]` and `hal paranoia [LEVEL]`: show or change the two safety dials (RESOLUTIONS I2, I5).

A change is made by a person on the host, never by a tool call (I2): the command refuses when
`HAL_HARNESS` is set, when stdin is not a terminal, and lowering a level needs the new level typed
back. The edit touches one key and leaves the rest of the file, comments included, as it was;
the result is parsed as TOML before it is written. `lockdown` lives in the config file's
`[sandbox]` table and `paranoia` in the policy file's `[policy]` table, where the readers look.
"""

from __future__ import annotations

import re
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from halhub.surfaces import in_harness
from halkit import paths
from halkit.files import write_atomic
from halsandbox import config as sandbox_config

EXIT_REFUSED = 3  # 06-core-cli exit code table: refused to act
LOCKDOWN_LEVELS = ("off", "relaxed", "standard", "strict")  # weakest first
PARANOIA_LEVELS = ("off", "low", "balanced", "high", "max")  # weakest first
MEANING = {
    "strict": "runsc required, isolated copy only, limits on, hub tools only, paranoia floor high",
    "standard": "runsc when installed (else runc with a warning), isolated copy by default, limits on",
    "relaxed": "also allows --direct and the dependency-prep egress box",
    "off": "allows backend none after per-project consent; red banner every start",
    "low": "classifier asks for high and critical risk; low risk follows policy",
    "balanced": "asks for medium and high, denies critical (default)",
    "high": "asks for medium, denies high and critical",
    "max": "asks even for low risk, denies high and critical",
}


@dataclass(frozen=True)
class Dial:
    """One safety setting: where it is stored and the levels it accepts."""

    name: str
    table: str
    key: str
    levels: tuple[str, ...]
    file: Callable[[], Path]
    current: Callable[[], str]


def _lockdown_now() -> str:
    return str(sandbox_config.load()["lockdown"])


def _paranoia_now() -> str:
    path = paths.policy_file()
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return str(data.get("policy", {}).get("paranoia", "balanced"))


DIALS = {
    "lockdown": Dial("lockdown", "sandbox", "lockdown", LOCKDOWN_LEVELS, paths.config_file, _lockdown_now),
    "paranoia": Dial("paranoia", "policy", "paranoia", PARANOIA_LEVELS, paths.policy_file, _paranoia_now),
}


def set_key(text: str, table: str, key: str, value: str, *, raw: bool = False) -> str:
    """Returns `text` with `[table] key = "value"` set, changing only that line (or adding it).

    `raw` writes `value` unquoted, for booleans and numbers.
    """
    line = f"{key} = {value}" if raw else f'{key} = "{value}"'
    lines = text.splitlines()
    header = re.compile(rf"^\s*\[{re.escape(table)}\]\s*(#.*)?$")
    start = next((i for i, raw in enumerate(lines) if header.match(raw)), None)
    if start is None:
        sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return f"{text}{sep}[{table}]\n{line}\n"
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[", lines[i])), len(lines))
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(start + 1, end):
        if pattern.match(lines[i]):
            comment = re.search(r"\s+#.*$", lines[i])
            lines[i] = line + (comment.group(0) if comment else "")
            break
    else:
        lines.insert(start + 1, line)
    return "\n".join(lines) + "\n"


def change(dial: Dial, level: str, *, confirm: Callable[[str], str]) -> str:
    """Sets `dial` to `level`; returns a one-line result. Raises `PermissionError` or `ValueError`."""
    if level not in dial.levels:
        raise ValueError(f"unknown {dial.name} level {level!r}; use one of {', '.join(dial.levels)}")
    now = dial.current()
    if level == now:
        return f"{dial.name} is already {level}"
    if dial.levels.index(level) < dial.levels.index(now):
        typed = confirm(f"Lowering {dial.name} from {now} to {level} ({MEANING[level]}). Type {level} to confirm: ")
        if typed.strip() != level:
            raise PermissionError("not confirmed; nothing changed")
    path = dial.file()
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    new = set_key(text, dial.table, dial.key, level)
    tomllib.loads(new)  # never write a file the readers cannot parse
    write_atomic(path, new)
    return f"{dial.name}: {now} -> {level} (written to {path})"


def run(
    dial: Dial,
    argv: list[str],
    *,
    stdin: TextIO | None = None,
    out: TextIO | None = None,
    confirm: Callable[[str], str] = input,
) -> int:
    """Shows the dial, or changes it for a person at a terminal; returns the exit status."""
    stdin, out = stdin or sys.stdin, out or sys.stdout
    if not argv:
        now = dial.current()
        print(f"{dial.name}: {now}", file=out)
        for level in reversed(dial.levels):
            print(f"  {'*' if level == now else ' '} {level:9} {MEANING[level]}", file=out)
        return 0
    if in_harness() or not stdin.isatty():
        print(f"hal {dial.name}: refused: only a person at a terminal changes this; edit {dial.file()} yourself", file=sys.stderr)
        return EXIT_REFUSED
    try:
        print(change(dial, argv[0], confirm=confirm), file=out)
    except PermissionError as exc:
        print(f"hal {dial.name}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except ValueError as exc:
        print(f"hal {dial.name}: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str]) -> int:
    """`hal lockdown ...` and `hal paranoia ...` (`argv[0]` names the dial)."""
    return run(DIALS[argv[0]], argv[1:])
