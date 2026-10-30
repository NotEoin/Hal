"""Goose profile: OpenAI Chat Completions at the relay.

Not yet run against the real binary in a box (02-sandbox REQ-BOX-61); the keys follow goose's
documented `config.yaml`.
"""

from __future__ import annotations

from .base import DUMMY_KEY, BoxContext, BoxLaunch, check_context, common_env, dump_yaml

EXTENSION_TIMEOUT_S = 300  # goose's own default for a slow tool


class Goose:
    """Writer for `goose`."""

    name = "goose"

    def launch(self, ctx: BoxContext) -> BoxLaunch:
        """Env, seed files and argv for goose."""
        check_context(ctx)
        config = {
            "GOOSE_PROVIDER": "openai",
            "GOOSE_MODEL": ctx.model,
            "OPENAI_HOST": ctx.relay_url,
            "OPENAI_BASE_PATH": "v1/chat/completions",
            "extensions": {
                "hal": {
                    "type": "stdio", "cmd": ctx.mcp_command[0], "args": list(ctx.mcp_command[1:]),
                    "enabled": True, "timeout": EXTENSION_TIMEOUT_S,
                }
            },
        }  # fmt: skip
        env = {
            **common_env(), "OPENAI_API_KEY": DUMMY_KEY, "GOOSE_DISABLE_KEYRING": "1",
            "GOOSE_TELEMETRY_ENABLED": "false", "GOOSE_MODE": "auto" if ctx.auto_approve else "approve",
        }  # fmt: skip
        return BoxLaunch(
            ("goose", *ctx.user_args), env, {".config/goose/config.yaml": dump_yaml(config).encode("ascii")}
        )
