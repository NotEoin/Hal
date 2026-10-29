"""The hub's MCP server on the official SDK (04-hub.md, "MCP server").

`build_server(hub, options)` returns a low-level `mcp.server.lowlevel.server.Server` whose
`tools/list` and `tools/call` handlers run the hub's one call pipeline (`Hub.call` with
`front_end="mcp"`). `serve_streams` runs it on any pair of SDK streams (the broker's unix
socket, an in-memory test transport); `serve_stdio` runs it on this process's stdin and
stdout for `hal-hub mcp`.

    {"mcpServers": {"hal-hub": {"command": "hal-hub", "args": ["mcp"]}}}

Design decisions:

- The low-level `Server`, not `MCPServer`: tool schemas come from manifests at run time, and
  `MCPServer` builds them from Python signatures (Decision D2). The SDK serves both the
  `initialize` era (2024-11-05 to 2025-11-25) and the 2026-07-28 per-request envelope; the
  first request of a connection decides which (REQ-HUB-36).
- Meta mode lists `hal_find_tools`, `hal_call` and `hal_modules`, then the `always_offer`
  tools and `[hub].pinned` the session may use, sorted by name (REQ-HUB-48). A call to any
  name goes through the pipeline, so policy applies to unlisted names too (01 REQ-SEC-89).
  `hal_find_tools` and `hal_modules` are the built-in `hub` module's tools, so they are
  audited and filtered like any other call. An approval for `hal_call` binds the inner tool.
- One `HubSession` per connection, opened in the server's lifespan (which the SDK enters
  once per connection in both eras): kind `hub`, origin `host_mcp`, with 01's router over
  this connection's control socket, so an `ask` waits for `hal approve ID` up to
  `[approval].timeout_s` (REQ-HUB-41). The socket is bound on the first request only. A
  caller that passes `ServeOptions.session` (the broker) keeps its own session and approver.
- `Hub.call` blocks (workers, approvals), so it runs in a worker thread. A cancelled request
  sets the call's cancel event, which stops the tool's worker; the thread is not waited for.
- stdio hygiene (REQ-HUB-37): the SDK points fd 1 at stderr while serving, but an unflushed
  `print` still reached the wire in the spec's prototype. `serve_stdio` therefore also sets
  `sys.stdout = sys.stderr` for the whole serve. It does so just after the transport has
  claimed fd 1, not before: the SDK claims fd 1 only when `sys.stdout` is backed by it.
- Results: one `TextContent` (a string as is, anything else as compact JSON); errors set
  `isError` and carry `CallResult.to_dict()` without v1's `error` key, never a traceback
  (REQ-HUB-47); every result carries `_meta.hal` as 02-sandbox defines it (REQ-HUB-46). The
  hub has already scrubbed, cut and, for `untrusted_output` tools, wrapped the text.

Not here yet (later plan steps): flat mode, `packs` and `tools` limits, annotations and
per-tool `_meta` (step 19); elicitation, the `elicit`, `callback` and `deny` approval modes and
progress notifications while waiting (step 20); the remote channel (step 21); HTTP (step 22).
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _control_standin as control
from ._approvals_standin import ApprovalRequest, ApprovalRouter
from .hub import CallResult, Hub, new_call_id
from .manifest import ToolSpec
from .session import HubSession, new_session_id

if TYPE_CHECKING:  # the SDK is imported lazily: only `hal-hub mcp` and the broker need it
    import mcp_types as types
    from mcp.server.lowlevel.server import Server

log = logging.getLogger(__name__)

__all__ = ["META_TOOLS", "ServeOptions", "build_server", "render", "serve_stdio", "serve_streams"]

ORIGIN = "host_mcp"  # 01 `Origin.HOST_MCP`: a harness on the host speaking MCP to the hub
UNAVAILABLE_ERRORS = frozenset({"unknown_tool", "unavailable"})  # 02's `_meta.hal` status `unavailable`
INSTRUCTIONS = (
    "Hal's tools for this computer and its owner. Search with hal_find_tools before saying "
    "something cannot be done. When a tool needs approval, Hal asks the user directly; wait "
    "for the result."
)

# The meta tool schemas of 04-hub.md, "Modes"; `hal_find_tools` and `hal_modules` are the
# built-in module's tools under the same names.
META_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "hal_find_tools",
        "description": (
            "Search Hal's tools for ones that can do what the user asked. Returns names, tiers, "
            "whether approval is needed, and parameters; run one with hal_call."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "what you want to do, in plain words"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "most tools to return (default 8)",
                },
            },
        },
    },
    {
        "name": "hal_call",
        "description": (
            "Run one of Hal's tools by name. Some tools need the user's approval; the user is "
            "asked by Hal, not by you."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["tool"],
            "properties": {
                "tool": {"type": "string", "description": "tool name from hal_find_tools"},
                "arguments": {"type": "object", "description": "the tool's arguments"},
            },
        },
    },
    {
        "name": "hal_modules",
        "description": "List the installed Hal packs and their tool names.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
)
META_NAMES = frozenset(t["name"] for t in META_TOOLS)


@dataclass(frozen=True)
class ServeOptions:
    """How one server serves: the policy profile, or the caller's own session."""

    profile: str | None = None  # None: the policy's default profile (01-security precedence)
    session: HubSession | None = None  # the broker passes its own; else one per connection
    server_name: str = "hal-hub"


@dataclass
class _Connection:
    """One connection's session and the control socket it owns (none for a caller's session)."""

    session: HubSession
    control: control.ControlServer | None = None
    owned: bool = False
    calls: set[threading.Event] = field(default_factory=set)

    def close(self) -> None:
        for cancel in list(self.calls):
            cancel.set()
        if self.control is not None:
            self.control.close()
        if self.owned:
            self.session.close()


