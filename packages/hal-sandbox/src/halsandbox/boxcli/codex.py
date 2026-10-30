"""Codex profile: the Responses API at the relay; codex's own sandbox is off because it cannot nest."""

from __future__ import annotations

from .base import BoxContext, BoxLaunch, check_context, common_env, dump_toml

MCP_STARTUP_TIMEOUT_S = 20  # the bridge starts in well under a second; this only bounds a hang


class Codex:
    """Writer for `codex`."""

    name = "codex"

    def launch(self, ctx: BoxContext) -> BoxLaunch:
        """Env, seed files and argv for codex."""
        check_context(ctx)
        top = {
            "model": ctx.model,
            "model_provider": "hal",
            "approval_policy": "never" if ctx.auto_approve else "on-request",
            "sandbox_mode": "danger-full-access",  # codex's bwrap cannot create namespaces in the box
            "check_for_update_on_startup": False,
            "model_context_window": ctx.context_window,
        }
        tables = {
            "analytics": {"enabled": False},
            "otel": {"exporter": "none"},
            "model_providers.hal": {
                "name": "Hal", "base_url": f"{ctx.relay_url}/v1", "env_key": "HAL_API_KEY",
                "wire_api": "responses",
            },
            "mcp_servers.hal": {
                "command": ctx.mcp_command[0], "args": list(ctx.mcp_command[1:]),
                "startup_timeout_sec": MCP_STARTUP_TIMEOUT_S,
            },
        }  # fmt: skip
        return BoxLaunch(
            ("codex", *ctx.user_args), common_env(), {".codex/config.toml": dump_toml(top, tables)}
        )
