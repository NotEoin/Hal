"""A hand-rolled ACP agent server: `initialize`, `session/new`, `session/prompt`, `session/cancel`.

Messages are one JSON object per line. The reader thread stays free while a prompt runs on a
worker thread, so `session/cancel` is seen at once. The server never calls `fs/*` or `terminal/*`
client methods: that would let an in-box agent act on the host outside the box (design 03-agent,
3.14.6). A session's `cwd` must lie inside the workspace root. Only mode `auto` exists here;
review mode, `session/load` and `_hal/record` are not built yet.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from halagent import __version__
from halagent.loop import Agent, RunResult
from halagent.provider import ProviderError
from halagent.sessions import SessionError, check_session_id, new_session_id

log = logging.getLogger("halagent.acp")

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 8 * 1024 * 1024
ERR_PARSE, ERR_INVALID, ERR_NOT_FOUND, ERR_PARAMS, ERR_INTERNAL = -32700, -32600, -32601, -32602, -32603
ERR_BUSY = -32002
STOP_REASONS = {
    "done": "end_turn", "finished": "end_turn", "turn_limit": "max_turn_requests",
    "tool_limit": "max_turn_requests", "context_overflow": "max_tokens", "truncated": "max_tokens",
    "cancelled": "cancelled",
}  # fmt: skip
KIND_OF = {
    "read": "read", "grep": "search", "glob": "search", "edit": "edit", "write": "edit",
    "run": "execute", "todo": "think",
}  # fmt: skip
MODES = {"currentModeId": "auto", "availableModes": [
    {"id": "auto", "name": "Auto", "description": "Work freely inside the sandbox"}]}  # fmt: skip

AgentFactory = Callable[[Path, str, Callable[[dict], None]], Agent]


class RpcError(Exception):
    """A JSON-RPC error to send back for one request."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class AcpServer:
    """Serves one client on a pair of text streams until the input closes."""

    def __init__(self, root: Path, factory: AgentFactory, rfile: IO[str], wfile: IO[str]) -> None:
        self.root = root.resolve()
        self.factory = factory
        self.rfile, self.wfile = rfile, wfile
        self.sessions: dict[str, Agent] = {}
        self._running: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []

    def _send(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=True)
        with self._lock:
            self.wfile.write(line + "\n")
            self.wfile.flush()

    def _notify_update(self, sid: str, update: dict) -> None:
        self._send(
            {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": update}}
        )

    def serve(self) -> None:
        """Reads requests until EOF, then waits for running prompts."""
        for raw in self.rfile:
            if len(raw) > MAX_LINE_BYTES:
                self._send(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": ERR_INVALID, "message": "line too long"}}
                )
                continue
            if raw.strip():
                self._handle(raw)
        for t in self._workers:
            t.join()

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
                if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                    return  # a reply to a request we never send
                raise RpcError(ERR_INVALID, "not a JSON-RPC request")
        except ValueError:
            self._send({"jsonrpc": "2.0", "id": None, "error": {"code": ERR_PARSE, "message": "parse error"}})
            return
        except RpcError as exc:
            self._send({"jsonrpc": "2.0", "id": None, "error": {"code": exc.code, "message": exc.message}})
            return
        method, rid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        if method == "session/prompt" and rid is not None:
            self._start_prompt(rid, params)
            return
        try:
            result = self._dispatch(method, params)
        except RpcError as exc:
            if rid is not None:
                err = {"code": exc.code, "message": exc.message, **({"data": exc.data} if exc.data else {})}
                self._send({"jsonrpc": "2.0", "id": rid, "error": err})
            return
        if rid is not None:
            self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return self._initialize()
        if method == "session/new":
            return self._new_session(params)
        if method == "session/cancel":
            agent = self.sessions.get(str(params.get("sessionId")))
            if agent:
                agent.cancel.set()
            return {}
        if method == "session/set_mode":
            if params.get("modeId") != "auto":
                raise RpcError(ERR_PARAMS, f"unknown mode {params.get('modeId')!r}; only 'auto' exists")
            return {}
        raise RpcError(ERR_NOT_FOUND, f"method not found: {method}")

    @staticmethod
    def _initialize() -> dict:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
                "mcpCapabilities": {"http": False, "sse": False},
                "_meta": {"hal": {"records": False, "version": __version__}},
            },
            "agentInfo": {"name": "hal-agent", "title": "Hal", "version": __version__},
            "authMethods": [],
        }

    def _new_session(self, params: dict) -> dict:
        cwd = Path(str(params.get("cwd") or self.root)).resolve()
        if cwd != self.root and self.root not in cwd.parents:
            raise RpcError(ERR_PARAMS, f"cwd {cwd} is outside the workspace {self.root}")
        hint = ((params.get("_meta") or {}).get("hal") or {}).get("sessionId")
        try:
            sid = check_session_id(hint) if hint else new_session_id()
        except SessionError as exc:
            raise RpcError(ERR_PARAMS, str(exc)) from exc
        if sid in self.sessions:
            raise RpcError(ERR_PARAMS, f"session {sid} already exists")
        self.sessions[sid] = self.factory(cwd, sid, lambda update: self._map_update(sid, update))
        return {"sessionId": sid, "modes": MODES}

    def _map_update(self, sid: str, u: dict) -> None:
        kind = u["kind"]
        if kind == "text":
            self._notify_update(
                sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": u["text"]}}
            )
        elif kind == "tool_call":
            try:
                raw_input = json.loads(u["raw"] or "{}")
            except ValueError:
                raw_input = {"raw": u["raw"]}
            self._notify_update(sid, {"sessionUpdate": "tool_call", "toolCallId": u["id"], "title": u["name"],
                                      "kind": KIND_OF.get(u["name"], "other"), "status": "pending",
                                      "rawInput": raw_input})  # fmt: skip
        elif kind == "tool_result":
            body = {"type": "content", "content": {"type": "text", "text": u["text"]}}
            status = "failed" if u["failed"] else "completed"
            self._notify_update(
                sid,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": u["id"],
                    "status": status,
                    "content": [body],
                },
            )
        elif kind == "plan":
            entries = [
                {"content": ln[6:].strip(), "priority": "medium",
                 "status": {"[x]": "completed", "[~]": "in_progress"}.get(ln[2:5], "pending")}
                for ln in u["text"].splitlines()[1:] if ln.startswith("- [")
            ]  # fmt: skip
            self._notify_update(sid, {"sessionUpdate": "plan", "entries": entries})

    def _start_prompt(self, rid: object, params: dict) -> None:
        sid = str(params.get("sessionId"))
        agent = self.sessions.get(sid)
        if agent is None:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": ERR_PARAMS, "message": f"unknown session {sid}"},
                }
            )
            return
        text = "\n".join(
            str(b.get("text", ""))
            for b in params.get("prompt") or []
            if isinstance(b, dict) and b.get("type") == "text"
        )
        if not text.strip():
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": ERR_PARAMS, "message": "the prompt has no text"},
                }
            )
            return
        if sid in self._running and self._running[sid].is_alive():
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": ERR_BUSY, "message": "a prompt is already running"},
                }
            )
            return
        worker = threading.Thread(target=self._run_prompt, args=(rid, sid, agent, text), daemon=True)
        self._running[sid] = worker
        self._workers.append(worker)
        worker.start()

    def _run_prompt(self, rid: object, sid: str, agent: Agent, text: str) -> None:
        try:
            result: RunResult = agent.run(text)
        except ProviderError as exc:
            data = {"hal": {"stopReason": "error", "message": str(exc)}}
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": ERR_INTERNAL, "message": str(exc), "data": data},
                }
            )
            return
        except Exception as exc:  # noqa: BLE001 - one failed turn must answer the request, not kill the server
            log.exception("prompt failed")
            data = {"hal": {"stopReason": "error", "message": str(exc)}}
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": ERR_INTERNAL, "message": "internal error", "data": data},
                }
            )
            return
        reason = STOP_REASONS.get(result.stopped, "end_turn")
        out: dict[str, Any] = {"stopReason": reason}
        if reason != "end_turn" and reason != "cancelled":
            out["_meta"] = {"hal": {"stopReason": result.stopped}}
        if result.stopped == "finished":
            self._notify_update(
                sid,
                {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": result.text}},
            )
        self._send({"jsonrpc": "2.0", "id": rid, "result": out})
