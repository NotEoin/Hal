"""Tests for the assistant toolset and its claims check (REQ-AGT-51, 52, 102)."""

import importlib.util
import json
from pathlib import Path

from halagent.assistant import AssistantOptions, build_assistant
from halagent.assistant.claims import ClaimsCheck
from halagent.assistant.prompts import NO_TOOLS_RETRY, RETRY, system_prompt
from halagent.settings import AgentSettings
from halkit.testing.broker import FakeBroker
from halkit.testing.model_server import Reply


def _tool_names(request: dict) -> list[str]:
    return [t["function"]["name"] for t in request["body"].get("tools", [])]


def _agent(server, tmp_path, broker=None, **opts):
    settings = AgentSettings(endpoint=server.url, broker=str(broker.socket_path) if broker else "")
    return build_assistant(settings, AssistantOptions(user_name="Sam", **opts), tmp_path)


def test_assistant_toolset_has_only_broker_tools(server, tmp_path):
    with FakeBroker(tmp_path / "b.sock") as broker:
        broker.add_tool("calendar_add", "ok")
        server.script("Hello there.")
        agent = _agent(server, tmp_path, broker)
        agent.run("hi")
        agent.close()
    names = set(_tool_names(server.requests[0]))
    assert not names & {"read", "write", "edit", "run", "grep", "glob", "todo", "finish"}
    assert names <= {"hal_find_tools", "calendar_add"}


def test_assistant_offers_the_tools_the_broker_found_for_the_message(server, tmp_path):
    rows = {"tools": [{"name": "mail_send", "description": "Send mail", "parameters": {"type": "object"}}]}
    with FakeBroker(tmp_path / "b.sock") as broker:
        broker.add_tool("hal_find_tools", json.dumps(rows))
        broker.add_tool("other_tool", "x")
        server.script("Fine.")
        agent = _agent(server, tmp_path, broker)
        agent.run("send mail to bob")
        agent.close()
    assert broker.calls[0] == ("hal_find_tools", {"query": "send mail to bob", "limit": 8})
    assert set(_tool_names(server.requests[0])) == {"mail_send", "hal_find_tools"}


def test_assistant_budgets_are_rounds_plus_one_retry(server, tmp_path):
    agent = _agent(server, tmp_path, max_tool_rounds=5)
    assert (agent.settings.max_turns, agent.settings.max_tool_calls) == (6, 10)
    agent.close()


def test_suspect_reply_with_no_tool_run_is_retried_silently(server, tmp_path):
    with FakeBroker(tmp_path / "b.sock") as broker:
        broker.add_tool(
            "hal_find_tools", json.dumps({"tools": [{"name": "calendar_add", "description": "add"}]})
        )
        broker.add_tool("calendar_add", "added")
        server.script(
            "I've added it to your calendar.",
            Reply(tool_calls=[("calendar_add", {})]),
            "Done, lunch is booked.",
        )
        shown = []
        agent = _agent(server, tmp_path, broker)
        agent.on_update = shown.append
        res = agent.run("lunch with Ann on friday")
        agent.close()
    assert res.text == "Done, lunch is booked."
    assert [u["text"] for u in shown if u.get("kind") == "text"] == ["Done, lunch is booked."]
    assert server.requests[1]["body"]["messages"][-1] == {"role": "system", "content": RETRY}


def test_ok_reply_after_a_retry_restores_the_original():
    check = ClaimsCheck(has_tools=True)
    first = check.decide("I can't do that.", tools_run=0, retried=False)
    assert first.action == "retry"
    assert check.decide("OK", tools_run=0, retried=True).text == "I can't do that."


def test_no_tools_assistant_retries_on_a_claim():
    check = ClaimsCheck(has_tools=False)
    verdict = check.decide("I've set a reminder.", tools_run=0, retried=False)
    assert (verdict.action, verdict.system_message) == ("retry", NO_TOOLS_RETRY)
    assert check.decide("Plain answer.", tools_run=0, retried=False).action == "accept"


def test_reply_after_a_tool_ran_is_not_retried():
    check = ClaimsCheck(has_tools=True)
    assert check.decide("I've added it.", tools_run=1, retried=False).action == "accept"


def test_think_block_is_stripped_from_the_released_reply():
    check = ClaimsCheck(has_tools=True)
    assert check.decide("<think>hm</think>Hello.", tools_run=0, retried=False).text == "Hello."


def test_claims_check_can_be_turned_off(server, tmp_path):
    server.script("I've added it.")
    agent = _agent(server, tmp_path, claims_check=False)
    assert agent.run("hi").text == "I've added it."
    agent.close()


def test_prompt_has_user_channel_style_and_local_time():
    text = system_prompt(user="Sam", persona="", channel="voice", timezone="Europe/London",
                         has_tools=True, profile="likes tea")  # fmt: skip
    assert "Sam's calendar" in text and "spoken aloud" in text and "likes tea" in text
    assert "It is " in text and "_confirmed" not in text


def test_prompt_without_tools_says_the_extension_is_missing():
    assert "hal packs add" in system_prompt(user="", persona="", channel="chat", timezone="", has_tools=False)


def test_memory_profile_is_fetched_through_the_broker(server, tmp_path):
    with FakeBroker(tmp_path / "b.sock") as broker:
        broker.add_tool("memory_profile", "Sam lives in Leeds")
        server.script("Hi.")
        agent = _agent(server, tmp_path, broker)
        agent.run("hi")
        agent.close()
    assert "Sam lives in Leeds" in server.requests[0]["body"]["messages"][0]["content"]


def test_toolset_flag_conflicting_with_agent_json_is_a_usage_error(tmp_path, capsys):
    from halagent import cli

    path = tmp_path / "agent.json"
    spec = importlib.util.spec_from_file_location(
        "agentfile_cases", Path(__file__).parent / "test_agentfile.py"
    )
    cases = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cases)
    path.write_text(json.dumps({**cases.GOOD, "toolset": "coding"}))
    assert cli.main(["--config", str(path), "--toolset", "assistant", "-p", "x"]) == cli.EXIT_CONFIG
    assert "conflicts" in capsys.readouterr().err
