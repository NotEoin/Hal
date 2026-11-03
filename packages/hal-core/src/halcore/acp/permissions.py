"""Host-originated `session/request_permission` for broker approvals (01-security ACP mapping, REQ-CLI-38).

The hub child holds the pending approvals and serves them on its control socket. The bridge
polls those sockets for requests of this proxy's sessions, shows each one to the editor or
channel with ids `hal-<n>` and `_meta` `hal/origin = "broker"`, and turns the response back into a
control-socket decision. The proxy never forwards these to the agent and never lets the agent
answer them (REQ-SEC-29); an answer to an agent-made request never creates a broker grant.

Design decisions:

- The request `display` is host-made and already escaped (REQ-SEC-25); it travels in `_meta`.
- `session` is offered only when the request's scopes allow it.
- A voice request carries a two-digit challenge in `_meta`; `once` counts only when the response
  echoes the same digits, else the answer is a deny.
- A malformed or unknown response is a deny, never an allow.
- A request that leaves the hub's pending list (answered in another terminal or the console, or
  expired) is dropped here too, so `is_pending` turns false and a late answer is ignored rather
  than sent. A failed listing drops nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from halkit import paths

from halhub.approvals import control

from .proxy import OWN_ID_PREFIX

log = logging.getLogger(__name__)

MAX_ANCESTRY = 16  # process-tree levels walked from the hub child up to `hal`
PEER_TIMEOUT_S = 2.0
PEERCRED_SIZE = struct.calcsize("3i")  # struct ucred: pid, uid, gid (socket(7))
POLL_INTERVAL_S = 0.5  # how often the control sockets are listed for new requests
Message = dict[str, object]

OPTIONS = {
    "once": {"optionId": "once", "name": "Allow once", "kind": "allow_once"},
    "session": {
        "optionId": "session",
        "name": "Allow these exact arguments for this session",
        "kind": "allow_always",
    },
    "deny": {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
}


def build_request(request: Mapping[str, object], rid: str, session_id: str) -> Message:
    """The ACP `session/request_permission` for one `approval_request` wire object."""
    scopes = request.get("scopes")
    offered = ["once"] + (["session"] if isinstance(scopes, list) and "session" in scopes else []) + ["deny"]
    meta: Message = {
        "hal/origin": "broker",
        "hal/approval_id": request.get("id"),
        "hal/key": request.get("key"),
        "hal/display": request.get("display"),
        "hal/summary": request.get("summary"),
    }
    if request.get("challenge"):
        meta["hal/challenge"] = request["challenge"]
    call: Message = {
        "toolCallId": request.get("id"),
        "title": f"{request.get('tool')} [{request.get('tier')}] {request.get('pack')}",
        "kind": "other",
        "rawInput": request.get("args"),
    }
    params: Message = {
        "sessionId": session_id,
        "toolCall": call,
        "options": [OPTIONS[name] for name in offered],
        "_meta": meta,
    }
    return {"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": params}


def decide(request: Mapping[str, object], response: Mapping[str, object]) -> tuple[str, str | None]:
    """Maps a client response to `(verdict, scope)`; anything unexpected is `("deny", None)`."""
    result = response.get("result")
    outcome = result.get("outcome") if isinstance(result, dict) else None
    if not isinstance(outcome, dict) or outcome.get("outcome") != "selected":
        return "deny", None
    chosen = outcome.get("optionId")
    scopes = request.get("scopes")
    if chosen not in ("once", "session") or not isinstance(scopes, list) or chosen not in scopes:
        return "deny", None
    challenge = request.get("challenge")
    if challenge:
        meta = (result.get("_meta") if isinstance(result, dict) else None) or {}
        echoed = meta.get("hal/challenge") if isinstance(meta, dict) else None
        if str(echoed) != str(challenge):
            return "deny", None
    return "allow", str(chosen)


def process_descends_from(pid: int, ancestor: int) -> bool:
    """True when `pid` is `ancestor` or one of its descendants, read from `/proc/<pid>/stat`."""
    for _ in range(MAX_ANCESTRY):
        if pid == ancestor:
            return True
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        pid = int(stat.rsplit(")", 1)[1].split()[1])  # field 4, after the command in parentheses
        if pid <= 1:
            return False
    return False


def socket_owned_by_us(session_id: str) -> bool:
    """True when the control socket of hub session `session_id` is served by a child of this process.

    The hub child makes its own session id, so the id says nothing about who started it; the
    process that listens does (SO_PEERCRED on the client side reports the listener).
    """
    path = paths.runtime_dir() / control.CONTROL_DIR / f"{session_id}.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(PEER_TIMEOUT_S)
            conn.connect(str(path))
            pid, _uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, PEERCRED_SIZE))
    except OSError:
        return False
    return process_descends_from(pid, os.getpid())


@dataclass
class _Pending:
    request: Mapping[str, object]


class PermissionBridge:
    """Shows broker approvals of this proxy's sessions to the client and returns its answers."""

    def __init__(self, owns: Callable[[str], bool], acp_session: Callable[[], str | None]) -> None:
        self._owns = owns
        self._acp_session = acp_session
        self._counter = 0
        self._pending: dict[str, _Pending] = {}
        self._shown: set[str] = set()

    def is_pending(self, rid: str) -> bool:
        """True while our request `rid` is unanswered here and still pending in the hub."""
        return rid in self._pending

    def request_for(self, rid: str) -> Mapping[str, object] | None:
        """The wire request behind our id `rid`, while it is unanswered."""
        pending = self._pending.get(rid)
        return pending.request if pending else None

    def consume(self, response: Message) -> bool:
        """True when `response` answers one of our requests; the decision is sent in the background."""
        rid = response.get("id")
        pending = self._pending.pop(rid, None) if isinstance(rid, str) else None
        if pending is None:
            return False
        verdict, scope = decide(pending.request, response)
        asyncio.ensure_future(self._answer(pending.request, verdict, scope))
        return True

    async def _answer(self, request: Mapping[str, object], verdict: str, scope: str | None) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: control.answer(request, verdict, scope=scope))
        except control.ControlError as exc:
            log.warning("approval %s not recorded: %s", request.get("id"), exc.code)

    async def run(self, send: Callable[[Message], asyncio.Future[None] | object]) -> None:
        """Polls the control sockets and sends each new request of ours through `send`."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                found = await loop.run_in_executor(None, control.list_pending)
            except OSError as exc:
                log.warning("cannot list approvals: %s", exc)
                found = None
            if found is None:
                await asyncio.sleep(POLL_INTERVAL_S)
                continue
            listed = {str(r.get("id")) for r in found}
            for rid, pending in list(self._pending.items()):
                if str(pending.request.get("id")) not in listed:
                    log.info("approval %s was answered elsewhere or expired", pending.request.get("id"))
                    del self._pending[rid]
            for request in found:
                ident = str(request.get("id"))
                if ident in self._shown or not self._owns(str(request.get("session"))):
                    continue
                self._shown.add(ident)
                self._counter += 1
                rid = f"{OWN_ID_PREFIX}{self._counter}"
                self._pending[rid] = _Pending(request)
                session_id = self._acp_session() or str(request.get("session"))
                await send(build_request(request, rid, session_id))  # type: ignore[misc]
            await asyncio.sleep(POLL_INTERVAL_S)
