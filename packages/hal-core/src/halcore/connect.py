"""`hal connect HARNESS`: print the settings that point an outside coding tool at Hal.

The harness runs on the host, unsandboxed, so the first line says so (REQ-CLI-43). The snippet
refers to the token file with `$(cat ...)`, never the secret, so it can be pasted into notes.
Design decisions:

- One named token per harness (`connect-<harness>`), minted by the gateway CLI in a subprocess
  with a timeout, never the owner token (REQ-CLI-104). The secret goes to a 0600 file only.
- Not done yet, by design of this step: `--write` of the harness config and switching the
  gateway's TCP listener on. The output tells the person to set `[gateway].tcp = true`.
"""

from __future__ import annotations

import argparse
import json
import sys

from halkit import files, paths, proc

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
        "# The gateway needs [gateway].tcp = true in config.toml for this URL to answer.",
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


def main(argv: list[str]) -> int:
    """Runs `hal connect`."""
    parser = argparse.ArgumentParser(prog="hal connect", description="print settings for an outside coding tool")
    parser.add_argument("harness", nargs="?", choices=sorted(HARNESSES))
    parser.add_argument("--list", action="store_true", help="list the known harnesses")
    parser.add_argument("--issue", action="store_true", help="mint the named client token first")
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
    text = render(args.harness)
    print(json.dumps(text.splitlines()) if args.json else text)
    return EXIT_OK
