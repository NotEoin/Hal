"""The `hal-gateway` command line; the only module of the package that prints.

Commands: `serve`, `status`, `mode`, `modes`, `models`, `load`, `unload`, `router stop`,
`backend start|stop|restart`, `release-gpu`, `token`, `render`, `check-config`. Every command
that talks to a running gateway does so through `GatewayClient`. Exit codes: 0 ok, 2 bad
usage, 3 invalid configuration (or a token name that exists), 4 a mode, model or token that was
refused, 5 the gateway is unreachable or failed the call, 6 a conflict (`release-gpu
--reclaim` found the gateway busy, or the router is busy with a request).
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from halkit import config

from . import auth, modes
from .client import ClientError, GatewayClient
from .clienttokens import CLASSES as TOKEN_CLASSES
from .clienttokens import TokenError
from .config import load_config
from .server import Gateway, StartError

EXIT_OK = 0
EXIT_CONFIG = 3  # also: a token that exists, or a bad token name
EXIT_REFUSED = 4
EXIT_GATEWAY = 5
EXIT_BUSY = 6
HTTP_CONFLICT = 409  # the router is busy, or a peer holds the GPU
SERVE_POLL_S = 1.0  # how often `serve` re-checks its stop flag


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the `hal-gateway` command; returns the process exit code."""
    parser = argparse.ArgumentParser(prog="hal-gateway", description="Hal model gateway")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-config", help="validate the [gateway] configuration and exit")
    check.add_argument("--config", type=Path, default=None, help="config file (default: the Hal config file)")
    sub.add_parser("modes", help="list the mode presets").add_argument("--config", type=Path, default=None)
    render = sub.add_parser("render", help="render a mode for llama-server after the security checks")
    render.add_argument("mode")
    render.add_argument("--config", type=Path, default=None)
    release = sub.add_parser("release-gpu", help="unload models and release the GPU claims")
    release.add_argument("--config", type=Path, default=None)
    release.add_argument("--force", action="store_true", help="ignore leases; running requests fail")
    release.add_argument(
        "--stop-router", action="store_true", help="also stop the managed router and managed servers"
    )
    release.add_argument(
        "--reclaim", action="store_true", help="as the gpu-light reclaimer: refuse when busy"
    )
    sub.add_parser("serve", help="run the gateway in the foreground").add_argument(
        "--config", type=Path, default=None
    )
    status = sub.add_parser("status", help="backends, mode, GPU claims, leases, remote warnings")
    status.add_argument("--config", type=Path, default=None)
    status.add_argument("--json", action="store_true")
    mode = sub.add_parser("mode", help="show or switch the managed router's mode")
    mode.add_argument("name", nargs="?")
    mode.add_argument("--config", type=Path, default=None)
    mode.add_argument("--force", action="store_true", help="switch even with requests in flight")
    mode.add_argument("--no-wait", action="store_true", help="do not wait for the model to load")
    models = sub.add_parser("models", help="what /v1/models returns to the owner")
    models.add_argument("--config", type=Path, default=None)
    models.add_argument("--json", action="store_true")
    for verb in ("load", "unload"):
        one = sub.add_parser(verb, help=f"{verb} a router preset")
        one.add_argument("model")
        one.add_argument("--config", type=Path, default=None)
    router = sub.add_parser("router", help="the managed router process")
    router.add_argument("action", choices=("stop",))
    router.add_argument("--config", type=Path, default=None)
    backend = sub.add_parser("backend", help="start, stop or restart a managed-openai server")
    backend.add_argument("action", choices=("start", "stop", "restart"))
    backend.add_argument("name")
    backend.add_argument("--config", type=Path, default=None)
    token = sub.add_parser("token", help="owner and client tokens")
    token.add_argument("--config", type=Path, default=None)
    actions = token.add_subparsers(dest="action", required=True)
    actions.add_parser("show", help="print the owner token")
    actions.add_parser("rotate", help="replace the owner token")
    issue = actions.add_parser("issue", help="mint a client token and print it once")
    issue.add_argument("--name", required=True)
    issue.add_argument("--allow-remote", action="store_true")
    issue.add_argument("--class", dest="cls", default="interactive", choices=TOKEN_CLASSES)
    issue.add_argument("--rotate", action="store_true", help="replace an existing token of that name")
    issue.add_argument("--json", action="store_true")
    actions.add_parser("revoke", help="revoke a client token").add_argument("--name", required=True)
    actions.add_parser("list", help="list client tokens (no secrets)").add_argument(
        "--json", action="store_true"
    )
    args = parser.parse_args(argv)
    if args.command == "token":
        return _token_command(args)
    if args.command == "serve":
        return _serve(args)
    if args.command in ("status", "mode", "models", "load", "unload", "router", "backend"):
        return _client_command(args)
    if args.command == "release-gpu":
        return _release_gpu(args)
    if args.command in ("modes", "render"):
        return _modes_command(args)
    if args.command == "check-config":
        try:
            cfg = load_config(args.config)
        except config.ConfigError as exc:
            print(f"hal-gateway: {exc}", file=sys.stderr)
            return EXIT_CONFIG
        print(f"ok: {len(cfg.backends)} backend(s), {len(cfg.aliases)} alias(es)")
    return EXIT_OK


