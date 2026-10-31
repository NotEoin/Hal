"""The `hal` command table: one place that maps a command name to the module that runs it.

Modules load lazily, so `hal kill` does not import the TUI. Exit codes follow the table in
docs/design/06-core-cli.md ("Exit codes"); unknown commands and usage errors return `EXIT_USAGE`.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from halkit import config as kitconfig
from halkit import paths

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PREREQUISITE = 3
EXIT_INTERRUPTED = 130
DEFAULT_COMMAND = "chat"  # a bare `hal` opens the conversation


@dataclass(frozen=True)
class Command:
    """One `hal` command: the module that runs it, its one-line help and the args it prepends."""

    module: str
    help: str
    prepend: tuple[str, ...] = ()  # the command name itself, for modules that serve several commands


@dataclass(frozen=True)
class Alias:
    """A shorter or legacy name for a command line; `notice` is printed on stderr when set."""

    target: tuple[str, ...]
    notice: str = ""


COMMANDS: dict[str, Command] = {
    "chat": Command("chat", "talk to Hal in a box"),
    "code": Command("code", "coding session on a project copy"),
    "acp": Command("acpcmd", "ACP proxy for editors and channels"),
    "jobs": Command("deepwork", "list, follow, cancel and accept background jobs"),
    "sessions": Command("sessionscmd", "list, resume, fork and export sessions"),
    "lockdown": Command("levels", "show or explain the lockdown level", ("lockdown",)),
    "paranoia": Command("levels", "show or explain the paranoia level", ("paranoia",)),
    "policy": Command("policycli", "inspect and edit the approval policy", ("policy",)),
    "audit": Command("policycli", "read the audit log", ("audit",)),
    "guard": Command("guardcli", "prompt-injection guard tools"),
    "approvals": Command("halhub.cli", "list pending approvals", ("approvals",)),
    "approve": Command("halhub.cli", "approve a pending request", ("approve",)),
    "deny": Command("halhub.cli", "deny a pending request", ("deny",)),
    "gateway": Command("halgateway.cli", "model gateway admin (hal-gateway passthrough)"),
    "box": Command("halsandbox.cli", "run an agent CLI in the box, build box images"),
    "kill": Command("killcmd", "stop everything Hal started"),
    "scratch": Command("scratch", "inspect and clean box scratchpads"),
    "security": Command("security", "security status and doctor"),
    "doctor": Command("doctor", "check docker, runsc, the model server, images and permissions"),
    "status": Command("doctor", "same report as doctor"),
    "power": Command("power", "GPU power state"),
    "gpulight": Command("gpulight", "gpu-light integration"),
    "units": Command("units", "render and install systemd units"),
    "configure": Command("configure", "write the configuration"),
    "console": Command("console", "local web console (UNBOXED: runs on the host, real data)"),
    "eval": Command("evaluate", "run the evaluation suites"),
    "migrate": Command("migrate", "move from the old hal-ecosystem layout (dry run by default)"),
    "ext": Command("ext", "extensions"),
}

ALIASES: dict[str, Alias] = {
    "-p": Alias(("headless",)),
    "--print": Alias(("headless",)),
    "on": Alias(("power", "on")),
    "off": Alias(("power", "off")),
    "toggle": Alias(("power", "toggle")),
    "install": Alias(("units", "install", "--start"), "hal: 'hal install' is now 'hal units install --start'"),
}

# `-p` has no subcommand name of its own, so it maps to a module directly.
HEADLESS = Command("headless", "one prompt, no TUI (hal -p)")


def default_command() -> str:
    """What a bare `hal` runs: `code` in a project folder, `chat` elsewhere (REQ-CLI-08)."""
    from . import launch  # lazy: it imports the policy profiles

    requested = kitconfig.read_toml(paths.config_file()).get("ui", {}).get("default_mode", "auto")
    if requested not in ("auto", "code", "chat"):
        raise ValueError(f"[ui].default_mode = {requested!r} is unknown; use auto, code or chat")
    try:
        return launch.resolve_target(Path.cwd(), requested, home=Path.home()).mode
    except launch.ForbiddenRoot:
        return DEFAULT_COMMAND


def resolve(argv: list[str]) -> tuple[Command, list[str]] | None:
    """The command and its arguments for `argv` (without the program name), or None when unknown."""
    name, rest = (argv[0], argv[1:]) if argv else (default_command(), [])
    alias = ALIASES.get(name)
    if alias:
        if alias.notice:
            print(alias.notice, file=sys.stderr)
        if alias.target == ("headless",):
            return HEADLESS, rest
        name, rest = alias.target[0], [*alias.target[1:], *rest]
    command = COMMANDS.get(name)
    if command is None:
        return None
    return command, [*command.prepend, *rest]


def usage() -> str:
    """The command list `hal --help` prints."""
    width = max(map(len, COMMANDS))
    lines = ["usage: hal [COMMAND] [ARGS]", "", "commands:"]
    lines += [f"  {name:<{width}}  {cmd.help}" for name, cmd in sorted(COMMANDS.items())]
    lines += ["", "a bare `hal` runs `hal chat`; `hal -p PROMPT` runs headless"]
    return "\n".join(lines)


def load(command: Command) -> Callable[[list[str]], int | None]:
    """The `main` function of the command's module."""
    package = __package__ or "halcore"
    name = command.module if "." in command.module else f"{package}.{command.module}"
    return importlib.import_module(name).main


def main(argv: list[str] | None = None) -> int:
    """Runs one `hal` command line and returns the exit status."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["--help"], ["-h"], ["help"]):
        print(usage())
        return EXIT_OK
    found = resolve(argv)
    if found is None:
        print(f"hal: unknown command {argv[0]!r}; `hal --help` lists the commands", file=sys.stderr)
        return EXIT_USAGE
    command, args = found
    return load(command)(args) or EXIT_OK
