"""The hub's MCP server on the official SDK (04-hub.md, "MCP server").

`build_server(hub, options)` returns a low-level `mcp.server.lowlevel.server.Server` whose
`tools/list` and `tools/call` handlers run the hub's one call pipeline (`Hub.call` with
`front_end="mcp"`). `serve_streams` runs it on any pair of SDK streams (the broker's unix
socket, an in-memory test transport); `serve_stdio` runs it on this process's stdin and
stdout for `hal-hub mcp`; `serve_http` serves streamable HTTP on loopback behind a bearer
token for `hal-hub mcp --transport http`.

    {"mcpServers": {"hal-hub": {"command": "hal-hub", "args": ["mcp"]}}}

Design decisions:

- The low-level `Server`, not `MCPServer`: tool schemas come from manifests at run time, and
  `MCPServer` builds them from Python signatures (Decision D2). The SDK serves both the
  `initialize` era (2024-11-05 to 2025-11-25) and the 2026-07-28 per-request envelope; the
  first request of a connection decides which (REQ-HUB-36).
- Meta mode (the default, `[hub.mcp].mode`) lists `hal_find_tools`, `hal_call` and
  `hal_modules`, then the `always_offer` tools and `[hub].pinned` the session may use, sorted
  by name (REQ-HUB-48). A call to any name goes through the pipeline, so policy applies to
  unlisted names too (01 REQ-SEC-89). `hal_find_tools` and `hal_modules` are the built-in
  `hub` module's tools, so they are audited and filtered like any other call. An approval for
  `hal_call` binds the inner tool.
- Flat mode lists every tool the session may use, sorted by name, so a harness can apply its
  own per-tool rules; a call to a name it did not list (`hal_call` included) is
  `unknown_tool` before the pipeline (REQ-HUB-38). Above `FLAT_WARN_TOOLS` it logs a warning.
- `packs` and `tools` narrow what a server lists and runs: a tool passes when every given
  limit admits it. Policy stays the boundary; the limits only shrink the surface. In meta
  mode the three meta tools stay, and `hal_find_tools` and `hal_modules` results are cut to
  the admitted tools.
- Annotations come from the tier by 01's table ("MCP annotations") and are display only;
  nothing in Hal reads them back (REQ-HUB-39). Each listed tool also carries `_meta`
  `hal/tier` and `hal/approval` (`ask` or `none`, the listing verdict).
- One `HubSession` per connection, opened in the server's lifespan (which the SDK enters
  once per connection in both eras): kind `hub`, origin `host_mcp`. Its approver depends on
  the approval mode (`[hub.mcp].approvals`, REQ-HUB-40 to REQ-HUB-42): `auto` offers the
  `elicitation` surface when the call's client declared form elicitation, plus this
  connection's control socket (`hal approve ID`); `elicit` only the first; `out-of-band` only
  the second; `deny` none, so every `ask` is `no-approver`; `callback` (the broker) uses the
  caller's approver or session and never elicits, because an in-box client could answer its
  own dialog. The control socket is bound on the first request only. 01's `notify` surface
  joins `auto` and `out-of-band` with 01's phase 2.
- Elicitation, handshake era: the router thread sends `elicitation/create` on the event loop
  (`anyio.from_thread` with the loop's token) and the call finishes in the same request; the
  control socket stays open in parallel and the first valid answer wins.
- Elicitation, 2026-07-28: the call runs on in its own thread, blocked in the router, and the
  first `tools/call` returns `input_required` with `requestState = "hal-approval:" + id`.
  The retry re-runs steps 1 to 5 on its own arguments (`Hub.approval_key`); another key is
  audited `approval_mismatch` and refused as `approval_declined`, and the request stays
  pending for the control socket. The state carries no authority: it only names a request of
  this connection, so an unknown or expired id starts a fresh call. A matching retry whose
  request another surface already decided gets that call's result instead of a second
  prompt, so an approval given with `hal approve` is not lost. The SDK's `requestState`
  sealing is not installed, because its request binding would refuse a changed retry before
  it could be audited as a mismatch.
- While a call runs or waits, `notifications/progress` goes out every `PROGRESS_S` when the
  request carried a progress token (REQ-HUB-41).
- Channel `remote` (07-remote's forced command, REQ-HUB-71): a session of kind `remote` on
  01's `remote` channel profile (which only tightens), origin `remote:<client>`, and
  `remote_addr` from `HAL_REMOTE_ADDR` in every audit record. Its approval mode is always
  `auto`: the laptop client's dialog when it has one, else `hal approve` on the desktop. It
  never gets 01's `notify` surface: a desktop notification is not where the person at the
  laptop is, and it cannot show every argument.
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

- Streamable HTTP (REQ-HUB-43 to REQ-HUB-45): the SDK's `streamable_http_app` with sessions
  on (`stateless_http=False`), so handshake-era clients can receive `elicitation/create`,
  under uvicorn on `127.0.0.1` or `::1` only; any other host is a `ConfigError` before
  binding. An ASGI guard runs first, in this order: `Host` must name the loopback address and
  the bound port (403); an `Origin`, when present, must be on the allow list (403); and
  `Authorization` must be `Bearer <token>` under `hmac.compare_digest` (401). The SDK alone
  accepts a missing `Origin`, any `http://localhost:*` origin, and answers a bad `Host` with
  421; its DNS-rebinding check stays on as a second layer. Refusals are audited as
  `listener_auth_failed`, at most `AUTH_FAIL_LIMIT` a minute and then one summary. The token
  is `config_dir()/hub-token`, created 0600 through `halkit.secrets` and refused when others
  can read it.
- The SDK enters the server lifespan once for the whole HTTP listener, not per client, so
  every HTTP client shares one session and one control socket. They all hold the same
  user's token, and the 2026-07-28 envelope gives no stable client identity to split them by.
"""

