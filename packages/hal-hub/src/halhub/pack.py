"""The entry point of a standalone pack: `hal-<pack> mcp|tools|call|doctor` (04-hub.md, "Pack entry point").

A pack runs as its own MCP server and command line without Hal core (RESOLUTIONS A3, C9):

    from halhub import pack

    def main() -> int:
        return pack.main("organiser", ("calendar", "tasks", "clock"))

Design decisions:

- The subcommands are `hal-hub`'s own (`halhub.cli.run_scoped`), so a pack gets the same
  pipeline, policy, audit and approvals: only the pack's named modules (and the built-in
  module) are loaded, the default profile is `standalone` (01-security: `default` without
  `system` tools), and `mcp` serves flat mode limited to the pack's tools.
- `extra` adds human-only subcommands, such as a password prompt or a long-running bridge.
  They run in the pack's process with no pipeline, no session and no approvals, and they are
  never tools: nothing lists, searches or calls them over MCP (REQ-HUB-49). A name that
  clashes with a built-in subcommand, or does not match `EXTRA_NAME_RE`, raises `ValueError`
  when `main` starts, so the mistake shows on the pack author's first run.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Mapping, Sequence

from .cli import PACK_COMMANDS, Scope, run_scoped

__all__ = ["EXTRA_NAME_RE", "STANDALONE_PROFILE", "main"]

STANDALONE_PROFILE = "standalone"  # RESOLUTIONS C9
EXTRA_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")  # REQ-HUB-49
PACK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")  # a distribution name `hal-<pack>` (RESOLUTIONS C7)


def main(
    pack: str,
    modules: Sequence[str],
    argv: Sequence[str] | None = None,
    *,
    extra: Mapping[str, Callable[[list[str]], int]] | None = None,
) -> int:
    """Runs the pack CLI for `modules` of `pack` and returns its exit code.

    Raises `ValueError` for a bad pack name, an empty module list, or an `extra` name that
    clashes with a built-in subcommand or does not match `EXTRA_NAME_RE`.
    """
    if not PACK_NAME_RE.fullmatch(pack):
        raise ValueError(f"pack name {pack!r} must match {PACK_NAME_RE.pattern}")
    if not modules:
        raise ValueError(f"pack {pack} names no modules")
    commands = dict(extra or {})
    for name in commands:
        if name in PACK_COMMANDS:
            raise ValueError(f"extra subcommand {name!r} clashes with the built-in {name}")
        if not EXTRA_NAME_RE.fullmatch(name):
            raise ValueError(f"extra subcommand {name!r} must match {EXTRA_NAME_RE.pattern}")
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in commands:
        return int(commands[args[0]](args[1:]))
    scope = Scope(prog=f"hal-{pack}", pack=pack, modules=frozenset(modules), profile=STANDALONE_PROFILE)
    return run_scoped(scope, args)
