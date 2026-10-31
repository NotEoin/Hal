"""`hal connect HARNESS`: print the settings that point an outside coding tool at Hal.

The harness runs on the host, unsandboxed, so the first line says so (REQ-CLI-43). The snippet
refers to the token file with `$(cat ...)`, never the secret, so it can be pasted into notes.
Design decisions:

- One named token per harness (`connect-<harness>`), minted by the gateway CLI in a subprocess
  with a timeout, never the owner token (REQ-CLI-104). The secret goes to a 0600 file only.
- The gateway's TCP listener is switched on in `config.toml` only after a notice on stderr; the
  gateway reads it at start, so the notice says to restart it (REQ-CLI-104).
- `--write` supports the harnesses whose config is a JSON file with an `env` block (`claude`,
  `qwen`). It shows a diff with the token masked, needs `y`, and keeps the old file as
  `<file>.hal.bak`. The other harnesses keep the printed snippet: their formats are not a
  safe merge.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import tomllib
from collections.abc import Callable
from typing import TextIO
from pathlib import Path

from halkit import files, paths, proc

from . import levels

GATEWAY_URL = "http://127.0.0.1:8741"  # the gateway's default loopback listener (05)
ISSUE_TIMEOUT_S = 30.0
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2

# harness -> (env style, one-line note). "openai" harnesses take an OpenAI-compatible base URL.
HARNESSES: dict[str, tuple[str, str]] = {
    "claude": ("anthropic", "Claude Code"),
    "qwen": ("openai", "Qwen Code"),
    "opencode": ("openai", "opencode"),
    "codex": ("openai", "Codex CLI"),
    "crush": ("openai", "Crush"),
    "goose": ("openai", "Goose"),
    "cline": ("openai", "Cline"),
}
# harness -> config file under the home directory; each holds a JSON object with an "env" block.
WRITE_TARGETS: dict[str, tuple[str, ...]] = {"claude": (".claude", "settings.json"), "qwen": (".qwen", "settings.json")}
MASK = "<token>"
MCP_ENTRY = {"mcpServers": {"hal": {"command": "hal-hub", "args": ["mcp"]}}}


def token_file(harness: str):
    """Where the named client token for `harness` is kept."""
    return paths.config_dir() / "tokens" / f"connect-{harness}"


def render(harness: str) -> str:
    """The snippet for `harness`, warning line first."""
    style, label = HARNESSES[harness]
    ref = f'"$(cat {token_file(harness)})"'
    lines = [
        f"# UNSANDBOXED: {label} and every tool it runs work on your host as you; Hal's approvals are "
        f"advisory here. Use `hal box -- {harness}` for the sandboxed version.",
    ]
    if style == "anthropic":
        lines += [f"export ANTHROPIC_BASE_URL={GATEWAY_URL}", f"export ANTHROPIC_AUTH_TOKEN={ref}"]
    else:
        lines += [f"export OPENAI_BASE_URL={GATEWAY_URL}/v1", f"export OPENAI_API_KEY={ref}"]
    lines += ["export HAL_HARNESS=1", f"# MCP server entry for {harness}:", json.dumps(MCP_ENTRY)]
    if not token_file(harness).exists():
        lines.append(f"# no token yet: run `hal connect {harness} --issue`")
    return "\n".join(lines)


def issue_token(harness: str) -> None:
    """Mints the named token through the gateway CLI and stores it in a 0600 file."""
    done = proc.run(
        [sys.executable, "-m", "halgateway", "token", "issue", "--name", f"connect-{harness}", "--rotate"],
        timeout=ISSUE_TIMEOUT_S,
        check=False,
    )
    secret = done.stdout.decode().strip()
    if done.returncode != 0 or not secret:
        raise RuntimeError(done.stderr.decode(errors="replace").strip() or "the gateway issued no token")
    files.write_atomic(token_file(harness), secret + "\n")


def env_block(harness: str, token: str) -> dict[str, str]:
    """The environment variables that point `harness` at the gateway."""
    if HARNESSES[harness][0] == "anthropic":
        return {"ANTHROPIC_BASE_URL": GATEWAY_URL, "ANTHROPIC_AUTH_TOKEN": token}
    return {"OPENAI_BASE_URL": f"{GATEWAY_URL}/v1", "OPENAI_API_KEY": token}


def ensure_tcp(err: TextIO | None = None) -> bool:
    """Turns `[gateway].tcp` on after a notice; returns True when it changed the file."""
    path = paths.config_file()
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if tomllib.loads(text).get("gateway", {}).get("tcp") is True:
        return False
    print(
        f"hal connect: turning on the gateway's loopback TCP listener ({GATEWAY_URL}) in {path}; "
        "restart the gateway (hal stop, hal serve) for it to take effect.",
        file=err or sys.stderr,
    )
    new = levels.set_key(text, "gateway", "tcp", "true", raw=True)
    tomllib.loads(new)  # never write a file the readers cannot parse
    files.write_atomic(path, new)
    return True


def write_config(harness: str, *, home: Path, confirm: Callable[[str], str] = input, out: TextIO | None = None) -> int:
    """Merges the gateway settings into the harness config after a diff and `y`; returns an exit code."""
    out = out or sys.stdout
    parts = WRITE_TARGETS.get(harness)
    if parts is None:
        print(f"hal connect: --write is not supported for {harness}; paste the snippet instead", file=sys.stderr)
        return EXIT_USAGE
    tfile = token_file(harness)
    if not tfile.is_file():
        print(f"hal connect: no token yet: run `hal connect {harness} --issue`", file=sys.stderr)
        return EXIT_FAILED
    target = home.joinpath(*parts)
    old = target.read_text(encoding="utf-8") if target.is_file() else ""
    try:
        data = json.loads(old) if old.strip() else {}
        env = data.setdefault("env", {})
    except (json.JSONDecodeError, AttributeError) as exc:
        print(f"hal connect: {target} is not a JSON object with an env block: {exc}", file=sys.stderr)
        return EXIT_FAILED
    secret = tfile.read_text(encoding="utf-8").strip()
    env.update(env_block(harness, secret))
    new = json.dumps(data, indent=2) + "\n"
    shown = new.replace(secret, MASK)  # the secret never reaches the terminal
    diff = difflib.unified_diff(old.splitlines(True), shown.splitlines(True), str(target), f"{target} (new)")
    print("".join(diff), file=out)
    if confirm(f"Write {target}? [y/N] ").strip().lower() != "y":
        print("hal connect: nothing written", file=out)
        return EXIT_OK
    if old:
        files.write_atomic(target.with_name(target.name + ".hal.bak"), old)
    files.write_atomic(target, new, mode=0o600)
    print(f"hal connect: wrote {target}", file=out)
    return EXIT_OK


def main(argv: list[str]) -> int:
    """Runs `hal connect`."""
    parser = argparse.ArgumentParser(prog="hal connect", description="print settings for an outside coding tool")
    parser.add_argument("harness", nargs="?", choices=sorted(HARNESSES))
    parser.add_argument("--list", action="store_true", help="list the known harnesses")
    parser.add_argument("--issue", action="store_true", help="mint the named client token first")
    parser.add_argument("--write", action="store_true", help="merge the settings into the harness config")
    parser.add_argument("--json", action="store_true", help="print the snippet lines as JSON")
    args = parser.parse_args(argv)
    if args.list:
        print("\n".join(f"{name:<9} {label}" for name, (_, label) in sorted(HARNESSES.items())))
        return EXIT_OK
    if not args.harness:
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    if args.issue:
        try:
            issue_token(args.harness)
        except (RuntimeError, OSError, proc.ProcTimeout) as exc:
            print(f"hal connect: could not issue a token: {exc}", file=sys.stderr)
            return EXIT_FAILED
    try:
        ensure_tcp()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"hal connect: could not enable the gateway TCP listener: {exc}", file=sys.stderr)
        return EXIT_FAILED
    if args.write:
        return write_config(args.harness, home=Path.home())
    text = render(args.harness)
    print(json.dumps(text.splitlines()) if args.json else text)
    return EXIT_OK
