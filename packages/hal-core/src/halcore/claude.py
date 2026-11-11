"""`hal claude`: Claude Code in the current folder, on your models through the gateway.

    hal claude [--cloud] [--vault | --dir DIR] [CLAUDE_ARGS...]

Claude Code runs on the host, unsandboxed, so the first line says so (REQ-CLI-44); `hal box --
claude` is the sandboxed version. It asks the gateway for `hal/main` (the conversation) and
`hal/agent` (subagents and side requests), which serve the aliases `hal tune` sets. Design
decisions (06-core-cli, "hal claude"):

- The gateway is reached on its loopback TCP listener with the named client token `claude`
  (REQ-CLI-104), never the owner token; the listener is switched on with a notice, as `hal
  connect` does.
- Claude Code assumes a 200K window for a model it does not know and would compact too late, so
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS` is the context the gateway reports for `hal/main`.
- With a local model nothing should leave the machine: every Claude Code feature that talks to
  a service is switched off (`PRIVACY_ENV`, the list of the old `hal` launcher), and all HTTP(S)
  goes to a proxy address that refuses connections unless `[claude] allow_network` (REQ-CLI-45).
  Commands Claude Code runs inherit the backstop. This is privacy, not security: the process can
  ignore it.
- `--permission-mode default` (ask) unless the user passed one: a local model must not edit
  files unasked.
- `--cloud` runs Claude Code with your own Anthropic login and none of the above.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from halgateway import client as gwclient

from . import config, connect, modellink

log = logging.getLogger(__name__)

HARNESS = "claude"
MAIN, AGENT = "hal/main", "hal/agent"
EXIT_REFUSED = 3  # 06-core-cli exit code table: a prerequisite is missing
NOTICE = (
    "hal: Claude Code runs unsandboxed on your host; the network block is advisory and Hal's "
    "approvals are advisory. Use 'hal box -- claude' for the sandboxed version."
)
DEFAULT_MAX_OUTPUT = 32000  # Claude Code's own default for a reply
DEFAULT_TIMEOUT_MS = 3_600_000  # a long local prefill or a model server's cold start must not time out
BACKSTOP = "http://127.0.0.1:9"  # nothing listens on the discard port, so every connection is refused
LOOPBACK = "localhost,127.0.0.1,::1"
PROXY_NAMES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
PRIVACY_ENV = {
    name: "1"
    for name in (
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
        "DISABLE_AUTOUPDATER", "DISABLE_UPDATES", "DISABLE_GROWTHBOOK", "DISABLE_BUG_COMMAND",
        "DISABLE_FEEDBACK_COMMAND", "DISABLE_INSTALLATION_CHECKS", "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY",
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL", "CLAUDE_CODE_DISABLE_WORKING_SYNC",
        "CLAUDE_CODE_DISABLE_ARTIFACT", "CLAUDE_CODE_DISABLE_WEB_FETCH", "CLAUDE_CODE_DISABLE_VITALS_EMITTER",
        "CLAUDE_CODE_DISABLE_ORG_MEMORY", "CLAUDE_CODE_DISABLE_MEMORY_PERIODIC_RESYNC",
        "CLAUDE_CODE_SKIP_REPO_UPLOAD", "CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK", "CLAUDE_CODE_DISABLE_FAST_MODE",
    )
} | {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}  # fmt: skip


def gateway_env(token: str, main_context: int, settings: Mapping[str, object]) -> dict[str, str]:
    """The variables that point Claude Code at the gateway's `hal/main` and `hal/agent`."""
    env = {
        **connect.env_block(HARNESS, token),
        "ANTHROPIC_API_KEY": token,
        "ANTHROPIC_MODEL": MAIN,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": MAIN,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": MAIN,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": AGENT,
        "ANTHROPIC_SMALL_FAST_MODEL": AGENT,
        "CLAUDE_CODE_SUBAGENT_MODEL": AGENT,
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(settings.get("max_output_tokens") or DEFAULT_MAX_OUTPUT),
        "API_TIMEOUT_MS": str(settings.get("api_timeout_ms") or DEFAULT_TIMEOUT_MS),
        "HAL_HARNESS": "1",
        **PRIVACY_ENV,
    }
    if main_context > 0:
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(main_context)
    if not settings.get("allow_network"):
        env |= {name: BACKSTOP for name in PROXY_NAMES} | {"NO_PROXY": LOOPBACK, "no_proxy": LOOPBACK}
    extra = settings.get("env")
    if isinstance(extra, dict):
        env |= {str(k): str(v) for k, v in extra.items()}
    return env


def with_permission_mode(args: Sequence[str]) -> list[str]:
    """`args` with `--permission-mode default` first, unless the user chose a mode."""
    if any(a.startswith("--permission-mode") or a == "--dangerously-skip-permissions" for a in args):
        return list(args)
    return ["--permission-mode", "default", *args]


def main_context(owner: gwclient.GatewayClient) -> int:
    """The context the gateway reports for `hal/main`; 0 when it reports none."""
    for row in owner.aliases().get("aliases", []):
        if isinstance(row, dict) and row.get("name") == MAIN:
            value = row.get("context_window")
            return value if isinstance(value, int) else 0
    return 0


def _token() -> str:
    tfile = connect.token_file(HARNESS)
    if not tfile.is_file():
        connect.issue_token(HARNESS)
    return tfile.read_text(encoding="utf-8").strip()


def main(argv: list[str]) -> int:
    """Runs `hal claude`; replaces this process with Claude Code, or returns an exit status."""
    p = argparse.ArgumentParser(prog="hal claude", description="Claude Code on your models (unsandboxed)")
    p.add_argument("--cloud", action="store_true", help="your own Anthropic login instead of Hal's models")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--vault", action="store_true", help="start in the vault ([hal] vault)")
    where.add_argument("--dir", type=Path, help="start in DIR")
    opts, claude_args = p.parse_known_args(argv)
    print(NOTICE, file=sys.stderr)
    binary = shutil.which("claude")
    if binary is None:
        print("hal claude: Claude Code is not on your PATH; install it, then open a new terminal", file=sys.stderr)
        return EXIT_REFUSED
    target = Path(config.shared()["vault"]).expanduser() if opts.vault else (opts.dir or Path.cwd())
    env = dict(os.environ)
    args = list(claude_args)
    if not opts.cloud:
        sock = modellink.gateway_socket()
        if sock is None:
            print("hal claude: the Hal gateway is not running; start it with hal-gateway serve", file=sys.stderr)
            return EXIT_REFUSED
        try:
            if connect.ensure_tcp():
                print("hal claude: restart the gateway, then run hal claude again", file=sys.stderr)
                return EXIT_REFUSED
            context = main_context(gwclient.GatewayClient(sock, ""))
            env |= gateway_env(_token(), context, config.load().get("claude", {}))
        except (gwclient.ClientError, RuntimeError, OSError) as exc:
            print(f"hal claude: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        args = with_permission_mode(args)
    os.chdir(target)
    os.execve(binary, [binary, *args], env)  # noqa: S606 - Claude Code replaces this process, as `exec claude` did
    return 0  # not reached: execve does not return
