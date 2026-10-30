"""Host MCP servers offered to the box through the broker (REQ-BOX-66).

An entry of 03-agent's `[mcp.servers.<name>]` table with `host = true` runs here, on the host,
as a stdio MCP client, and its tools join the hub registry as `<name>__<tool>` through
`halhub`'s `ToolProvider` hook. The hub then applies policy, approvals and audit to them like
any other tool. Constraints, each with its reason:

- The upstream's environment is `PATH`, `HOME`, `LANG` and `SSH_AUTH_SOCK` plus the entry's own
  `env`: an SSH upstream needs the user's agent, and nothing else of the host leaks to it.
- Upstream annotations are never read: `halhub` sets tier, `untrusted_output` and
  `reviewable` itself (REQ-SEC-88).
- No elicitation or sampling handler is registered, so an upstream request for either fails.
  The approval mapping of REQ-BOX-77 is a later step.
- A failed upstream gives error results; it never stops the hub or the broker (RESOLUTIONS E6).
- Startup is bounded by `UPSTREAM_STARTUP_TIMEOUT_S` and each call by the entry's `timeout_s`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

from halhub.hub import Hub
from halhub.policy.model import Tier
from halhub.providers import PROVIDER_NAME_RE, provider_tool
from halhub.runner import RunOutcome

from .errors import SandboxError

log = logging.getLogger("halsandbox.upstreams")

UPSTREAM_STARTUP_TIMEOUT_S = 20.0  # REQ-BOX-66: an SSH connection plus the far side's `tools/list`
DEFAULT_TIMEOUT_S = 60.0  # 03-agent's default `timeout_s`
CLOSE_TIMEOUT_S = 5.0
DEFAULT_RESTART_LIMIT = 1  # 03-agent's `[mcp].restart_limit`
BASE_ENV = ("PATH", "HOME", "LANG", "SSH_AUTH_SOCK")


@dataclass(frozen=True)
class UpstreamConfig:
    """One enabled `host = true` entry of `[mcp.servers]`."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    tools_allow: tuple[str, ...] = ()
    tools_deny: tuple[str, ...] = ()
    restart_limit: int = DEFAULT_RESTART_LIMIT


def parse_upstreams(
    servers: Mapping[str, Mapping[str, Any]], *, restart_limit: int = DEFAULT_RESTART_LIMIT
) -> list[UpstreamConfig]:
    """The upstreams among `servers`; raises `SandboxError` for an entry the broker cannot run.

    Entries without `host = true`, and disabled ones, are skipped: they never run on the host.
    """
    out: list[UpstreamConfig] = []
    for name, raw in servers.items():
        if not raw.get("host") or not raw.get("enabled", True):
            continue
        if not PROVIDER_NAME_RE.fullmatch(name):
            raise SandboxError(
                f"mcp server {name!r}: a host server name must match {PROVIDER_NAME_RE.pattern} "
                "(lowercase letters and digits), because it prefixes the tool names"
            )
        if "url" in raw or "headers" in raw:
            raise SandboxError(f"mcp server {name!r}: url entries are not supported; use a stdio command")
        command = raw.get("command")
        if not isinstance(command, str) or not command:
            raise SandboxError(f"mcp server {name!r}: command must be a non-empty string")
        out.append(
            UpstreamConfig(
                name=name,
                command=command,
                args=tuple(str(a) for a in raw.get("args", ())),
                env={str(k): str(v) for k, v in dict(raw.get("env", {})).items()},
                cwd=str(raw.get("cwd", "")),
                timeout_s=float(raw.get("timeout_s", DEFAULT_TIMEOUT_S)),
                tools_allow=tuple(raw.get("tools_allow", ())),
                tools_deny=tuple(raw.get("tools_deny", ())),
                restart_limit=restart_limit,
            )
        )
    return out


def upstream_env(cfg: UpstreamConfig) -> dict[str, str]:
    """The base environment plus the entry's own `env`."""
    base = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    return {**base, **cfg.env}


def _render(result: Any) -> str:
    """Text blocks joined; other blocks replaced by a marker; `structured_content` when no text."""
    parts, has_text = [], False
    for block in result.content:
        if getattr(block, "type", "") == "text":
            parts.append(block.text)
            has_text = True
        else:
            parts.append(f"[{getattr(block, 'type', 'unknown')} content omitted]")
    if not has_text and getattr(result, "structured_content", None):
        return json.dumps(result.structured_content, indent=1)
    return "\n".join(parts)


