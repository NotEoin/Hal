"""Qwen Code profile: OpenAI Chat Completions at the relay."""

from __future__ import annotations

from .base import BoxContext, BoxLaunch, check_context, common_env, dump_json


class Qwen:
    """Writer for `qwen`."""

    name = "qwen"

    def launch(self, ctx: BoxContext) -> BoxLaunch:
        """Env, seed files and argv for Qwen Code."""
        check_context(ctx)
        settings = {
            "$version": 4,
            "security": {"auth": {"selectedType": "openai"}},
            "model": {"name": ctx.model},
            "modelProviders": {
                "openai": [
                    {
                        "id": ctx.model,
                        "name": "Hal",
                        "baseUrl": f"{ctx.relay_url}/v1",
                        "envKey": "HAL_API_KEY",
                    }
                ]
            },
            "privacy": {"usageStatisticsEnabled": False},
            "telemetry": {"enabled": False},
            "general": {"enableAutoUpdate": False},
        }  # SETTINGS_VERSION 4 is in the 0.25.0 bundle (02-sandbox, checked 2026-10-23)
        if ctx.mcp:
            settings["mcpServers"] = {
                "hal": {"command": ctx.mcp_command[0], "args": list(ctx.mcp_command[1:])}
            }
        argv = ["qwen", *(["--approval-mode", "yolo"] if ctx.auto_approve else []), *ctx.user_args]
        return BoxLaunch(tuple(argv), common_env(), {".qwen/settings.json": dump_json(settings)})
