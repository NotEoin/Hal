"""The `hal acp` proxy: what an editor or channel may say to the boxed agent and the reverse.

`ProxyFilter` holds the rules as pure functions of one message so they are testable without
pipes; `Proxy` pumps two transports through it. The agent is untrusted (REQ-CLI-37..39):

- `fs/*` and `terminal/*` requests from the agent never reach the editor: they get -32601, so the
  agent cannot read or run anything on the host through it.
- The client's `fs` and `terminal` capabilities are removed from `initialize`.
- Every `hal/` key under `_meta` is deleted from agent messages and tool titles get `[box] `, so
  an agent cannot pose as a broker approval (REQ-SEC-29). Request ids starting `hal-` are the
  proxy's own and an agent request that uses one is refused.
- A `cwd` outside the project is refused; an assistant channel's `cwd` is replaced by scratch.
- The client's `mcpServers` are dropped: the box reaches host tools only through the broker, and
  an editor-supplied server would be a second, unreviewed way out (deviation from REQ-CLI-98).
- `_hal/record` notifications are stored on the host and not forwarded.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .records import RecordRefused, RecordStore
from .transport import Transport

log = logging.getLogger(__name__)

ERR_INVALID_REQUEST, ERR_METHOD_NOT_FOUND, ERR_INVALID_PARAMS, ERR_INTERNAL = -32600, -32601, -32602, -32603
OWN_ID_PREFIX = "hal-"  # ids of requests the proxy itself sends to the client
META_PREFIX = "hal/"
BOX_PREFIX = "[box] "
HOST_METHOD_PREFIXES = ("fs/", "terminal/")
TITLED_UPDATES = ("tool_call", "tool_call_update")
Message = dict[str, object]


@dataclass
class Routed:
    """What to do with one message: forward it, answer its sender, or both nothing (consumed)."""

    forward: Message | None = None
    reply: Message | None = None


def _error(rid: object, code: int, text: str) -> Message:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": text}}


def strip_hal_meta(value: object) -> object:
    """Returns `value` with every `_meta` key that starts with `hal/` deleted at any depth."""
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            if key == "_meta" and isinstance(item, dict):
                item = {k: v for k, v in item.items() if not str(k).startswith(META_PREFIX)}
            out[key] = strip_hal_meta(item)
        return out
    if isinstance(value, list):
        return [strip_hal_meta(item) for item in value]
    return value


def prompt_text(params: Message) -> str:
    """The text of a `session/prompt`, for the human-message hook."""
    parts: list[str] = []
    blocks = params.get("prompt")
    for block in blocks if isinstance(blocks, list) else []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


class ProxyFilter:
    """The per-message rules of the proxy."""

    def __init__(
        self,
        *,
        project: Path | None = None,
        scratch: Path | None = None,
        on_human_message: Callable[[str], None] | None = None,
        records: RecordStore | None = None,
        on_own_response: Callable[[Message], bool] | None = None,
    ) -> None:
        if (project is None) == (scratch is None):
            raise ValueError("give exactly one of project (code) or scratch (assistant channel)")
        self.project = project.resolve() if project else None
        self.scratch = scratch
        self._human = on_human_message
        self._records = records
        self._own = on_own_response
        self.sessions: set[str] = set()
        self.active: str | None = None

    def to_agent(self, msg: Message) -> Routed:
        """Applies the rules to a message from the editor."""
        method = msg.get("method")
        if method is None:
            if self._own is not None and self._own(msg):
                return Routed()  # an answer to a broker request: the agent never sees it (REQ-SEC-29)
            return Routed(forward=msg)
        params = msg.get("params")
        params = dict(params) if isinstance(params, dict) else {}
        rid = msg.get("id")
        if method == "initialize":
            caps = dict(params.get("clientCapabilities") or {})  # type: ignore[call-overload]
            caps.pop("fs", None)
            caps.pop("terminal", None)
            meta = dict(caps.get("_meta") or {})
            meta["hal"] = {"records": True}
            caps["_meta"] = meta
            params["clientCapabilities"] = caps
        elif method in ("session/new", "session/load"):
            refused = self._place(params, rid)
            if refused is not None:
                return Routed(reply=refused)
            params["mcpServers"] = []
        elif method == "session/prompt":
            self.active = str(params.get("sessionId", "")) or self.active
            if self._human is not None:
                try:
                    self._human(prompt_text(params))
                except (ValueError, OSError) as exc:  # fail loudly: an unhooked prompt is not run
                    return Routed(reply=_error(rid, ERR_INTERNAL, f"human message hook failed: {exc}"))
        return Routed(forward={**msg, "params": params})

    def _place(self, params: Message, rid: object) -> Message | None:
        if self.scratch is not None:
            params["cwd"] = str(self.scratch)
            return None
        cwd = Path(str(params.get("cwd", ""))).resolve()
        assert self.project is not None
        if cwd != self.project and self.project not in cwd.parents:
            return _error(rid, ERR_INVALID_PARAMS, f"cwd {cwd} is outside the project {self.project}")
        return None

    def to_client(self, msg: Message) -> Routed:
        """Applies the rules to a message from the agent."""
        method = msg.get("method")
        rid = msg.get("id")
        if method is None:
            return Routed(forward=strip_hal_meta(msg))  # type: ignore[arg-type]
        if rid is not None and isinstance(rid, str) and rid.startswith(OWN_ID_PREFIX):
            return Routed(reply=_error(rid, ERR_INVALID_REQUEST, f"request ids starting {OWN_ID_PREFIX!r} are reserved"))
        if isinstance(method, str) and method.startswith(HOST_METHOD_PREFIXES):
            if rid is None:
                return Routed()
            return Routed(reply=_error(rid, ERR_METHOD_NOT_FOUND, f"method not found: {method}"))
        params = msg.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "_hal/record":
            self._store(params)
            return Routed()
        out = strip_hal_meta(msg)
        assert isinstance(out, dict)
        self._prefix_title(out)
        return Routed(forward=out)

    def _store(self, params: Message) -> None:
        sid, record = params.get("sessionId"), params.get("record")
        if self._records is None or not isinstance(record, dict) or not isinstance(sid, str):
            return
        try:
            self._records.append(sid, record)
        except RecordRefused as exc:
            log.warning("transcript record refused: %s", exc)

    @staticmethod
    def _prefix_title(out: Message) -> None:
        params = out.get("params")
        if not isinstance(params, dict):
            return
        target = None
        if out.get("method") == "session/request_permission":
            target = params.get("toolCall")
        elif out.get("method") == "session/update":
            update = params.get("update")
            if isinstance(update, dict) and update.get("sessionUpdate") in TITLED_UPDATES:
                target = update
        if isinstance(target, dict) and isinstance(target.get("title"), str):
            if not target["title"].startswith(BOX_PREFIX):
                target["title"] = BOX_PREFIX + target["title"]


class Proxy:
    """Pumps messages between an editor transport and the agent transport through a filter."""

    def __init__(self, editor: Transport, agent: Transport, rules: ProxyFilter) -> None:
        self.editor, self.agent, self.rules = editor, agent, rules

    async def run(self) -> None:
        """Runs until either side closes; raises `AgentCrashed` if the agent broke a limit."""
        tasks = [
            asyncio.ensure_future(self._pump(self.editor, self.agent, self.rules.to_agent)),
            asyncio.ensure_future(self._pump(self.agent, self.editor, self.rules.to_client)),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            task.result()

    async def _pump(self, src: Transport, dst: Transport, rule: Callable[[Message], Routed]) -> None:
        while (msg := await src.receive()) is not None:
            if log.isEnabledFor(logging.DEBUG):
                log.debug("acp %s id=%s", msg.get("method", "response"), msg.get("id"))
            routed = rule(msg)
            if routed.reply is not None:
                await src.send(routed.reply)
            if routed.forward is not None:
                await dst.send(routed.forward)
