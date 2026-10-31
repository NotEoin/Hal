"""A hand-rolled ACP agent server: `initialize`, `session/new`, `session/prompt`, `session/cancel`.

Messages are one JSON object per line. The reader thread stays free while a prompt runs on a
worker thread, so `session/cancel` is seen at once. The server never calls `fs/*` or `terminal/*`
client methods: that would let an in-box agent act on the host outside the box (design 03-agent,
3.14.6). A session's `cwd` must lie inside the workspace root. Modes are `auto`, `review` and `plan`;
plan mode is read-only: edits and commands are refused until the client leaves it.
Review mode asks the client with `session/request_permission` before an edit or a command.
A client that advertises `clientCapabilities._meta.hal.records` gets every transcript record as a
`_hal/record` notification and the agent writes no file (REQ-AGT-66). `session/load` rebuilds a
session from the records the client sends, else from the local store, and replays it as updates.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from halagent import __version__
from halagent.architect import split_ready
from halagent.loop import Agent, RunResult
from halagent.provider import ProviderError
from halagent.sessions import SessionError, check_session_id, finalize_record, new_session_id, replay, utc_now

log = logging.getLogger("halagent.acp")

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 8 * 1024 * 1024
ERR_PARSE, ERR_INVALID, ERR_NOT_FOUND, ERR_PARAMS, ERR_INTERNAL = -32700, -32600, -32601, -32602, -32603
ERR_BUSY = -32002
STOP_REASONS = {
    "done": "end_turn",
    "finished": "end_turn",
    "context_overflow": "max_tokens",
    "cancelled": "cancelled",
    **dict.fromkeys(
        ("max_model_requests", "max_tool_calls", "max_time", "loop", "tool_errors", "nudge_limit"),
        "max_turn_requests",
    ),
}
KIND_OF = {
    "read": "read", "grep": "search", "glob": "search", "repo_map": "search", "edit": "edit",
    "patch": "edit", "write": "edit",
    "run": "execute", "git": "execute", "todo": "think", "subagent": "think",
}  # fmt: skip
MODE_LIST = [
    {"id": "auto", "name": "Auto", "description": "Work freely inside the sandbox"},
    {"id": "review", "name": "Review", "description": "Ask before every edit and command"},
    {"id": "plan", "name": "Plan", "description": "Read-only until the plan is approved"},
]
PERMISSION_OPTIONS = [
    {"optionId": oid, "name": name, "kind": oid}
    for oid, name in (
        ("allow_once", "Allow"), ("allow_always", "Always allow"),
        ("reject_once", "Reject"), ("reject_always", "Always reject"),
    )
]  # fmt: skip
PERMISSION_POLL_S = 0.2  # how often a waiting call checks for a cancel
PERMISSION_TIMEOUT_S = 3600.0  # an unattended review prompt is treated as cancelled after an hour


def _chunk(kind: str, text: str) -> dict:
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}}


def _modes(current: str) -> dict:
    return {"currentModeId": current, "availableModes": MODE_LIST}


AgentFactory = Callable[..., Agent]  # (cwd, sid, on_update) and, with host records, `session=sink`
RecordLoader = Callable[[str], list[dict]]


class RecordSink:
    """A transcript sink that sends each record to the host as `_hal/record` instead of writing a file."""

    def __init__(self, send: Callable[[dict], None], sid: str, header: dict | None) -> None:
        self._send, self._sid, self._header = send, sid, header

    def write(self, record: dict) -> None:
        """Sends one record; the `session` header goes first, once, for a new session."""
        if self._header is not None:
            header, self._header = self._header, None
            self.write({"type": "session", "v": 1, "created": utc_now(), **header})
        self._send({"jsonrpc": "2.0", "method": "_hal/record",
                    "params": {"sessionId": self._sid, "record": finalize_record(record)}})  # fmt: skip

    def close(self) -> None:
        """Nothing to close: the host owns the file."""


class RpcError(Exception):
    """A JSON-RPC error to send back for one request."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class AcpServer:
    """Serves one client on a pair of text streams until the input closes."""

    def __init__(
        self,
        root: Path,
        factory: AgentFactory,
        rfile: IO[str],
        wfile: IO[str],
        *,
        load_records: RecordLoader | None = None,
        first_session_id: str = "",
        mode: str = "auto",
    ) -> None:
        self.first_session_id = (
            first_session_id  # `agent.json` session_id: the host's id for the first session
        )
        self.default_mode = mode
        self.root = root.resolve()
        self.factory = factory
        self.load_records = load_records
        self.records = False  # the client takes transcript records (set by `initialize`)
        self.rfile, self.wfile = rfile, wfile
        self.sessions: dict[str, Agent] = {}
        self._running: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._pending: dict[str, tuple[threading.Event, list[dict]]] = {}
        self._next_request = 0

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
                    self._reply(msg)
                    return
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
            caps = ((params.get("clientCapabilities") or {}).get("_meta") or {}).get("hal") or {}
            self.records = caps.get("records") is True
            return self._initialize(self.records)
        if method == "session/new":
            return self._new_session(params)
        if method == "session/load":
            return self._load_session(params)
        if method == "session/cancel":
            agent = self.sessions.get(str(params.get("sessionId")))
            if agent:
                agent.cancel.set()
            return {}
        if method == "session/set_mode":
            agent = self.sessions.get(str(params.get("sessionId")))
            mode = params.get("modeId")
            if agent is None or mode not in {m["id"] for m in MODE_LIST}:
                raise RpcError(ERR_PARAMS, f"unknown session or mode {mode!r}; modes: auto, review, plan")
            agent.mode = mode
            return {}
        if method == "session/set_model":
            agent = self.sessions.get(str(params.get("sessionId")))
            if agent is None:
                raise RpcError(ERR_PARAMS, "unknown session")
            busy = self._running.get(str(params.get("sessionId")))
            if busy is not None and busy.is_alive():
                raise RpcError(ERR_PARAMS, "a turn is running: switch the model after it ends")
            try:
                agent.switch_model(str(params.get("modelId", "")))
            except ValueError as exc:
                raise RpcError(ERR_PARAMS, str(exc)) from exc
            return {}
        if method == "session/set_split":
            agent = self.sessions.get(str(params.get("sessionId")))
            if agent is None:
                raise RpcError(ERR_PARAMS, "unknown session")
            enabled = bool(params.get("enabled"))
            if enabled and not split_ready(agent):
                raise RpcError(ERR_PARAMS, "set [agent.models] architect and editor first")
            agent.split = enabled
            return {}
        if method == "session/compact":
            agent = self.sessions.get(str(params.get("sessionId")))
            if agent is None:
                raise RpcError(ERR_PARAMS, "unknown session")
            busy = self._running.get(str(params.get("sessionId")))
            if busy is not None and busy.is_alive():
                raise RpcError(ERR_BUSY, "a turn is running: compact after it ends")
            try:
                before, after = agent.compact(str(params.get("focus") or ""))
            except ValueError as exc:
                raise RpcError(ERR_PARAMS, str(exc)) from exc
            return {"_meta": {"hal": {"beforeTokens": before, "afterTokens": after}}}
        raise RpcError(ERR_NOT_FOUND, f"method not found: {method}")

    @staticmethod
    def _initialize(records: bool) -> dict:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
                "mcpCapabilities": {"http": False, "sse": False},
                "_meta": {"hal": {"records": records, "version": __version__}},
            },
            "agentInfo": {"name": "hal-agent", "title": "Hal", "version": __version__},
            "authMethods": [],
        }

    def _cwd(self, params: dict) -> Path:
        cwd = Path(str(params.get("cwd") or self.root)).resolve()
        if cwd != self.root and self.root not in cwd.parents:
            raise RpcError(ERR_PARAMS, f"cwd {cwd} is outside the workspace {self.root}")
        return cwd

    def _make(self, cwd: Path, sid: str, *, new: bool, mcp_servers: list | None = None) -> Agent:
        """Builds the agent of session `sid`; with host records it gets a `_hal/record` sink."""
        update = lambda u: self._map_update(sid, u)  # noqa: E731 - a one-line closure over `sid`
        extra = {"mcp_servers": mcp_servers} if mcp_servers else {}
        if self.records:
            header = {"id": sid, "agent": f"halagent {__version__}", "mode": "code", "toolset": "coding",
                      "cwd": str(cwd)} if new else None  # fmt: skip
            sink = RecordSink(self._send, sid, header)
            agent = self.factory(cwd, sid, update, session=sink, **extra)
        else:
            agent = self.factory(cwd, sid, update, **extra)
        agent.mode = self.default_mode
        agent.permission = lambda cid, name, kind, args: self._ask(sid, agent, cid, name, kind, args)
        self.sessions[sid] = agent
        return agent

    def _new_session(self, params: dict) -> dict:
        cwd = self._cwd(params)
        hint = ((params.get("_meta") or {}).get("hal") or {}).get("sessionId")
        try:
            sid = (
                check_session_id(hint or self.first_session_id)
                if hint or self.first_session_id
                else new_session_id()
            )
        except SessionError as exc:
            raise RpcError(ERR_PARAMS, str(exc)) from exc
        if sid in self.sessions:
            raise RpcError(ERR_PARAMS, f"session {sid} already exists")
        self.first_session_id = ""  # used once
        listed = params.get("mcpServers")
        agent = self._make(cwd, sid, new=True, mcp_servers=listed if isinstance(listed, list) else None)
        return {"sessionId": sid, "modes": _modes(agent.mode)}

    def _load_session(self, params: dict) -> dict:
        """`session/load`: rebuild from the client's records, else the local store; replay as updates."""
        cwd = self._cwd(params)
        try:
            sid = check_session_id(str(params.get("sessionId") or ""))
        except SessionError as exc:
            raise RpcError(ERR_PARAMS, str(exc)) from exc
        if sid in self.sessions:
            raise RpcError(ERR_PARAMS, f"session {sid} is already loaded")
        sent = ((params.get("_meta") or {}).get("hal") or {}).get("records")
        records = sent if isinstance(sent, list) else (self.load_records(sid) if self.load_records else [])
        if not records:
            raise RpcError(ERR_PARAMS, f"unknown session {sid}")
        try:
            history = replay(records)
            agent = self._make(cwd, sid, new=False)
            agent.resume(records)
        except (KeyError, TypeError, ValueError) as exc:
            self.sessions.pop(sid, None)
            raise RpcError(ERR_PARAMS, f"session {sid} cannot be replayed: {exc}") from exc
        self._replay_updates(sid, history)
        return {}

    def _replay_updates(self, sid: str, history: list[dict]) -> None:
        """Sends the stored conversation as updates: text chunks and finished tool calls."""
        results = {
            m.get("tool_call_id"): str(m.get("content", "")) for m in history if m.get("role") == "tool"
        }
        for msg in history:
            role, text = msg.get("role"), msg.get("content")
            if role == "user" and text:
                self._notify_update(sid, _chunk("user_message_chunk", str(text)))
            elif role == "assistant":
                if text:
                    self._notify_update(sid, _chunk("agent_message_chunk", str(text)))
                for call in msg.get("tool_calls") or []:
                    self._replay_call(sid, call, results)

    def _replay_call(self, sid: str, call: dict, results: dict) -> None:
        fn, cid = call.get("function") or {}, str(call.get("id"))
        name = str(fn.get("name", ""))
        try:
            raw_input = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            raw_input = {"raw": fn.get("arguments")}
        failed = '"status": "error"' in results.get(cid, "")
        self._notify_update(sid, {"sessionUpdate": "tool_call", "toolCallId": cid, "title": name,
                                  "kind": KIND_OF.get(name, "other"), "rawInput": raw_input,
                                  "status": "failed" if failed else "completed"})  # fmt: skip
        if cid in results:
            body = {"type": "content", "content": {"type": "text", "text": results[cid]}}
            status = "failed" if failed else "completed"
            update = {"sessionUpdate": "tool_call_update", "toolCallId": cid, "status": status}
            self._notify_update(sid, {**update, "content": [body]})

    def _reply(self, msg: dict) -> None:
        """Hands the client's answer to the call that waits for it."""
        slot = self._pending.get(str(msg.get("id")))
        if slot:
            slot[1].append(msg)
            slot[0].set()

    def _ask(self, sid: str, agent: Agent, cid: str, name: str, kind: str, args: dict) -> str:
        """Sends `session/request_permission` and waits for the choice; cancelled on cancel or timeout."""
        with self._lock:
            self._next_request += 1
            rid = f"perm{self._next_request}"
        done: tuple[threading.Event, list[dict]] = (threading.Event(), [])
        self._pending[rid] = done
        call = {"toolCallId": cid, "title": name, "kind": KIND_OF.get(name, "other"), "rawInput": args}
        params = {"sessionId": sid, "toolCall": call, "options": PERMISSION_OPTIONS}
        self._send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": params})
        waited = 0.0
        try:
            while not done[0].wait(PERMISSION_POLL_S):
                waited += PERMISSION_POLL_S
                if agent.cancel.is_set() or waited >= PERMISSION_TIMEOUT_S:
                    return "cancelled"
        finally:
            self._pending.pop(rid, None)
        outcome = (done[1][0].get("result") or {}).get("outcome") or {}
        chosen = outcome.get("optionId")
        if outcome.get("outcome") == "selected" and chosen in {o["optionId"] for o in PERMISSION_OPTIONS}:
            return str(chosen)
        return "cancelled" if outcome.get("outcome") == "cancelled" else "reject_once"

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
        elif kind == "usage":
            self._notify_update(
                sid,
                {"sessionUpdate": "usage_update", "used": u["context_used"], "size": u["window"],
                 "_meta": {"hal": {"promptTokens": u["prompt_tokens"],
                                   "completionTokens": u["completion_tokens"],
                                   "tokensPerSecond": u["tokens_per_s"]}}},
            )  # fmt: skip
        elif kind == "compaction":
            self._notify_update(
                sid,
                {"sessionUpdate": "hal_compaction", "reason": u["reason"],
                 "method": u.get("method", "summary"), "beforeTokens": u["before_tokens"],
                 "afterTokens": u["after_tokens"]},
            )  # fmt: skip
        elif kind == "subagent":
            self._notify_update(
                sid,
                {"sessionUpdate": "hal_subagent", "subagentId": u["id"], "agent": u["agent"],
                 "phase": u["phase"], **{k: u[k] for k in ("task", "tool") if k in u}},
            )  # fmt: skip
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
        images = [
            {"mimeType": str(b.get("mimeType")), "data": str(b.get("data"))}
            for b in params.get("prompt") or []
            if isinstance(b, dict) and b.get("type") == "image" and b.get("data") and b.get("mimeType")
        ]
        if images and not agent.accepts_images:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": ERR_PARAMS, "message": "this model does not take image input"},
                }
            )
            return
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
        worker = threading.Thread(target=self._run_prompt, args=(rid, sid, agent, text, images), daemon=True)
        self._running[sid] = worker
        self._workers.append(worker)
        worker.start()

    def _run_prompt(self, rid: object, sid: str, agent: Agent, text: str, images: list[dict]) -> None:
        try:
            result: RunResult = agent.run(text, images)
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
