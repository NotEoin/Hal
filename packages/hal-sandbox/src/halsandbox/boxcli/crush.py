"""Crush profile: an OpenAI-compatible provider at the relay."""

from __future__ import annotations

from .base import BoxContext, BoxLaunch, check_context, common_env, dump_json

ENV = {"CRUSH_DISABLE_METRICS": "1", "CRUSH_DISABLE_PROVIDER_AUTO_UPDATE": "1"}


class Crush:
    """Writer for `crush`."""

    name = "crush"

    def launch(self, ctx: BoxContext) -> BoxLaunch:
        """Env, seed files and argv for crush."""
        check_context(ctx)
        config = {
            "providers": {
                "hal": {
                    "type": "openai-compat",
                    "base_url": f"{ctx.relay_url}/v1",
                    "api_key": "$HAL_API_KEY",
                    "models": [
                        {
                            "id": ctx.model,
                            "name": ctx.model,
                            "context_window": ctx.context_window,
                            "default_max_tokens": ctx.max_output_tokens,
                        }
                    ],  # fmt: skip
                }
            },
            "mcp": {
                "hal": {"type": "stdio", "command": ctx.mcp_command[0], "args": list(ctx.mcp_command[1:])}
            },
            "options": {"disable_metrics": True, "disable_provider_auto_update": True},
        }
        argv = ["crush", *(["--yolo"] if ctx.auto_approve else []), *ctx.user_args]
        return BoxLaunch(
            tuple(argv), {**common_env(), **ENV}, {".config/crush/crush.json": dump_json(config)}
        )
