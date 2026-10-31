"""The `hal-remote` command line; the only module of the package that prints.

Every subcommand of the design (07-remote.md, CLI synopsis) is registered; until its
implementation step lands it exits with `EXIT_USAGE` and says so, so a half-built install fails
loudly instead of doing nothing. Exit codes: 0 ok; 1 check or operation failed; 2 usage, config
or not-implemented; 3 not set up; 4 tunnel not up; 64 and 75 are reserved for `serve`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__, authorize, pairing, serve, servercheck
from . import config as _config  # noqa: F401 - import registers the [remote] tables

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
IMPLEMENTED = frozenset({"server-check", "serve", "authorize", "revoke", "clients", "setup"})

HARNESSES = ("claude", "qwen", "opencode", "codex", "crush", "goose", "cline", "openai", "mcp")
COMMANDS = (
    "setup", "up", "down", "status", "env", "connect", "profiles",
    "authorize", "revoke", "clients", "server-check", "serve", "tunnel-run",
)  # fmt: skip


def build_parser() -> argparse.ArgumentParser:
    """Builds the argparse tree for `hal-remote`."""
    parser = argparse.ArgumentParser(prog="hal-remote", description="Hal remote: SSH add-on")
    parser.add_argument("--version", action="version", version=f"hal-remote {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("server-check", help="check the desktop's sshd and Hal lines (read-only)")
    check.add_argument("--json", action="store_true", help="print the results as JSON")
    check.add_argument(
        "--sshd-t", action="store_true", help="read the effective config with `sudo -n sshd -T`"
    )
    check.add_argument("--fix", action="store_true", help="delete token deliveries past their TTL")
    auth = sub.add_parser("authorize", help="authorize a laptop (desktop side of pairing)")
    auth.add_argument("--client", required=True)
    auth.add_argument("--stdin", action="store_true", help="read the request JSON from stdin")
    auth.add_argument("--tunnel-key")
    auth.add_argument("--mcp-key")
    auth.add_argument("--from", dest="from_pattern", help="from= pattern, or 'any'")
    auth.add_argument("--expires", help="YYYY-MM-DD")
    auth.add_argument("--rotate", action="store_true", help="replace the keys of an existing client")
    auth.add_argument("--rotate-token", action="store_true", help="issue a new token for delivery")
    auth.add_argument("--token-only", action="store_true", help="print a new token for a hand-made client")
    auth.add_argument("--accept-risk", action="append", default=[], metavar="ID")
    auth.add_argument("--json", action="store_true")
    auth.add_argument("--authorized-keys", type=Path, default=None)
    setup = sub.add_parser("setup", help="pair this machine with the desktop (laptop side)")
    setup.add_argument("host", metavar="HOST", help="user@host of the desktop")
    setup.add_argument("--profile", default=pairing.DEFAULT_PROFILE)
    setup.add_argument("--client", default="", help="name the desktop knows this machine by")
    setup.add_argument("--user", default="")
    setup.add_argument("--port", type=int, default=pairing.DEFAULT_SSH_PORT)
    setup.add_argument("--manual", action="store_true", help="copy one command to the desktop by hand")
    setup.add_argument("--from", dest="from_pattern", help="from= pattern, or 'any'")
    setup.add_argument(
        "--remote-hal", default=pairing.DEFAULT_REMOTE_HAL, help="hal-remote path on the desktop"
    )
    setup.add_argument("--host-key-fingerprint", default="", metavar="SHA256:...")
    setup.add_argument("--expires", help="YYYY-MM-DD")
    setup.add_argument("--rotate", action="store_true", help="pair again with new keys")
    sub.add_parser("revoke", help="remove a client").add_argument("name")
    sub.add_parser("clients", help="list authorized clients").add_argument("--json", action="store_true")
    # serve is the forced command: it parses its own arguments and must not print argparse help.
    sub.add_parser("serve", add_help=False).add_argument("args", nargs=argparse.REMAINDER)
    for name in COMMANDS:
        if name in IMPLEMENTED:
            continue
        # Options arrive with each command's step; until then the command takes any arguments.
        sub.add_parser(name, help="not implemented yet", add_help=True).add_argument(
            "args", nargs=argparse.REMAINDER
        )
    return parser


def _server_check(args: argparse.Namespace) -> int:
    from pathlib import Path

    from halkit import proc

    from .config import SERVER_TABLE  # noqa: F401 - TTL default lives with the table

    results = servercheck.run_checks(home=Path.home(), runner=proc.run, sshd_t=args.sshd_t, fix=args.fix)
    if args.json:
        rows = [r.__dict__ for r in results]
        ok = not servercheck.has_critical_failure(results)
        sys.stdout.write(json.dumps({"ok": ok, "checks": rows}, indent=2) + "\n")
    else:
        for r in results:
            sys.stdout.write(f"{r.id} {r.severity:<8} {r.status:<7} {r.title}: {r.detail}\n")
            if r.fix:
                sys.stdout.write(f"     fix: {r.fix}\n")
    return EXIT_FAILED if servercheck.has_critical_failure(results) else EXIT_OK


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(f"hal-remote: {text}\n")


def _authorize(args: argparse.Namespace) -> int:
    from halkit import proc

    from .keys import parse_public_key

    keys_file = args.authorized_keys or Path.home() / ".ssh" / "authorized_keys"
    try:
        resolved = authorize.resolve_paths()
        if args.rotate_token or args.token_only:
            token = authorize.rotate_token(args.client, runner=proc.run, resolved=resolved)
            _out(token if args.token_only else f"new token for {args.client} is waiting for delivery")
            return EXIT_OK
        if args.from_pattern and not (args.stdin or args.tunnel_key):
            authorize.update_from(
                args.client, args.from_pattern, authorized_keys=keys_file, resolved=resolved
            )
            _out(f"updated from= for {args.client}")
            return EXIT_OK
        if args.stdin:
            req = authorize.AuthorizeRequest.from_json(sys.stdin.read())
            if req.client != args.client:
                raise authorize.AuthorizeError("--client does not match the request")
        else:
            req = authorize.AuthorizeRequest.build(
                client=args.client,
                tunnel_key=args.tunnel_key or "",
                mcp_key=args.mcp_key or "",
                from_pattern=args.from_pattern,
                expires=args.expires,
                rotate=args.rotate,
            )
        addr = os.environ.get("SSH_CONNECTION", "").split(" ")[0] or None
        resp = authorize.authorize(
            req,
            client_addr=addr,
            runner=proc.run,
            accept_risks=frozenset(args.accept_risk),
            authorized_keys=keys_file,
            resolved=resolved,
        )
    except authorize.AuthorizeError as exc:
        _err(str(exc))
        return EXIT_FAILED
    if args.json:
        _out(json.dumps(resp))
        return EXIT_OK
    _out(f"authorized {args.client} from {resp['from']}")
    for line in resp["host_keys"]:
        key = parse_public_key(line)
        _out(f"{key.type} {key.fingerprint()}  (compare this on the laptop)")
    if resp["gateway_tcp_enabled"]:
        _out("gateway TCP listener switched on; restart the gateway to apply")
    if resp["checks"]["warnings"]:
        _out("warnings: " + ", ".join(resp["checks"]["warnings"]))
    return EXIT_OK


def _revoke(args: argparse.Namespace) -> int:
    from halkit import proc

    problems = authorize.revoke(
        args.name, runner=proc.run, authorized_keys=Path.home() / ".ssh" / "authorized_keys"
    )
    for problem in problems:
        _err(f"could not remove: {problem}")
    if not problems:
        _out(f"revoked {args.name}")
    return EXIT_FAILED if problems else EXIT_OK


def _clients(args: argparse.Namespace) -> int:
    try:
        rows = authorize.clients()
    except authorize.AuthorizeError as exc:
        _err(str(exc))
        return EXIT_FAILED
    if args.json:
        _out(
            json.dumps(
                [
                    {**r.__dict__, "forwards": list(r.forwards), "accepted_risks": list(r.accepted_risks)}
                    for r in rows
                ]
            )
        )
    for r in [] if args.json else rows:
        _out(f"{r.name:<16} from={r.from_pattern} expires={r.expires or '-'} {r.tunnel_fingerprint}")
    return EXIT_OK


def _setup(args: argparse.Namespace) -> int:
    from halkit import paths

    from .config import REMOTE_TABLE  # noqa: F401 - keeps the [remote] table registered

    def wait(command: str) -> None:
        _out("On the desktop, run this command, then come back here:\n\n  " + command + "\n")
        input("Press Enter when the desktop printed its host key fingerprints: ")

    def confirm(question: str) -> bool:
        return input(question + " [yes/no] ").strip().lower() == "yes"

    opts = pairing.SetupOptions(
        host=args.host, profile=args.profile, client=args.client, user=args.user, port=args.port,
        manual=args.manual, from_pattern=args.from_pattern, remote_hal=args.remote_hal,
        host_key_fingerprint=args.host_key_fingerprint, expires=args.expires, rotate=args.rotate,
    )  # fmt: skip
    try:
        result = pairing.setup(opts, confirm=confirm, wait_for_desktop=wait)
    except (pairing.PairingError, EOFError) as exc:
        _err(str(exc) or "input ended")
        return EXIT_FAILED
    _out(f"paired profile {result.profile.name} with {result.profile.host}")
    if result.gateway_lines:
        _out(
            f"a [gateway] table exists in {paths.config_file()}; add these lines yourself:\n"
            + result.gateway_lines
        )
    if result.mcp_server_lines:
        _out(f"added the desktop's tools to {paths.config_file()}")
    _out("next: hal remote up; hal remote env <harness>")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Runs `hal-remote`; returns the process exit code."""
    words = list(sys.argv[1:] if argv is None else argv)
    if words[:1] == ["serve"]:
        # Bypasses argparse: REMAINDER rejects a first token that is an option (`--client`).
        return serve.main(words[1:])
    args = build_parser().parse_args(words)
    handlers = {"setup": _setup, "authorize": _authorize, "revoke": _revoke, "clients": _clients}
    if args.command in handlers:
        return handlers[args.command](args)
    if args.command == "server-check":
        return _server_check(args)
    sys.stderr.write(f"hal-remote {args.command}: not implemented\n")
    return EXIT_USAGE


def tunnel_main() -> int:
    """Windowless entry point for the tunnel supervisor; runs `tunnel-run`."""
    return main(["tunnel-run", *sys.argv[1:]])