def build_server(hub: Hub, options: ServeOptions | None = None) -> Server:
    """An SDK server for `hub`; each connection it serves gets its own session (see the header).

    Raises `halkit.config.ConfigError` on the first connection when the policy file is bad.
    """
    import mcp_types as types
    from mcp.server.lowlevel.server import Server

    from . import __version__

    opts = options or ServeOptions()

    @asynccontextmanager
    async def lifespan(_server: Server) -> AsyncIterator[_Connection]:
        connection = _open(hub, opts)
        try:
            yield connection
        finally:
            connection.close()

    async def list_tools(ctx: Any, _params: Any) -> types.ListToolsResult:
        connection: _Connection = ctx.lifespan_context
        return types.ListToolsResult(tools=[_tool(entry) for entry in _listing(hub, connection.session)])

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        connection: _Connection = ctx.lifespan_context
        result = await _call(hub, connection, params.name, params.arguments)
        return render(result)

    return Server(
        opts.server_name,
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve_streams(hub: Hub, read_stream: Any, write_stream: Any, options: ServeOptions) -> None:
    """Serves one connection on SDK streams until the read side closes (the broker's entry)."""
    server = build_server(hub, options)
    await server.run(read_stream, write_stream, server.create_initialization_options())


def serve_stdio(hub: Hub, options: ServeOptions | None = None) -> None:
    """Serves one connection on this process's stdin and stdout until stdin closes."""
    import anyio
    from mcp.server.stdio import stdio_server

    opts = options or ServeOptions()

    async def run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            saved, sys.stdout = sys.stdout, sys.stderr  # see the header: after the fd 1 claim
            try:
                await serve_streams(hub, read_stream, write_stream, opts)
            finally:
                sys.stdout = saved

    anyio.run(run)


def render(result: CallResult) -> types.CallToolResult:
    """The MCP form of a call's result, with `_meta.hal` (02-sandbox "Broker result metadata")."""
    import mcp_types as types

    if result.status == "ok":
        value = result.result
        text = value if isinstance(value, str) else _json(value)
    else:
        text = _json({k: v for k, v in result.to_dict().items() if k != "error"})
    if result.status == "ok" or result.status == "denied":
        status = result.status
    else:
        status = "unavailable" if result.error_type in UNAVAILABLE_ERRORS else "error"
    hal = {
        "status": status,
        "untrusted": result.untrusted,
        "guard": result.guard if result.untrusted else None,
    }
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        is_error=result.status != "ok",
        meta={"hal": hal},
    )


# -- helpers --


def _open(hub: Hub, opts: ServeOptions) -> _Connection:
    if opts.session is not None:
        return _Connection(opts.session)
    session_id = new_session_id()
    holder: list[HubSession] = []

    def audit(event: str, **fields: object) -> None:
        if holder:
            holder[0].emit(event, **fields)

    server = control.ControlServer(session_id, audit=audit, on_pending=_waiting)
    router = ApprovalRouter([server], audit=audit)
    try:
        session = hub.open_session(
            kind="hub", origin=ORIGIN, profile=opts.profile, approver=router, session_id=session_id
        )
    except BaseException:
        server.close()
        raise
    holder.append(session)
    return _Connection(session, server, owned=True)


def _waiting(request: ApprovalRequest, path: Path) -> None:
    """Says on stderr how to answer a request that waits on the control socket."""
    log.warning("%s Waiting for approval: run `hal approve %s` (or deny it).", request.summary, request.id)


def _listing(hub: Hub, session: HubSession) -> list[dict[str, Any]]:
    """Meta tools first, then the `always_offer` and pinned tools the session may use, by name."""
    usable = {t.name: t for t in hub.tools(session)}
    offered = {name for name, t in usable.items() if t.always_offer}
    offered |= {name for name in hub.config.pinned if name in usable}
    extra = [_entry(usable[name]) for name in sorted(offered - META_NAMES)]
    return [dict(t) for t in META_TOOLS] + extra


def _entry(tool: ToolSpec) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema()}


def _tool(entry: Mapping[str, Any]) -> types.Tool:
    import mcp_types as types

    return types.Tool(
        name=entry["name"],
        description=entry["description"],
        input_schema=dict(entry["inputSchema"]),
    )


async def _call(
    hub: Hub, connection: _Connection, name: str, arguments: Mapping[str, Any] | None
) -> CallResult:
    """Runs one `tools/call` through the pipeline in a worker thread; `hal_call` unwraps first."""
    import anyio

    if name == "hal_call":
        args = arguments or {}
        tool, inner = args.get("tool"), args.get("arguments")
        if not isinstance(tool, str) or not tool:
            return _invalid("hal_call needs `tool`, the name of a tool from hal_find_tools")
        if inner is not None and not isinstance(inner, Mapping):
            return _invalid(f"hal_call `arguments` for {tool} must be an object")
        name, arguments = tool, inner
    cancel = threading.Event()
    connection.calls.add(cancel)
    run = partial(hub.call, name, arguments, connection.session, front_end="mcp", cancel=cancel)
    try:
        return await anyio.to_thread.run_sync(run, abandon_on_cancel=True)
    except anyio.get_cancelled_exc_class():
        cancel.set()  # stops the worker; the abandoned thread returns on its own
        raise
    finally:
        connection.calls.discard(cancel)


def _invalid(message: str) -> CallResult:
    return CallResult("error", new_call_id(), "hal_call", error_type="invalid_arguments", message=message)


def _json(value: object) -> str:
    # ensure_ascii=False as 04-hub "Results" specifies: the wire is UTF-8 JSON-RPC, and escaping
    # would double the size of non-English results in the model's context
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
