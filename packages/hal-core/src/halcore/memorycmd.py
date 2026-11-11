"""`hal ask`, `hal index`, `hal brief` and the other vault commands: hand-off to the memory pack.

The vault lives in the hal-memory pack, so these commands run its `hal-memory` program with the
same arguments, as the removed bash launcher did. The program is looked up on PATH, then in the
shared packs environment `hal packs add` installs into. Without it the command says how to add
the pack, with exit status 3 (a prerequisite is missing).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import packs

PROGRAM = "hal-memory"
VAULT_COMMANDS = ("ask", "index", "brief", "summarize", "import", "obsidian-key", "register-repos")
EXIT_MISSING = 3


def find_program() -> Path | None:
    """`hal-memory` on PATH, else in the shared packs environment; None when the pack is not installed."""
    found = shutil.which(PROGRAM)
    if found:
        return Path(found)
    candidate = packs.env_dir("shared", "memory") / "bin" / PROGRAM
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def main(argv: list[str]) -> int:
    """Replaces this process with `hal-memory ARGV`; returns 3 when the memory pack is missing."""
    program = find_program()
    if program is None:
        command = argv[0] if argv else "ask"
        print(f"hal {command}: the vault commands come from the memory pack; add it with: hal packs add memory",
              file=sys.stderr)  # fmt: skip
        return EXIT_MISSING
    os.execv(program, [str(program), *argv])  # noqa: S606 - the pack's own program takes over, as `exec` did
    return 0  # not reached: execv does not return
