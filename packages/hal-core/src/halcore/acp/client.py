"""`AcpClient`: the host's side of one ACP connection to the boxed agent (06-core-cli "ACP client").

It advertises no `fs` and no `terminal` capability, so the agent does its file work inside the
box and a host terminal can never bypass the sandbox. It answers any `fs/*` or `terminal/*`
request with -32601 anyway, because the box is untrusted. `prompt` has no timeout (a turn may
run for an hour); every other request uses `REQUEST_TIMEOUT_S`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from .transport import AgentCrashed, Transport

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
REQUEST_TIMEOUT_S = 30.0  # covers a session/load replay of a long transcript [unverified: measure 2,000 messages]
COMPACT_TIMEOUT_S = 300.0  # one summary model call, which can be slow on a local model
ERR_METHOD_NOT_FOUND = -32601
Message = dict[str, object]


class AcpError(Exception):
    """The agent answered a request with a JSON-RPC error."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{message} (code {code})")
        self.code = code


class ClientHandler(Protocol):
    """What the host does with the agent's notifications and permission requests."""

    async def on_update(self, session_id: str, update: Message) -> None:
        """A `session/update` notification."""
        ...

    async def on_record(self, session_id: str, record: Message) -> None:
        """A `_hal/record` transcript record."""
        ...

    async def request_permission(
        self, session_id: str, tool_call: Message, options: list[Message]
    ) -> Message:
        """The agent's own permission request; returns the ACP `outcome` object."""
        ...


class AcpClient:
    """Sends requests to the agent and routes what it sends back to a `ClientHandler`."""

    def __init__(
        self, transport: Transport, handler: ClientHandler, *, request_timeout_s: float = REQUEST_TIMEOUT_S
    ) -> None:
        self._t = transport
        self._h = handler
        self._timeout = request_timeout_s
        self._next = 0
        self._pending: dict[int, asyncio.Future[Message]] = {}
        self._reader: asyncio.Task[None] | None = None
        self.crash: AgentCrashed | None = None

    def start(self) -> None:
        """Starts the reader task; `initialize` calls it."""
        if self._reader is None:
            self._reader = asyncio.ensure_future(self._read_loop())

    async def _call(self, method: str, params: Message, *, timeout: float | None) -> Message:
        self.start()
        if self.crash is not None:
            raise self.crash
        self._next += 1
        rid = self._next
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self._t.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            reply = await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise AcpError(-32000, f"{method} timed out after {timeout:.0f} s") from None
        finally:
            self._pending.pop(rid, None)
        error = reply.get("error")
        if isinstance(error, dict):
            raise AcpError(int(error.get("code", -32603)), str(error.get("message", "")))  # type: ignore[call-overload]
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    async def initialize(self) -> Message:
        """Negotiates protocol version 1; asks for `_hal/record` transcript records."""
        caps: Message = {"_meta": {"hal": {"records": True}}}
        return await self._call(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": caps},
            timeout=self._timeout,
        )

    async def new_session(self, cwd: str, mcp_servers: list[Message]) -> str:
        """Opens a conversation and returns its session id."""
        result = await self._call(
            "session/new", {"cwd": cwd, "mcpServers": mcp_servers}, timeout=self._timeout
        )
        return str(result["sessionId"])

    async def load_session(self, session_id: str, cwd: str, records: list[Message]) -> None:
        """Resumes `session_id` from the host's transcript `records`."""
        params: Message = {
            "sessionId": session_id,
            "cwd": cwd,
            "mcpServers": [],
            "_meta": {"hal": {"records": records}},
        }
        await self._call("session/load", params, timeout=self._timeout)

    async def prompt(self, session_id: str, blocks: list[Message]) -> str:
        """Runs one turn and returns the ACP stop reason."""
        result = await self._call(
            "session/prompt", {"sessionId": session_id, "prompt": blocks}, timeout=None
        )
        return str(result.get("stopReason", "end_turn"))

    async def cancel(self, session_id: str) -> None:
        """Asks the agent to stop the running turn."""
        await self._t.send(
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}}
        )

    async def set_mode(self, session_id: str, mode_id: str) -> None:
        """Switches the session's permission mode."""
        await self._call(
            "session/set_mode", {"sessionId": session_id, "modeId": mode_id}, timeout=self._timeout
        )

    async def set_model(self, session_id: str, model_id: str) -> None:
        """Switches the model used from the next turn on."""
        await self._call(
            "session/set_model", {"sessionId": session_id, "modelId": model_id}, timeout=self._timeout
        )

    async def set_effort(self, session_id: str, level: str) -> None:
        """Sets the effort level sent with each request from the next turn on; "" sends none."""
        await self._call(
            "session/set_effort", {"sessionId": session_id, "level": level}, timeout=self._timeout
        )

    async def compact(self, session_id: str) -> tuple[int, int]:
        """Summarises the session history; returns the estimated tokens before and after."""
        result = await self._call("session/compact", {"sessionId": session_id}, timeout=COMPACT_TIMEOUT_S)
        hal = ((result.get("_meta") or {}).get("hal")) or {}
        return int(hal.get("beforeTokens", 0)), int(hal.get("afterTokens", 0))

    async def close(self) -> None:
        """Stops the reader and the transport."""
        if self._reader is not None:
            self._reader.cancel()
        await self._t.close()

    async def _read_loop(self) -> None:
        try:
            while (msg := await self._t.receive()) is not None:
                await self._route(msg)
            self.crash = AgentCrashed("agent closed the connection")
        except AgentCrashed as exc:
            self.crash = exc
        for future in self._pending.values():
            if not future.done():
                future.set_exception(self.crash or AgentCrashed("agent connection lost"))

    async def _route(self, msg: Message) -> None:
        method = msg.get("method")
        params = msg.get("params")
        params = params if isinstance(params, dict) else {}
        if method is None:
            rid = msg.get("id")
            future = self._pending.get(rid) if isinstance(rid, int) else None
            if future is not None and not future.done():
                future.set_result(msg)
            return
        sid = str(params.get("sessionId", ""))
        if method == "session/update":
            update = params.get("update")
            if isinstance(update, dict):
                await self._h.on_update(sid, update)
        elif method == "_hal/record":
            record = params.get("record")
            if isinstance(record, dict):
                await self._h.on_record(sid, record)
        elif method == "session/request_permission" and "id" in msg:
            call, options = params.get("toolCall"), params.get("options")
            outcome = await self._h.request_permission(
                sid, call if isinstance(call, dict) else {}, options if isinstance(options, list) else []
            )
            await self._t.send({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": outcome}})
        elif "id" in msg:  # fs/* and terminal/* and anything else: the agent has no host to ask
            error = {"code": ERR_METHOD_NOT_FOUND, "message": f"method not found: {method}"}
            await self._t.send({"jsonrpc": "2.0", "id": msg["id"], "error": error})
