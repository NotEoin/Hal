"""opencode profile: an OpenAI-compatible provider at the relay."""

from __future__ import annotations

from .base import BoxContext, BoxLaunch, check_context, common_env, dump_json

ENV = {
    "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_SHARE": "1", "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
}  # fmt: skip


class Opencode:
    """Writer for `opencode`."""

    name = "opencode"

    def launch(self, ctx: BoxContext) -> BoxLaunch:
        """Env, seed files and argv for opencode."""
        check_context(ctx)
        config = {
            "autoupdate": False,
            "share": "disabled",
            "provider": {
                "hal": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Hal",
                    "options": {"baseURL": f"{ctx.relay_url}/v1", "apiKey": "{env:HAL_API_KEY}"},
                    "models": {ctx.model: {"name": ctx.model}},
                }
            },
            "model": f"hal/{ctx.model}",
            "mcp": {"hal": {"type": "local", "command": list(ctx.mcp_command), "enabled": True}},
            "permission": {"edit": "allow", "bash": "allow", "webfetch": "deny"} if ctx.auto_approve else {},
        }
        seed = {".config/opencode/opencode.json": dump_json(config)}
        return BoxLaunch(("opencode", *ctx.user_args), {**common_env(), **ENV}, seed)