class McpUpstream:
    """A `halhub` `ToolProvider` backed by one host stdio MCP server."""

    default_tier = Tier.ACT

    def __init__(self, cfg: UpstreamConfig) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.failed = ""
        self._restarts = 0
        self._client: Client | None = None
        self._stack: AsyncExitStack | None = None
        self._specs: list[Any] = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name=f"upstream-{cfg.name}", daemon=True
        )
        self._thread.start()
        self._open_checked()

    def _run(self, coro: Any, timeout: float) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _open_checked(self) -> None:
        try:
            self._run(self._open(), UPSTREAM_STARTUP_TIMEOUT_S)
            self.failed = ""
        except Exception as exc:  # noqa: BLE001 - a broken upstream only loses its own tools (RESOLUTIONS E6)
            self.failed = str(exc) or type(exc).__name__
            log.warning("mcp upstream %s failed to start: %s", self.name, self.failed)

    async def _open(self) -> None:
        cfg = self.cfg
        params = StdioServerParameters(
            command=cfg.command,
            args=list(cfg.args),
            env=upstream_env(cfg),
            cwd=cfg.cwd or str(Path.home()),
        )
        stack = AsyncExitStack()
        try:
            client = await stack.enter_async_context(Client(params, read_timeout_seconds=cfg.timeout_s))
            listed = await client.list_tools()
        except BaseException:
            await stack.aclose()
            raise
        self._client, self._stack = client, stack
        self._specs = []
        for tool in listed.tools:
            if (cfg.tools_allow and tool.name not in cfg.tools_allow) or tool.name in cfg.tools_deny:
                continue
            try:
                self._specs.append(
                    provider_tool(
                        cfg.name,
                        tool.name,
                        tool.description or "",
                        dict(tool.input_schema or {}),
                        timeout=cfg.timeout_s,
                    )
                )
            except ValueError as exc:
                log.warning("mcp upstream %s: tool %r skipped: %s", cfg.name, tool.name, exc)

    def tools(self) -> list[Any]:
        """The upstream's tools after `tools_allow` and `tools_deny`; empty while it is down."""
        return list(self._specs)

    def call(
        self, tool: str, args: Mapping[str, object], *, timeout: float, cancel: threading.Event | None = None
    ) -> RunOutcome:
        """Calls `tool` upstream; a crash or timeout is an error outcome, never an exception."""
        if self._client is None and not self._restart():
            return RunOutcome(
                False,
                error_type="worker_failed",
                message=f"mcp server {self.name} is unavailable: {self.failed}",
            )
        assert self._client is not None  # _restart() succeeded
        wait = min(timeout, self.cfg.timeout_s) if timeout > 0 else self.cfg.timeout_s
        try:
            result = self._run(self._client.call_tool(tool, dict(args)), wait + 1)
        except Exception as exc:  # noqa: BLE001 - one failed call must not stop the hub (RESOLUTIONS E6)
            # not retried: the call may already have had effects; the next call restarts the server
            self._client, self.failed = None, str(exc) or type(exc).__name__
            log.warning("mcp upstream %s call %s failed: %s", self.name, tool, self.failed)
            return RunOutcome(
                False, error_type="worker_failed", message=f"mcp server {self.name} failed: {self.failed}"
            )
        text = _render(result)
        if result.is_error:
            return RunOutcome(False, error_type="tool_error", message=text)
        return RunOutcome(True, result=text)

    def _restart(self) -> bool:
        if self._restarts >= self.cfg.restart_limit:
            return False
        self._restarts += 1
        log.info("mcp upstream %s is restarting", self.name)
        self._open_checked()
        return self._client is not None

    def close(self) -> None:
        """Stops the upstream and its loop."""
        if self._stack is not None:
            try:
                self._run(self._stack.aclose(), CLOSE_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 - closing is best effort
                log.info("closing mcp upstream %s: %s", self.name, exc)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(CLOSE_TIMEOUT_S)


def install(configs: Sequence[UpstreamConfig]) -> None:
    """Registers a `halhub.cli` hook that adds one `McpUpstream` per config to each new hub."""
    from halhub import cli

    def hook(hub: Hub) -> None:
        for cfg in configs:
            upstream = McpUpstream(cfg)
            if upstream.failed:
                upstream.close()
                continue
            try:
                hub.add_provider(upstream)
            except ValueError as exc:
                log.warning("mcp upstream %s not added: %s", cfg.name, exc)
                upstream.close()

    cli.HUB_HOOKS.append(hook)