def _modes_command(args: argparse.Namespace) -> int:
    """Runs `modes` or `render`."""
    try:
        gateway = Gateway(load_config(args.config))
        gateway.load_models()
    except (config.ConfigError, StartError) as exc:
        print(f"hal-gateway: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    user_dirs = list(gateway.router_entry().get("modes_dirs") or ["~/.config/hal/modes"])  # type: ignore[call-overload]
    if args.command == "modes":
        for name, description, path in modes.list_modes(user_dirs):
            print(f"{name:20s} {description}  [{path}]")
        return EXIT_OK
    try:
        rendered = modes.render_mode(
            args.mode,
            models_dir=gateway.models_dir(),
            state_dir=gateway.state_dir,
            verifier=gateway.verifier,
            user_dirs=user_dirs,
        )
    except modes.ModeError as exc:
        print(f"hal-gateway: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"{rendered.path} sha256={rendered.sha256} context_window={rendered.hal.context_window}")
    return EXIT_OK


def _serve(args: argparse.Namespace) -> int:
    """Runs the gateway until SIGTERM or SIGINT, then stops it cleanly."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        gateway = Gateway(load_config(args.config))
        gateway.start()
    except (config.ConfigError, StartError) as exc:
        print(f"hal-gateway: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.wait(SERVE_POLL_S):
        pass
    gateway.stop()
    return EXIT_OK


def _client_command(args: argparse.Namespace) -> int:
    """Runs `status`, `mode`, `models`, `load`, `unload`, `router stop` and `backend` through the client."""
    try:
        client = GatewayClient.from_config(args.config)
        return _run_client_command(client, args)
    except (config.ConfigError, StartError) as exc:
        print(f"hal-gateway: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except ClientError as exc:
        print(f"hal-gateway: {exc}", file=sys.stderr)
        return EXIT_BUSY if exc.status == HTTP_CONFLICT else EXIT_GATEWAY


def _run_client_command(client: GatewayClient, args: argparse.Namespace) -> int:
    """Dispatches one client command; `ClientError` is handled by the caller."""
    if args.command == "status":
        doc = client.status()
        if args.json:
            print(json.dumps(doc))
            return EXIT_OK
        for name, backend in doc.get("backends", {}).items():
            where = f" REMOTE {backend['host']}" if backend.get("remote") else ""
            print(f"{name:16s} {backend.get('type', '?'):14s} {backend.get('state', '-')}{where}")
        for warning in doc.get("warnings", []):
            print(f"warning: {warning}")
    elif args.command == "mode":
        if args.name is None:
            print(json.dumps(client.aliases().get("mode", {})))
        else:
            wait_s = 0.0 if args.no_wait else 900.0
            print(json.dumps(client.set_mode(args.name, wait_s=wait_s, force=args.force)))
    elif args.command == "models":
        rows = client.models()
        if args.json:
            print(json.dumps(rows))
        for row in [] if args.json else rows:
            print(f"{row.get('id', '?'):32s} {row.get('owned_by', '')}")
    elif args.command == "load":
        client.load(args.model)
    elif args.command == "unload":
        client.unload(args.model)
    elif args.command == "backend":
        print(json.dumps(client.backend(args.action, args.name)))
    else:
        client.stop_router()
    return EXIT_OK


def _release_gpu(args: argparse.Namespace) -> int:
    """Releases the GPU through the client and prints the result as JSON."""
    try:
        client = GatewayClient.from_config(args.config)
        result = client.release_gpu(args.force, stop_router=args.stop_router, reclaim=args.reclaim)
    except (config.ConfigError, StartError, ClientError) as exc:
        print(f"hal-gateway: {exc}", file=sys.stderr)
        return EXIT_GATEWAY
    print(json.dumps(result.as_json()))
    return EXIT_BUSY if args.reclaim and result.busy else EXIT_OK


def _token_command(args: argparse.Namespace) -> int:
    """Runs `token show|rotate|issue|revoke|list`; the files are edited directly (REQ-GW-66)."""
    try:
        gateway = Gateway(load_config(args.config))
    except config.ConfigError as exc:
        print(f"hal-gateway: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    path = Path(str(gateway.config["token_file"])).expanduser()
    try:
        if args.action == "show":
            print(auth.load_or_create_owner_token(path))
        elif args.action == "rotate":
            path.unlink(missing_ok=True)
            print(auth.load_or_create_owner_token(path))
        elif args.action == "issue":
            secret = gateway.clients.issue(
                args.name, allow_remote=args.allow_remote, cls=args.cls, rotate=args.rotate
            )
            print(json.dumps({"name": args.name, "token": secret}) if args.json else secret)
        elif args.action == "revoke":
            if not gateway.clients.revoke(args.name):
                print(f"hal-gateway: no token named {args.name!r}", file=sys.stderr)
                return EXIT_REFUSED
        else:
            rows = [t.public() for t in gateway.clients.list()]
            if args.json:
                print(json.dumps(rows))
            for row in [] if args.json else rows:
                print(
                    f"{row['name']:32s} {row['created']}  class={row['class']} remote={row['allow_remote']}"
                )
    except TokenError as exc:
        print(f"hal-gateway: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    return EXIT_OK
