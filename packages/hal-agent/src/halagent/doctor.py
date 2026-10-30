"""`hal-agent doctor`: independent checks of the setup, one result per check.

Each check returns a `Check`; a failing check never stops the others. Nothing here prints:
the command line renders the results.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from halagent.agentfile import AgentFileError, load_agent_file
from halagent.backends import BrokerBackend
from halagent.mcp.lite import McpError
from halagent.profiles import ProfileError, resolve_profile
from halagent.provider import ChatProvider, ProviderError
from halagent.settings import AgentSettings, load_settings
from halkit import config, paths

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass(frozen=True)
class Check:
    """One check: `status` is `ok`, `warn` or `fail`."""

    name: str
    status: str
    detail: str


def run_checks(config_file: Path | None = None, cli: dict | None = None) -> list[Check]:
    """Runs every check and returns the results in a fixed order."""
    out: list[Check] = []
    settings: AgentSettings | None = None
    sandbox = "none (standalone)"
    try:
        if config_file:
            run = load_agent_file(config_file)
            settings = run.to_settings()
            sandbox = str(run.data["sandbox"])
        else:
            settings = load_settings(cli=cli)
        out.append(Check("config", OK, f"model {settings.model}, endpoint {settings.endpoint}"))
    except (config.ConfigError, ValueError, AgentFileError) as exc:
        out.append(Check("config", FAIL, str(exc)))
    out.append(Check("sandbox", OK if sandbox != "none (standalone)" else WARN, sandbox))
    if settings:
        out += [_gateway(settings), _profile(settings), _broker(settings)]
    for tool in ("rg", "bash"):
        found = shutil.which(tool)
        status = OK if found else (WARN if tool == "rg" else FAIL)
        out.append(
            Check(
                tool, status, found or "not found" + (" (grep falls back to Python)" if tool == "rg" else "")
            )
        )
    return out


def _gateway(settings: AgentSettings) -> Check:
    provider = ChatProvider(settings.endpoint, settings.model, api_key=settings.api_key)
    try:
        status, data = provider.get_json("/v1/models")
    except ProviderError as exc:
        return Check("gateway", FAIL, str(exc))
    if status != 200 or not isinstance(data, dict):
        return Check("gateway", FAIL, f"GET /v1/models answered HTTP {status}")
    ids = [r.get("id") for r in data.get("data") or [] if isinstance(r, dict)]
    if settings.model not in ids:
        return Check(
            "gateway", WARN, f"answers, but model {settings.model!r} is not listed ({len(ids)} models)"
        )
    return Check("gateway", OK, f"answers, model {settings.model!r} is listed")


def _profile(settings: AgentSettings) -> Check:
    try:
        profile = resolve_profile(settings.model, user_file=paths.config_dir() / "model-profiles.toml")
    except ProfileError as exc:
        return Check("profile", FAIL, str(exc))
    return Check("profile", OK, f"family {profile.family}")


def _broker(settings: AgentSettings) -> Check:
    if not settings.broker:
        return Check("broker", WARN, "not configured: no host tools")
    try:
        backend = BrokerBackend(settings.broker)
        try:
            count = len(backend.list_tools())
        finally:
            backend.close()
    except (McpError, OSError, ValueError) as exc:
        return Check("broker", FAIL, str(exc))
    return Check("broker", OK, f"{count} tools listed")


def exit_code(checks: list[Check], ok_code: int, fail_code: int) -> int:
    """`ok_code` unless some check failed."""
    return fail_code if any(c.status == FAIL for c in checks) else ok_code
