"""Host tools through the broker: the `ToolBackend` protocol and its only implementation.

Approvals never pass through the agent (REQ-AGT-98): the broker answers a waiting `tools/call`
with the result or with `denied`. The broker's text goes to the model unchanged (REQ-AGT-26);
the outcome status comes from `result._meta.hal.status` (RESOLUTIONS E7), else from `isError`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from halagent.mcp.lite import LiteMcpClient, McpError

log = logging.getLogger("halagent.backends")

BROKER_CALL_TIMEOUT_S = 3_900.0  # longest approval wait (3,600 s) plus 300 s for the tool (03-agent 3.13.2)
FIND_TOOL, CALL_TOOL = "hal_find_tools", "hal_call"
META_TOOLS = frozenset({FIND_TOOL, CALL_TOOL})  # the agent routes these itself
FAILED_NOTE = "This did NOT happen. Fix the arguments and call again, or tell the user it failed."
DENIED_NOTE = "This did NOT happen: it was not approved. Tell the user; do not call it again unless they ask."
Status = Literal["ok", "error", "denied", "unavailable"]
STATUSES = ("ok", "denied", "unavailable")


@dataclass(frozen=True)
class ToolSpec:
    """A host tool as the model sees it."""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass(frozen=True)
class BackendOutcome:
    """The result of one host tool call; `text` is the broker's text, unchanged."""

    status: Status
    text: str
    untrusted: bool = False  # from `_meta.hal.untrusted`, for display only
    guard: str = ""  # from `_meta.hal.guard`, for display only

    def for_model(self) -> str:
        """The text the model reads: pass-through when ok, else with the REQ-AGT-54 note."""
        if self.status == "ok":
            return self.text
        note = DENIED_NOTE if self.status == "denied" else FAILED_NOTE
        return f"{self.text}\n{note}".strip()


class ToolBackend(Protocol):
    """Host tools. The only implementation is `BrokerBackend`; the protocol exists for fakes."""

    def list_tools(self) -> list[ToolSpec]:
        """The tools the broker lists directly."""
        ...

    def find_tools(self, query: str, limit: int) -> list[ToolSpec]:
        """The tools best matching `query`."""
        ...

    def call(self, name: str, args: dict[str, Any]) -> BackendOutcome:
        """Runs one tool."""
        ...

    def close(self) -> None:
        """Releases the connection."""
        ...


def outcome_from_result(result: dict[str, Any]) -> BackendOutcome:
    """Maps an MCP tool result to an outcome; a missing or odd `_meta.hal` falls back to `isError`."""
    text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    meta = (result.get("_meta") or {}).get("hal") or {}
    status = meta.get("status") if isinstance(meta, dict) else None
    if status not in STATUSES:
        status = "error" if (result.get("isError") or status) else "ok"
    return BackendOutcome(
        status,  # type: ignore[arg-type]  # narrowed to the literal values above
        text,
        untrusted=bool(meta.get("untrusted")) if isinstance(meta, dict) else False,
        guard=str(meta.get("guard") or "") if isinstance(meta, dict) else "",
    )


class BrokerBackend:
    """Talks MCP to the hub broker; unlisted tools go through `hal_call`."""

    def __init__(self, socket_path: str, *, timeout_s: float = BROKER_CALL_TIMEOUT_S) -> None:
        self._client = LiteMcpClient(socket_path, timeout_s=timeout_s)
        self._client.initialize()
        self._listed: set[str] = set()

    def list_tools(self) -> list[ToolSpec]:
        """Lists the broker's tools without its two meta tools, which the agent wraps."""
        tools = self._client.list_tools()
        self._listed = {t["name"] for t in tools}
        return [_spec(t) for t in tools if t["name"] not in META_TOOLS]

    def find_tools(self, query: str, limit: int) -> list[ToolSpec]:
        """Asks the broker's `hal_find_tools`; an unusable answer is an empty list."""
        try:
            out = self._client.call_tool_result(FIND_TOOL, {"query": query, "limit": limit})
        except McpError as exc:
            log.warning("hal_find_tools failed: %s", exc)
            return []
        try:
            data = json.loads(outcome_from_result(out).text)
        except ValueError:
            log.warning("hal_find_tools returned text that is not JSON")
            return []
        rows = data.get("tools", []) if isinstance(data, dict) else data
        return [_spec(r) for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)]

    def call(self, name: str, args: dict[str, Any]) -> BackendOutcome:
        """Calls `name` directly when listed, else through `hal_call`; a broken link is `unavailable`."""
        try:
            if name in self._listed:
                result = self._client.call_tool_result(name, args)
            else:
                result = self._client.call_tool_result(CALL_TOOL, {"tool": name, "arguments": args})
        except McpError as exc:
            return BackendOutcome("unavailable", f"the tool broker is not reachable: {exc}")
        return outcome_from_result(result)

    def close(self) -> None:
        """Closes the broker connection."""
        self._client.close()


def _spec(row: dict[str, Any]) -> ToolSpec:
    schema = row.get("inputSchema") or row.get("parameters") or {"type": "object", "properties": {}}
    return ToolSpec(row["name"], str(row.get("description") or ""), dict(schema))
