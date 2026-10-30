"""Tests for `halagent.backends` and the loop's `hal_find_tools` against the fake broker."""

import json

from halagent.backends import BackendOutcome, BrokerBackend, outcome_from_result
from halagent.loop import Agent
from halagent.profiles import resolve_profile
from halagent.settings import AgentSettings
from halagent.tools import Toolset
from halkit.testing.broker import FakeBroker
from halkit.testing.model_server import Reply


def result(text, status=None, is_error=False, **meta):
    out = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if status:
        out["_meta"] = {"hal": {"status": status, **meta}}
    return out


def test_meta_status_wins_over_is_error():
    assert outcome_from_result(result("x", "denied")).status == "denied"
    assert outcome_from_result(result("x", is_error=True)).status == "error"
    assert outcome_from_result(result("x")).status == "ok"


def test_unknown_meta_status_is_an_error():
    assert outcome_from_result(result("x", "weird")).status == "error"


def test_ok_text_passes_through_and_failures_carry_the_note():
    assert BackendOutcome("ok", "page").for_model() == "page"
    assert "This did NOT happen: it was not approved" in BackendOutcome("denied", "no").for_model()
    assert "Fix the arguments" in BackendOutcome("error", "bad").for_model()


def test_untrusted_and_guard_are_read_from_meta():
    out = outcome_from_result(result("x", "ok", untrusted=True, guard="off"))
    assert (out.untrusted, out.guard) == (True, "off")


def test_listed_tool_is_called_directly_and_unlisted_through_hal_call(tmp_path):
    with FakeBroker(tmp_path / "b.sock") as broker:
        broker.add_tool("web_read", "page")
        broker.add_tool("hal_call", "ran")
        backend = BrokerBackend(str(broker.socket_path))
        names = [t.name for t in backend.list_tools()]
        assert names == ["web_read"]
        backend.call("web_read", {"url": "u"})
        backend.call("calendar_add", {"t": 1})
        backend.close()
        assert broker.calls == [
            ("web_read", {"url": "u"}),
            ("hal_call", {"tool": "calendar_add", "arguments": {"t": 1}}),
        ]


def test_find_tools_parses_the_broker_answer(tmp_path):
    rows = {"tools": [{"name": "mail_send", "description": "Send mail", "parameters": {"type": "object"}}]}
    with FakeBroker(tmp_path / "b.sock") as broker:
        broker.add_tool("hal_find_tools", json.dumps(rows))
        backend = BrokerBackend(str(broker.socket_path))
        found = backend.find_tools("send mail", 8)
        backend.close()
    assert [t.name for t in found] == ["mail_send"]
    assert broker.calls == [("hal_find_tools", {"query": "send mail", "limit": 8})]


def test_found_tool_is_offered_in_later_rounds_and_called_by_name(server, tmp_path):
    rows = {"tools": [{"name": "mail_send", "description": "Send mail", "parameters": {"type": "object"}}]}
    with FakeBroker(tmp_path / "b.sock") as broker:
        broker.add_tool("hal_find_tools", json.dumps(rows))
        broker.add_tool("hal_call", result("sent", "ok"))
        server.script(
            Reply(tool_calls=[("hal_find_tools", {"query": "mail"})]),
            Reply(tool_calls=[("mail_send", {})]),
            "done",
        )
        agent = Agent(AgentSettings(endpoint=server.url, broker=str(broker.socket_path)), Toolset(tmp_path))
        res = agent.run("mail bob")
        agent.close()
    first = [t["function"]["name"] for t in server.requests[0]["body"]["tools"]]
    second = [t["function"]["name"] for t in server.requests[1]["body"]["tools"]]
    assert "hal_find_tools" in first and "mail_send" not in first and "mail_send" in second
    assert res.stopped == "done"
    assert ("hal_call", {"tool": "mail_send", "arguments": {}}) in broker.calls


def test_tools_beyond_max_tools_hint_stay_behind_hal_find_tools(server, tmp_path):
    profile = resolve_profile("x", overrides={"max_tools_hint": 10})
    with FakeBroker(tmp_path / "b.sock") as broker:
        for i in range(6):
            broker.add_tool(f"extra_{i}", "r", description=f"unique{i} thing")
        agent = Agent(
            AgentSettings(endpoint=server.url, broker=str(broker.socket_path)),
            Toolset(tmp_path),
            profile=profile,
        )
        assert len(agent._mcp_tools) + len(agent.toolset.tools) + 1 <= 10
        hidden = set(agent._hidden)
        out = json.loads(agent._find_tools({"query": "unique5 thing"}))
        agent.close()
    assert hidden and "extra_5" in {r["name"] for r in out["tools"]}


def test_denied_result_reaches_the_model_with_the_note(server, tmp_path):
    with FakeBroker(tmp_path / "b.sock") as broker:
        broker.add_tool("web_read", result("declined", "denied"))
        server.script(Reply(tool_calls=[("web_read", {})]), "ok")
        agent = Agent(AgentSettings(endpoint=server.url, broker=str(broker.socket_path)), Toolset(tmp_path))
        agent.run("read")
        agent.close()
    tool_msg = [m for m in server.requests[1]["body"]["messages"] if m["role"] == "tool"][0]
    assert "it was not approved" in tool_msg["content"]