from __future__ import annotations

import contextvars
import dataclasses
import hmac
import json
import logging
import re
import socket
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from halkit import paths
from halkit import secrets as halsecrets
from halkit.config import ConfigError

from . import policy
from .approvals import ApprovalRequest, ApprovalRouter, Approver, Decision, control
from .audit import AuditLog, AuditUnavailable
from .hub import CallResult, Hub, new_call_id
from .manifest import ToolSpec
from .session import HubSession, SessionSink, new_session_id
from .surfaces import (
    ELICITATION_KEY,
    ElicitationSurface,
    elicitation_message,
    elicitation_meta,
    elicitation_schema,
)

if TYPE_CHECKING:  # the SDK is imported lazily: only `hal-hub mcp` and the broker need it
    import mcp_types as types
    from mcp.server.lowlevel.server import Server

log = logging.getLogger(__name__)

__all__ = [
    "META_TOOLS",
    "ServeOptions",
    "annotations",
    "build_server",
    "check_http_host",
    "load_token",
    "render",
    "rotate_token",
    "serve_http",
    "serve_stdio",
    "serve_streams",
    "token_path",
]

ORIGIN = "host_mcp"  # 01 `Origin.HOST_MCP`: a harness on the host speaking MCP to the hub
REMOTE_CHANNEL = "remote"
REMOTE_ORIGIN_RE = re.compile(r"^remote:[a-z0-9][a-z0-9-]{0,31}$")  # 04-hub "Remote sessions"
UNAVAILABLE_ERRORS = frozenset({"unknown_tool", "unavailable"})  # 02's `_meta.hal` status `unavailable`
ApprovalMode = Literal["auto", "elicit", "out-of-band", "callback", "deny"]
APPROVAL_MODES: tuple[str, ...] = get_args(ApprovalMode)
BOX_ORIGINS = frozenset({"box_harness", "box_agent"})  # never elicitation: an in-box client could answer
PROGRESS_S = 15.0  # 04-hub REQ-HUB-41: some clients extend their tool timeout on progress
WAIT_POLL_S = 0.5  # a backstop only: the event wakes the waiter as soon as anything changes
RESULT_KEEP_S = 300.0  # an answered 2026-07-28 request waits this long for the client's retry
STATE_PREFIX = "hal-approval:"  # `requestState` = prefix + approval id; it carries no authority
CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"  # 2026-07-28 per-request `_meta` key
HTTP_HOSTS = ("127.0.0.1", "::1")  # loopback only (REQ-HUB-43, 00 REQ-SYS-09)
HTTP_PORT = 8742  # 00-overview's ports table (RESOLUTIONS D14)
HTTP_PATH = "/mcp"
TOKEN_FILE = "hub-token"  # under config_dir()
LISTENER = "hub-http"  # the `listener` field of `listener_auth_failed`
AUTH_FAIL_LIMIT = 10  # 01 audit schema: at most 10 `listener_auth_failed` a minute per listener
AUTH_FAIL_WINDOW_S = 60.0
LISTEN_BACKLOG = 64  # connections the kernel queues while uvicorn starts
FLAT_WARN_TOOLS = 100  # some clients cap the tool count (04-hub "Modes"; the cap differs per client)
INSTRUCTIONS_FLAT = "Hal's tools for this computer and its owner."
INSTRUCTIONS = (
    INSTRUCTIONS_FLAT + " Search with hal_find_tools before saying something cannot be done. "
    "When a tool needs approval, Hal asks the user directly; wait for the result."
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
# 04-hub "Annotations": the search and listing tools are read-only, `hal_call` may run anything
_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
META_ANNOTATIONS: dict[str, dict[str, bool]] = {
    "hal_find_tools": _READ_ONLY,
    "hal_modules": _READ_ONLY,
    "hal_call": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
}


@dataclass(frozen=True)
class ServeOptions:
    """How one server serves: listing mode, limits, and the policy profile or the caller's session.

    Raises `ValueError` for an unknown mode or an empty limit.
    """

    mode: Literal["meta", "flat"] = "meta"
    profile: str | None = None  # None: the policy's default profile (01-security precedence)
    channel: Literal["remote"] | None = None  # "remote": a laptop over SSH (07-remote)
    origin: str = ORIGIN  # or "remote:<client>" with channel "remote"
    remote_addr: str | None = None  # from HAL_REMOTE_ADDR, recorded in every audit record
    approvals: ApprovalMode = "auto"
    approver: Approver | None = None  # 01 protocol; required for "callback" without a session
    session: HubSession | None = None  # the broker passes its own (mode "callback")
    packs: frozenset[str] | None = None  # limit to these packs
    tools: frozenset[str] | None = None  # limit to these tools
    server_name: str = "hal-hub"

    def __post_init__(self) -> None:
        if self.mode not in ("meta", "flat"):
            raise ValueError(f"unknown MCP mode {self.mode!r}; expected meta or flat")
        if self.approvals not in APPROVAL_MODES:
            raise ValueError(
                f"unknown approval mode {self.approvals!r}; expected {', '.join(APPROVAL_MODES)}"
            )
        if self.approvals == "callback" and self.approver is None and self.session is None:
            raise ValueError("approval mode callback needs the caller's approver or session")
        if self.approvals != "callback" and (self.approver is not None or self.session is not None):
            raise ValueError("an approver or session of the caller's own needs approval mode callback")
        if self.channel not in (None, REMOTE_CHANNEL):
            raise ValueError(f"unknown channel {self.channel!r}; hal-hub mcp serves only channel remote")
        if self.origin != ORIGIN and not REMOTE_ORIGIN_RE.fullmatch(self.origin):
            raise ValueError(f"origin {self.origin!r} must be remote:<client> ({REMOTE_ORIGIN_RE.pattern})")
        if self.channel != REMOTE_CHANNEL and (self.origin != ORIGIN or self.remote_addr is not None):
            raise ValueError("a remote origin or address needs channel remote")
        if self.channel == REMOTE_CHANNEL and self.approvals != "auto":
            raise ValueError("channel remote always uses approval mode auto (REQ-HUB-71)")
        for name, limit in (("packs", self.packs), ("tools", self.tools)):
            if limit is not None and not limit:
                raise ValueError(f"an empty {name} limit would serve nothing; leave it unset instead")

    def admits(self, name: str, pack: str) -> bool:
        """True when every given limit admits the tool `name` of pack `pack`."""
        return (self.packs is None or pack in self.packs) and (self.tools is None or name in self.tools)


@dataclass
class _Parked:
    """A 2026-07-28 call whose approval went back to the client as `input_required`.

    The pipeline keeps running in its own thread, blocked in the router, so the control
    socket can answer meanwhile; the client's retry either delivers the form answer or picks
    up the result of a call another surface already decided.
    """

    request: ApprovalRequest | None = None
    answer: Mapping[str, object] | None = None
    result: CallResult | None = None
    finished_at: float = 0.0
    changed: threading.Event = field(default_factory=threading.Event)  # request set, or result
    answered: threading.Event = field(default_factory=threading.Event)  # answer, or withdrawal

    def ask(self, request: ApprovalRequest, timeout_s: float) -> Mapping[str, object] | None:
        """The elicitation surface's `send`: publish the request, then wait for the retry's answer."""
        self.request = request
        self.changed.set()
        self.answered.wait(timeout_s)
        return self.answer

    def deliver(self, answer: Mapping[str, object] | None) -> None:
        """The retry's form answer; a retry without one answers `cancel`, which is a deny."""
        self.answer = answer if answer is not None else {"action": "cancel"}
        self.answered.set()

    def stop(self) -> None:
        """No answer will come (withdrawn, or the connection closed): `ask` returns None."""
        self.answered.set()

    def finish(self, result: CallResult) -> None:
        self.result = result
        self.finished_at = time.monotonic()
        self.changed.set()
        self.answered.set()


@dataclass(frozen=True)
class _Channel:
    """How the current `tools/call` can reach its client: era, elicitation and the SDK context."""

    modern: bool
    elicits: bool
    ctx: Any = None
    token: Any = None  # the event loop's token, for a router thread that sends `elicitation/create`
    parked: _Parked | None = None
    scopes: dict[str, Any] = field(default_factory=dict)  # legacy: pending elicitations by id

    def surface(self) -> ElicitationSurface:
        """An elicitation surface over this call's transport."""
        if self.modern:
            assert self.parked is not None
            return ElicitationSurface(self.parked.ask, cancel=lambda _id: self.parked and self.parked.stop())
        return ElicitationSurface(self._legacy_send, cancel=self._legacy_cancel)

    def _legacy_send(self, request: ApprovalRequest, timeout_s: float) -> Mapping[str, object] | None:
        import anyio.from_thread

        return anyio.from_thread.run(self._elicit, request, timeout_s, token=self.token)

    async def _elicit(self, request: ApprovalRequest, timeout_s: float) -> Mapping[str, object] | None:
        import anyio
        import mcp_types as types
        from mcp.shared.message import ServerMessageMetadata

        message = types.ElicitRequest(
            params=types.ElicitRequestFormParams(
                message=elicitation_message(request),
                requested_schema=elicitation_schema(request),
                meta=elicitation_meta(request),
            )
        )
        with anyio.move_on_after(timeout_s) as scope:
            self.scopes[request.id] = scope
            try:
                result = await self.ctx.session.send_request(
                    message,
                    types.ElicitResult,
                    metadata=ServerMessageMetadata(related_request_id=self.ctx.request_id),
                )
            finally:
                self.scopes.pop(request.id, None)
            return {"action": result.action, "content": result.content}
        return None

    def _legacy_cancel(self, request_id: str) -> None:
        import anyio.from_thread

        scope = self.scopes.get(request_id)
        if scope is not None:
            try:
                anyio.from_thread.run_sync(scope.cancel, token=self.token)
            except RuntimeError:  # the connection's loop is gone; nothing waits any more
                pass


_CHANNEL: contextvars.ContextVar[_Channel | None] = contextvars.ContextVar("halhub_mcp_channel", default=None)


class _ConnectionApprover:
    """The approver of a connection's own session: 01's router over this call's surfaces.

    The surfaces depend on the call (its era, whether its client takes forms), so the router
    is built per request from the `_CHANNEL` of the call being decided.
    """

    name = "router"
    shows_all_args = True

    def __init__(
        self, mode: ApprovalMode, control_server: control.ControlServer, origin: str, audit: SessionSink
    ) -> None:
        self._mode = mode
        self._control = control_server
        self._origin = origin
        self._audit = audit

    @property
    def surfaces(self) -> tuple[Approver, ...]:
        """The surfaces a request of the current call may go to (for the audit record)."""
        return self._surfaces(None)

    def available(self) -> bool:
        return bool(self._surfaces(None))

    def decide(self, request: ApprovalRequest, timeout_s: float) -> Decision:
        surfaces = self._surfaces(request)
        if not surfaces:
            return Decision(request.id, request.key, "deny", None, "no-approver")
        return ApprovalRouter(surfaces, audit=self._audit).decide(request, timeout_s)

    def withdraw(self, request_id: str) -> None:
        """Nothing to do: each request's router withdraws from its own surfaces."""

    def _surfaces(self, request: ApprovalRequest | None) -> tuple[Approver, ...]:
        out: list[Approver] = []
        channel = _CHANNEL.get()
        if self._mode in ("auto", "elicit") and channel is not None and channel.elicits:
            if self._origin not in BOX_ORIGINS:
                surface = channel.surface()
                if request is None or surface.available_for(request):
                    out.append(surface)
        if self._mode in ("auto", "out-of-band"):
            out.append(self._control)
        return tuple(out)


@dataclass
class _Connection:
    """One connection's session, the control socket it owns, and its parked 2026-07-28 calls."""

    session: HubSession
    approvals: ApprovalMode = "callback"
    control: control.ControlServer | None = None
    owned: bool = False
    calls: set[threading.Event] = field(default_factory=set)
    parked: dict[str, _Parked] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def park(self, parked: _Parked) -> None:
        assert parked.request is not None
        now = time.monotonic()
        with self.lock:
            for key in [
                k
                for k, v in self.parked.items()
                if v.result is not None and now - v.finished_at > RESULT_KEEP_S
            ]:
                del self.parked[key]
            self.parked[parked.request.id] = parked

    def take(self, state: str | None) -> _Parked | None:
        if not state or not state.startswith(STATE_PREFIX):
            return None
        with self.lock:
            return self.parked.pop(state[len(STATE_PREFIX) :], None)

    def close(self) -> None:
        for cancel in list(self.calls):
            cancel.set()
        for parked in list(self.parked.values()):
            parked.stop()
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
        listing = _listing(hub, connection.session, opts)
        return types.ListToolsResult(tools=[_tool(entry) for entry in listing])

    async def call_tool(
        ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        connection: _Connection = ctx.lifespan_context
        result = await _call(hub, connection, opts, ctx, params)
        return render(result) if isinstance(result, CallResult) else result

    return Server(
        opts.server_name,
        version=__version__,
        instructions=INSTRUCTIONS if opts.mode == "meta" else INSTRUCTIONS_FLAT,
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


def serve_http(
    hub: Hub,
    options: ServeOptions | None = None,
    *,
    host: str = HTTP_HOSTS[0],
    port: int = HTTP_PORT,
    token: str,
    allow_origins: Sequence[str] = (),
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """Serves streamable HTTP at `http://<host>:<port>/mcp` until interrupted.

    Raises `ConfigError` for a host that is not loopback and `OSError` when the port is
    taken. `port=0` picks a free port; `on_ready` receives the URL once the socket listens.
    """
    import uvicorn

    check_http_host(host)
    if not token:
        raise ValueError("the HTTP transport needs a bearer token")
    server = build_server(hub, options)
    app = server.streamable_http_app(
        streamable_http_path=HTTP_PATH, json_response=False, stateless_http=False, host=host
    )
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(LISTEN_BACKLOG)
        bound = listener.getsockname()[1]
        audit = AuditLog(settings=policy.load_policy(None).audit, secrets=hub.known_secrets)
        guarded = _Guard(app, port=bound, token=token, allow_origins=allow_origins, audit=audit)
        config = uvicorn.Config(guarded, lifespan="on", access_log=False, log_config=None)
        url = f"http://{'[' + host + ']' if ':' in host else host}:{bound}{HTTP_PATH}"
        if on_ready is not None:
            on_ready(url)
        uvicorn.Server(config).run(sockets=[listener])
    finally:
        listener.close()


def check_http_host(host: str) -> None:
    """Raises `ConfigError` unless `host` is `127.0.0.1` or `::1` (REQ-HUB-43)."""
    if host not in HTTP_HOSTS:
        raise ConfigError(
            f"the hub's HTTP transport binds only {' or '.join(HTTP_HOSTS)}, not {host!r}; "
            "remote use goes through hal remote (SSH)",
            path=paths.config_file(),
            table="hub.mcp",
            key="http_host",
        )


def token_path() -> Path:
    """Where the HTTP bearer token lives: `config_dir()/hub-token`."""
    return paths.config_dir() / TOKEN_FILE


def load_token(path: Path | None = None) -> str:
    """The HTTP token, created (mode 0600) when missing; `ConfigError` when others can read it."""
    where = path or token_path()
    try:
        return halsecrets.generate_token(where)
    except halsecrets.UnsafeSecretError as exc:
        raise ConfigError(str(exc), path=where) from None


def rotate_token(path: Path | None = None) -> Path:
    """Replaces the HTTP token with a new random one and returns its path; old clients get 401."""
    where = path or token_path()
    where.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fresh = where.with_name(f".{where.name}.new")
    fresh.unlink(missing_ok=True)
    halsecrets.generate_token(fresh)
    fresh.replace(where)
    return where


class _Guard:
    """The ASGI layer in front of the SDK app: Host, then Origin, then the bearer token."""

    def __init__(
        self, app: Any, *, port: int, token: str, allow_origins: Sequence[str], audit: AuditLog
    ) -> None:
        self._app = app
        self._hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        self._origins = frozenset(allow_origins)
        self._expected = f"Bearer {token}".encode()
        self._audit = audit
        self._lock = threading.Lock()
        self._window_start = 0.0
        self._failures = 0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":  # no websockets on this listener
            await _refuse(send, 403, "Forbidden")
            return
        headers = {k.decode("latin-1").lower(): v for k, v in scope.get("headers", [])}
        client = scope.get("client") or ("", 0)
        if headers.get("host", b"").decode("latin-1").lower() not in self._hosts:
            self._failed("host", client[0])
            await _refuse(send, 403, "Forbidden")
            return
        origin = headers.get("origin")
        if origin is not None and origin.decode("latin-1") not in self._origins:
            self._failed("origin", client[0])
            await _refuse(send, 403, "Forbidden")
            return
        if not hmac.compare_digest(headers.get("authorization", b""), self._expected):
            self._failed("token", client[0])
            await _refuse(send, 401, "Unauthorized", [(b"www-authenticate", b"Bearer")])
            return
        await self._app(scope, receive, send)

    def _failed(self, reason: str, remote: str) -> None:
        """Audits a refusal, at most `AUTH_FAIL_LIMIT` a minute and then one summary."""
        now = time.monotonic()
        with self._lock:
            if now - self._window_start > AUTH_FAIL_WINDOW_S:
                self._window_start, self._failures = now, 0
            self._failures += 1
            count = self._failures
        fields: dict[str, object] = {"listener": LISTENER, "reason": reason, "remote_addr": remote}
        if count > AUTH_FAIL_LIMIT + 1:
            return
        if count == AUTH_FAIL_LIMIT + 1:
            fields["summary"] = f"more than {AUTH_FAIL_LIMIT} refusals this minute; the rest are not recorded"
        log.warning("refused an HTTP request (%s) from %s", reason, remote)
        try:
            self._audit.append("listener_auth_failed", origin=ORIGIN, **fields)
        except AuditUnavailable as exc:  # the refusal stands; the gap shows in `seq`
            log.error("listener_auth_failed record failed: %s", exc)


async def _refuse(send: Any, status: int, text: str, extra: Sequence[tuple[bytes, bytes]] = ()) -> None:
    body = f"{status} {text}\n".encode()
    headers = [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": [*headers, *extra]})
    await send({"type": "http.response.body", "body": body})


def annotations(meta: policy.ToolMeta) -> dict[str, bool]:
    """The MCP annotations for a tool, from its tier by 01-security's "MCP annotations" table.

    Display only: no decision in Hal reads them (REQ-HUB-39, 01 REQ-SEC-87).
    """
    tier = policy.Tier(meta.tier)
    read = tier is policy.Tier.READ
    destructive = {
        policy.Tier.READ: False,
        policy.Tier.WRITE: False,
        policy.Tier.ACT: not meta.reversible,
        policy.Tier.SEND: True,
        policy.Tier.SYSTEM: True,
    }[tier]
    open_world = True if tier is policy.Tier.SEND else meta.network
    return {
        "readOnlyHint": read,
        "destructiveHint": destructive,
        "idempotentHint": read,
        "openWorldHint": open_world,
    }


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
    kind = "remote" if opts.channel == REMOTE_CHANNEL else "hub"
    where = {"origin": opts.origin, "channel": opts.channel, "remote_addr": opts.remote_addr}
    if opts.approvals == "callback":
        session = hub.open_session(kind=kind, profile=opts.profile, approver=opts.approver, **where)
        return _Connection(session, owned=True)
    session_id = new_session_id()
    audit = SessionSink()
    server = control.ControlServer(session_id, audit=audit, on_pending=_waiting)
    approver = None
    if opts.approvals != "deny":
        approver = _ConnectionApprover(opts.approvals, server, opts.origin, audit)
    try:
        session = hub.open_session(
            kind=kind, profile=opts.profile, approver=approver, session_id=session_id, **where
        )
    except BaseException:
        server.close()
        raise
    audit.session = session
    return _Connection(session, opts.approvals, server, owned=True)


def _waiting(request: ApprovalRequest, path: Path) -> None:
    """Says on stderr how to answer a request that waits on the control socket."""
    log.warning("%s Waiting for approval: run `hal approve %s` (or deny it).", request.summary, request.id)


def _listing(hub: Hub, session: HubSession, opts: ServeOptions) -> list[dict[str, Any]]:
    """What `tools/list` shows in `opts.mode` (see the header), in the stable order of REQ-HUB-48."""
    usable = [t for t in hub.tools(session) if opts.admits(t.name, t.pack)]
    if opts.mode == "flat":
        if len(usable) > FLAT_WARN_TOOLS:
            log.warning("flat mode lists %d tools; some clients cap the tool count", len(usable))
        return [_entry(hub, session, t) for t in usable]
    by_name = {t.name: t for t in usable}
    offered = {name for name, t in by_name.items() if t.always_offer}
    offered |= {name for name in hub.config.pinned if name in by_name}
    meta = []
    for template in META_TOOLS:
        entry = dict(template, annotations=META_ANNOTATIONS[template["name"]])
        builtin = by_name.get(template["name"]) or hub.registry.tool(template["name"])
        if builtin is not None:  # the two built-in tools carry `_meta` like any listed tool
            entry["_meta"] = _hal_meta(hub, session, builtin)
        meta.append(entry)
    return meta + [_entry(hub, session, by_name[name]) for name in sorted(offered - META_NAMES)]


def _entry(hub: Hub, session: HubSession, tool: ToolSpec) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema(),
        "annotations": annotations(hub.tool_meta(tool)),
        "_meta": _hal_meta(hub, session, tool),
    }
    if tool.title:
        entry["title"] = tool.title
    return entry


def _hal_meta(hub: Hub, session: HubSession, tool: ToolSpec) -> dict[str, str]:
    resolved = hub.listed(tool, session)
    ask = resolved is not None and resolved.verdict is policy.Verdict.ASK
    return {"hal/tier": tool.tier, "hal/approval": "ask" if ask else "none"}


def _tool(entry: Mapping[str, Any]) -> types.Tool:
    import mcp_types as types

    return types.Tool(
        name=entry["name"],
        title=entry.get("title"),
        description=entry["description"],
        input_schema=dict(entry["inputSchema"]),
        annotations=types.ToolAnnotations(**{_SNAKE[k]: v for k, v in entry["annotations"].items()}),
        meta=entry.get("_meta"),
    )


_SNAKE = {
    "readOnlyHint": "read_only_hint",
    "destructiveHint": "destructive_hint",
    "idempotentHint": "idempotent_hint",
    "openWorldHint": "open_world_hint",
}


async def _call(
    hub: Hub,
    connection: _Connection,
    opts: ServeOptions,
    ctx: Any,
    params: types.CallToolRequestParams,
) -> CallResult | types.InputRequiredResult:
    """Runs one `tools/call` through the pipeline; `hal_call` unwraps first."""
    name, arguments = params.name, params.arguments
    if opts.mode == "flat" and name not in {e["name"] for e in _listing(hub, connection.session, opts)}:
        return _unknown(name, "this server did not list it")
    if opts.mode == "meta" and name == "hal_call":
        args = arguments or {}
        tool, inner = args.get("tool"), args.get("arguments")
        if not isinstance(tool, str) or not tool:
            return _invalid("hal_call needs `tool`, the name of a tool from hal_find_tools")
        if inner is not None and not isinstance(inner, Mapping):
            return _invalid(f"hal_call `arguments` for {tool} must be an object")
        name, arguments = tool, inner
    if opts.mode == "meta" and name not in META_NAMES and not _admitted(hub, opts, name):
        return _unknown(name, "this server is limited to other packs or tools")
    channel = _channel(ctx, connection)
    if channel.modern and channel.elicits:
        result = await _call_modern(hub, connection, channel, name, arguments, params)
        if not isinstance(result, CallResult):
            return result
    else:
        result = await _run(hub, connection, channel, name, arguments)
    if opts.mode == "meta" and name in META_NAMES and result.status == "ok":
        return dataclasses.replace(result, result=_narrow(hub, opts, name, result.result))
    return result


def _channel(ctx: Any, connection: _Connection) -> _Channel:
    """The current call's era and whether its client takes elicitation forms (04-hub "Approvals over MCP")."""
    import anyio.lowlevel

    offered = connection.approvals in ("auto", "elicit")
    caps = (ctx.meta or {}).get(CLIENT_CAPS) if ctx is not None else None
    if isinstance(caps, Mapping):  # a 2026-07-28 request: capabilities ride on every request
        elicitation = caps.get("elicitation")
        # a bare `elicitation: {}` counts as form support, as the SDK treats it
        forms = isinstance(elicitation, Mapping) and ("form" in elicitation or not elicitation)
        return _Channel(modern=True, elicits=offered and forms, ctx=ctx)
    elicits = False
    if ctx is not None and offered:
        params = ctx.session.client_params
        elicitation = params.capabilities.elicitation if params is not None else None
        forms = elicitation is not None and (elicitation.form is not None or elicitation.url is None)
        elicits = forms and bool(getattr(ctx.session, "can_send_request", False))
    return _Channel(modern=False, elicits=elicits, ctx=ctx, token=anyio.lowlevel.current_token())


async def _run(
    hub: Hub, connection: _Connection, channel: _Channel, name: str, arguments: Mapping[str, Any] | None
) -> CallResult:
    """Runs the pipeline in a worker thread, with progress notifications while it runs or waits."""
    import anyio

    cancel = threading.Event()
    connection.calls.add(cancel)
    run = partial(hub.call, name, arguments, connection.session, front_end="mcp", cancel=cancel)
    token = _CHANNEL.set(channel)  # the worker thread runs in a copy of this context
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_progress, channel.ctx)
            try:
                return await anyio.to_thread.run_sync(run, abandon_on_cancel=True)
            finally:
                tasks.cancel_scope.cancel()
    except anyio.get_cancelled_exc_class():
        cancel.set()  # stops the worker; the abandoned thread returns on its own
        raise
    finally:
        _CHANNEL.reset(token)
        connection.calls.discard(cancel)


async def _call_modern(
    hub: Hub,
    connection: _Connection,
    channel: _Channel,
    name: str,
    arguments: Mapping[str, Any] | None,
    params: types.CallToolRequestParams,
) -> CallResult | types.InputRequiredResult:
    """A 2026-07-28 call from a client that takes forms: `input_required`, then the retry.

    A retry whose state names a parked request is checked against it by key; anything else
    (no state, an unknown or expired id) starts a fresh call.
    """
    import anyio
    import mcp_types as types

    parked = connection.take(params.request_state)
    if parked is not None:
        assert parked.request is not None
        request = parked.request
        key = await anyio.to_thread.run_sync(hub.approval_key, name, arguments, connection.session)
        if key != request.key:
            connection.park(parked)  # the request stays pending for the control socket
            _mismatch(connection.session, request, key)
            return CallResult(
                "denied",
                new_call_id(),
                name,
                error_type="approval_declined",
                rule="approval-mismatch",
                message="arguments changed after approval",
            )
        parked.deliver(_response(params.input_responses))
        await _wait(channel.ctx, lambda: parked.result is not None, parked.changed)
        assert parked.result is not None
        return parked.result

    parked = _Parked()
    channel = dataclasses.replace(channel, parked=parked)
    cancel = threading.Event()
    connection.calls.add(cancel)
    context = contextvars.copy_context()
    context.run(_CHANNEL.set, channel)

    def run() -> None:
        try:
            parked.finish(hub.call(name, arguments, connection.session, front_end="mcp", cancel=cancel))
        finally:
            connection.calls.discard(cancel)

    threading.Thread(target=context.run, args=(run,), name="hal-mcp-call", daemon=True).start()
    await _wait(channel.ctx, lambda: parked.result is not None or parked.request is not None, parked.changed)
    if parked.request is None:
        assert parked.result is not None
        return parked.result
    connection.park(parked)
    request = parked.request
    elicit = types.ElicitRequest(
        params=types.ElicitRequestFormParams(
            message=elicitation_message(request),
            requested_schema=elicitation_schema(request),
            meta=elicitation_meta(request),
        )
    )
    return types.InputRequiredResult(
        input_requests={ELICITATION_KEY: elicit}, request_state=STATE_PREFIX + request.id
    )


async def _wait(ctx: Any, done: Any, changed: threading.Event) -> None:
    """Waits in a worker thread until `done()`, sending progress notifications meanwhile."""
    import anyio

    def block() -> None:
        while True:
            changed.clear()  # before the check: a change after it sets the event again
            if done():
                return
            changed.wait(WAIT_POLL_S)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(_progress, ctx)
        try:
            await anyio.to_thread.run_sync(block, abandon_on_cancel=True)
        finally:
            tasks.cancel_scope.cancel()


async def _progress(ctx: Any) -> None:
    """`notifications/progress` every `PROGRESS_S` while a call runs or waits; a no-op without a token."""
    import anyio

    if ctx is None:
        return
    ticks = 0
    while True:
        await anyio.sleep(PROGRESS_S)
        ticks += 1
        try:
            await ctx.session.report_progress(ticks)
        except Exception as exc:  # progress is a courtesy; a failure must not end the call
            log.debug("progress notification failed: %s", exc)
            return


def _response(responses: Mapping[str, Any] | None) -> Mapping[str, object] | None:
    """The form answer of a retry's `inputResponses`, as `{"action", "content"}`."""
    answer = (responses or {}).get(ELICITATION_KEY)
    if answer is None:
        return None
    return {"action": getattr(answer, "action", None), "content": getattr(answer, "content", None)}


def _mismatch(session: HubSession, request: ApprovalRequest, key: str | None) -> None:
    """Audits a retry whose arguments give another key (01 REQ-SEC-23)."""
    log.warning("approval %s: the retry's arguments do not match the request", request.id)
    try:
        session.emit(
            "approval_mismatch",
            approval=request.id,
            key_expected=request.key,
            key_got=key,
            source="elicitation",
        )
    except Exception as exc:  # the refusal stands even when its record cannot be written
        log.error("approval_mismatch record failed: %s", exc)


def _admitted(hub: Hub, opts: ServeOptions, name: str) -> bool:
    if opts.packs is None and opts.tools is None:
        return True
    tool = hub.registry.tool(name) or next((t for t in hub.provider_tools() if t.name == name), None)
    return tool is not None and opts.admits(tool.name, tool.pack)


def _narrow(hub: Hub, opts: ServeOptions, name: str, value: object) -> object:
    """A `hal_find_tools` or `hal_modules` result cut to the tools the limits admit."""
    if (opts.packs is None and opts.tools is None) or not isinstance(value, list):
        return value
    if name == "hal_find_tools":
        return [t for t in value if isinstance(t, dict) and opts.admits(str(t["name"]), str(t["pack"]))]
    out = []
    for module in value:
        if not isinstance(module, dict):
            continue
        tools = [t for t in module.get("tools", []) if opts.admits(str(t), str(module["pack"]))]
        if tools:
            out.append(dict(module, tools=tools))
    return out


def _unknown(name: str, why: str) -> CallResult:
    message = f"no tool named {name} on this server ({why})"
    return CallResult("error", new_call_id(), name, error_type="unknown_tool", message=message)


def _invalid(message: str) -> CallResult:
    return CallResult("error", new_call_id(), "hal_call", error_type="invalid_arguments", message=message)


def _json(value: object) -> str:
    # ensure_ascii=False as 04-hub "Results" specifies: the wire is UTF-8 JSON-RPC, and escaping
    # would double the size of non-English results in the model's context
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
