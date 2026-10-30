"""The `hal-gateway` command line; the only module of the package that prints.

Commands so far: `check-config`, `modes`, `render`, `release-gpu`. Exit codes: 0 ok, 2 bad usage, 3 invalid
configuration, 4 a mode or model that was refused, 5 the gateway is
unreachable or refused the call, 6 `release-gpu --reclaim` found the gateway busy.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from halkit import config

from . import modes
from .client import ClientError, GatewayClient
from .config import load_config
from .server import Gateway, StartError

EXIT_OK = 0
EXIT_CONFIG = 3
EXIT_REFUSED = 4
EXIT_GATEWAY = 5
EXIT_BUSY = 6


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
    release.add_argument("--stop-router", action="store_true", help="also stop the managed router")
    release.add_argument(
        "--reclaim", action="store_true", help="as the gpu-light reclaimer: refuse when busy"
    )
    args = parser.parse_args(argv)
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


def _release_gpu(args: argparse.Namespace) -> int:
    """Posts `/hal/v1/gpu/release` and prints the result as JSON."""
    try:
        gateway = Gateway(load_config(args.config))
        token = Path(str(gateway.config["token_file"])).expanduser().read_text(encoding="ascii").strip()
        client = GatewayClient(gateway.socket_path(), token)
        body = {"force": args.force, "stop_router": args.stop_router, "reclaim": args.reclaim}
        result = client.admin("POST", "/hal/v1/gpu/release", body)
    except (config.ConfigError, StartError, OSError, ClientError) as exc:
        print(f"hal-gateway: {exc}", file=sys.stderr)
        return EXIT_GATEWAY
    print(json.dumps(result))
    return EXIT_BUSY if args.reclaim and result.get("busy") else EXIT_OK
