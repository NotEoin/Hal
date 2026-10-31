"""Claude Code profile: Messages API at the relay, MCP through a strict config file."""

from __future__ import annotations

from .base import BRIDGE, DUMMY_KEY, BoxContext, BoxLaunch, check_context, common_env, dump_json

MCP_CONFIG = ".config/hal-box/claude-mcp.json"
QUIET_ENV = {  # the telemetry block of the old `hal` launcher, now quiet offline operation
    name: "1"
    for name in (
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
        "DISABLE_AUTOUPDATER", "DISABLE_UPDATES", "DISABLE_GROWTHBOOK", "DISABLE_BUG_COMMAND",
        "DISABLE_FEEDBACK_COMMAND", "DISABLE_INSTALLATION_CHECKS",
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY", "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL",
        "CLAUDE_CODE_DISABLE_WORKING_SYNC", "CLAUDE_CODE_DISABLE_ARTIFACT",
        "CLAUDE_CODE_DISABLE_WEB_FETCH", "CLAUDE_CODE_DISABLE_VITALS_EMITTER",
        "CLAUDE_CODE_DISABLE_ORG_MEMORY", "CLAUDE_CODE_DISABLE_MEMORY_PERIODIC_RESYNC",
        "CLAUDE_CODE_SKIP_REPO_UPLOAD", "CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK",
        "CLAUDE_CODE_DISABLE_FAST_MODE",
    )
}  # fmt: skip


class Claude:
    """Writer for `claude`."""

    name = "claude"

    def launch(self, ctx: BoxContext) -> BoxLaunch:
        """Env, seed files and argv for Claude Code."""
        check_context(ctx)
        env = {
            **common_env(), **QUIET_ENV,
            "ANTHROPIC_BASE_URL": ctx.relay_url, "ANTHROPIC_AUTH_TOKEN": DUMMY_KEY,
            "ANTHROPIC_MODEL": ctx.model, "ANTHROPIC_DEFAULT_OPUS_MODEL": ctx.model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": ctx.model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": ctx.model,
            "ANTHROPIC_SMALL_FAST_MODEL": ctx.model, "CLAUDE_CODE_SUBAGENT_MODEL": ctx.model,
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(ctx.max_output_tokens),
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(ctx.context_window),
        }  # fmt: skip
        seed = {
            ".claude.json": dump_json(
                {"hasCompletedOnboarding": True, "bypassPermissionsModeAccepted": ctx.auto_approve}
            ),
        }
        argv = ["claude"]
        if ctx.mcp:
            hal = {"type": "stdio", "command": BRIDGE, "args": list(ctx.mcp_command[1:])}
            seed[MCP_CONFIG] = dump_json({"mcpServers": {"hal": hal}})
            argv += ["--mcp-config", f"{ctx.home}/{MCP_CONFIG}"]
        argv.append("--strict-mcp-config")
        if ctx.auto_approve:
            argv.append("--dangerously-skip-permissions")
        return BoxLaunch((*argv, *ctx.user_args), env, seed)
