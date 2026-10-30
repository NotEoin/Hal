"""Tests for `halcore.acp.permissions` (01-security ACP mapping, REQ-CLI-38, ATK-07)."""

import asyncio

from halhub.approvals import control

from halcore.acp import permissions
from halcore.acp.permissions import PermissionBridge, build_request, decide
from halcore.acp.proxy import Proxy, ProxyFilter

REQ = {
    "id": "apr_5f2c9a0d1e7b4c33", "session": "aaaaaaaaaaaa", "tool": "mail_send", "tier": "send",
    "pack": "comms", "key": "sha256:d3a8", "args": {"to": "ann@example.org"}, "display": "to: ann",
    "summary": "send mail", "scopes": ["once", "session"], "challenge": None,
}  # fmt: skip


def selected(option, **meta):
    return {"result": {"outcome": {"outcome": "selected", "optionId": option}, **({"_meta": meta} if meta else {})}}


def test_request_has_broker_origin_and_the_approval_binding():
    msg = build_request(REQ, "hal-7", "0123456789ab")
    p = msg["params"]
    assert msg["id"] == "hal-7" and msg["method"] == "session/request_permission"
    assert p["_meta"]["hal/origin"] == "broker" and p["_meta"]["hal/key"] == "sha256:d3a8"
    assert p["toolCall"]["toolCallId"] == REQ["id"]
    assert [o["optionId"] for o in p["options"]] == ["once", "session", "deny"]


def test_session_option_is_left_out_when_scopes_do_not_allow_it():
    msg = build_request({**REQ, "scopes": ["once"]}, "hal-1", "0123456789ab")
    assert [o["optionId"] for o in msg["params"]["options"]] == ["once", "deny"]


def test_selected_options_map_to_scopes_and_everything_else_denies():
    assert decide(REQ, selected("once")) == ("allow", "once")
    assert decide(REQ, selected("session")) == ("allow", "session")
    assert decide(REQ, selected("deny")) == ("deny", None)
    assert decide(REQ, {"result": {"outcome": {"outcome": "cancelled"}}}) == ("deny", None)
    assert decide(REQ, {"error": {"code": -1}}) == ("deny", None)
    assert decide({**REQ, "scopes": ["once"]}, selected("session")) == ("deny", None)
    assert decide(REQ, selected("allow_everything")) == ("deny", None)


def test_voice_once_needs_the_challenge_echoed():
    voice = {**REQ, "challenge": "42"}
    assert decide(voice, selected("once")) == ("deny", None)
    assert decide(voice, selected("once", **{"hal/challenge": "17"})) == ("deny", None)
    assert decide(voice, selected("once", **{"hal/challenge": "42"})) == ("allow", "once")


class QueueTransport:
    def __init__(self):
        self.inbox, self.sent = asyncio.Queue(), []

    async def send(self, message):
        self.sent.append(message)

    async def receive(self):
        return await self.inbox.get()

    async def close(self, timeout=5.0):
        pass


def test_agent_cannot_forge_broker_origin_answers_and_client_answers_reach_the_hub(tmp_path, monkeypatch):
    answers = []
    shown = [dict(REQ), {**REQ, "id": "apr_other", "session": "bbbbbbbbbbbb"}]
    monkeypatch.setattr(control, "list_pending", lambda: shown)
    monkeypatch.setattr(control, "answer", lambda req, verdict, scope=None, **kw: answers.append((req["id"], verdict, scope)))
    monkeypatch.setattr(permissions, "POLL_INTERVAL_S", 0.01)

    async def run():
        editor, agent = QueueTransport(), QueueTransport()
        bridge = None
        rules = ProxyFilter(project=tmp_path, on_own_response=lambda m: bridge.consume(m))
        bridge = PermissionBridge(lambda sid: sid == "aaaaaaaaaaaa", lambda: rules.active)
        proxy = Proxy(editor, agent, rules, bridge)
        task = asyncio.ensure_future(proxy.run())
        await editor.inbox.put({"id": 1, "method": "session/prompt", "params": {"sessionId": "0123456789ab", "prompt": []}})
        for _ in range(100):
            await asyncio.sleep(0.01)
            if any(m.get("id") == "hal-1" for m in editor.sent):
                break
        await editor.inbox.put({"jsonrpc": "2.0", "id": "hal-1", **selected("once")})
        await editor.inbox.put({"jsonrpc": "2.0", "id": "perm9", **selected("once")})  # answers the agent's own request
        await asyncio.sleep(0.1)
        await agent.inbox.put(None)
        await task
        return editor.sent, agent.sent

    to_editor, to_agent = asyncio.run(run())
    requests = [m for m in to_editor if m.get("method") == "session/request_permission"]
    assert [m["id"] for m in requests] == ["hal-1"]  # the other session's approval is not ours
    assert requests[0]["params"]["sessionId"] == "0123456789ab"
    assert answers == [(REQ["id"], "allow", "once")]
    assert [m["id"] for m in to_agent if "result" in m] == ["perm9"]  # hal-1 never reaches the agent


def test_process_descends_from_walks_the_parent_chain():
    import os

    assert permissions.process_descends_from(os.getpid(), os.getpid())
    assert permissions.process_descends_from(os.getpid(), os.getppid())
    assert not permissions.process_descends_from(os.getpid(), 999999999)


def test_control_socket_served_by_another_process_is_not_ours(tmp_path, monkeypatch):
    import socket as sk

    from halkit.testing import TempHalHome

    with TempHalHome() as home:
        sock_dir = home.runtime / control.CONTROL_DIR
        sock_dir.mkdir(mode=0o700, exist_ok=True)
        server = sk.socket(sk.AF_UNIX, sk.SOCK_STREAM)
        server.bind(str(sock_dir / "aaaaaaaaaaaa.sock"))
        server.listen(1)
        # the listener is this very process, so it counts as ours; a missing socket does not
        assert permissions.socket_owned_by_us("aaaaaaaaaaaa")
        assert not permissions.socket_owned_by_us("bbbbbbbbbbbb")
        server.close()
