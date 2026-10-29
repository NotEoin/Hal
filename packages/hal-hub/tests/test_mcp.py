"""Tests for `halhub.mcp`: the SDK server in-process, in both protocol eras (REQ-HUB-36 to REQ-HUB-48).

Each test connects the SDK's own `Client` to `build_server(...)` in memory, once with the
`initialize` handshake (`legacy`) and once with the 2026-07-28 per-request envelope. The hub
is the synthetic `demo` pack with a spy runner unless a test needs real workers.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time

import anyio
import pytest
from hubkit import add_probe_key
from mcp import Client

from halhub import _control_standin as control
from halhub._audit_standin import tail
from halhub.hub import CallResult
from halhub.mcp import META_TOOLS, ServeOptions, build_server, render
from halhub.runner import Runner, RunnerConfig

ERAS = ("legacy", "2026-07-28")
APPROVE_WAIT_S = 10  # how long the answering thread looks for the pending request


def session_call(hub, mode, name, args=None, options=None):
    """Lists the tools, then calls `name`; returns (tool names, result)."""

    async def main():
        async with Client(build_server(hub, options), mode=mode) as client:
            listed = await client.list_tools()
            result = await client.call_tool(name, args or {})
            return [t.name for t in listed.tools], result

    return anyio.run(main)


def text_of(result) -> str:
    assert len(result.content) == 1
    return result.content[0].text


@pytest.mark.parametrize("mode", ERAS)
def test_meta_mode_lists_meta_tools_first_then_offered_tools_by_name(make_hub, mode):
    hub = make_hub()
    hub.config = dataclasses.replace(hub.config, pinned=("demo_lights", "nope_missing"))
    names, _ = session_call(hub, mode, "hal_modules")
    assert names == ["hal_find_tools", "hal_call", "hal_modules", "demo_echo", "demo_lights"]


@pytest.mark.parametrize("mode", ERAS)
def test_hal_call_runs_the_inner_tool_through_the_pipeline(make_hub, mode):
    hub = make_hub()
    _, result = session_call(hub, mode, "hal_call", {"tool": "demo_echo", "arguments": {"text": "hi"}})
    assert (text_of(result), result.is_error) == ("ok", False)
    assert result.meta["hal"] == {"status": "ok", "untrusted": False, "guard": None}
    assert [(name, args) for name, args, _ in hub.runner.calls] == [("demo_echo", {"text": "hi"})]


@pytest.mark.parametrize("mode", ERAS)
def test_hal_find_tools_runs_the_builtin_tool_for_this_session(make_hub, mode):
    hub = make_hub(runner=Runner(RunnerConfig(bwrap="off")))
    _, result = session_call(hub, mode, "hal_find_tools", {"query": "pretend to delete a file", "limit": 2})
    found = json.loads(text_of(result))
    assert (found[0]["name"], found[0]["approval"]) == ("demo_wipe", "ask") and len(found) <= 2


def test_malformed_hal_call_is_invalid_arguments(make_hub):
    _, result = session_call(make_hub(), "legacy", "hal_call", {"tool": "demo_echo", "arguments": "text=hi"})
    data = json.loads(text_of(result))
    assert result.is_error and data["error_type"] == "invalid_arguments"


def test_unknown_tool_is_unavailable_and_suggests_search(make_hub):
    _, result = session_call(make_hub(), "2026-07-28", "nope_tool")
    data = json.loads(text_of(result))
    assert result.is_error and data["error_type"] == "unknown_tool" and "hal_find_tools" in data["message"]
    assert result.meta["hal"]["status"] == "unavailable"


@pytest.mark.parametrize("mode", ERAS)
def test_untrusted_output_uses_security_envelope(make_hub, mode):
    hub = make_hub()
    hub.runner.result = "page text"
    _, result = session_call(hub, mode, "demo_fetch", {"url": "https://example.org/"})
    text = text_of(result)
    assert text.startswith('<tool_result tool="demo_fetch" trust="untrusted"') and "page text" in text
    assert result.meta["hal"] == {"status": "ok", "untrusted": True, "guard": "off"}


def test_result_meta_hal_has_status_untrusted_guard():
    cases = {
        "ok": CallResult("ok", "c_1", "t_x", result={"a": 1}),
        "error": CallResult("error", "c_2", "t_x", error_type="timeout", message="slow"),
        "denied": CallResult("denied", "c_3", "t_x", error_type="policy_denied", rule="r", message="no"),
        "unavailable": CallResult("error", "c_4", "t_x", error_type="unavailable", message="gone"),
    }
    for status, result in cases.items():
        rendered = render(result)
        assert set(rendered.meta["hal"]) == {"status", "untrusted", "guard"}
        assert rendered.meta["hal"]["status"] == status and rendered.is_error == (status != "ok")
    assert text_of(render(cases["ok"])) == '{"a":1}'
    assert "error" not in json.loads(text_of(render(cases["error"])))


def test_error_result_has_no_traceback(make_hub, hal_home, tmp_path):
    add_probe_key(hal_home.config, tmp_path)
    hub = make_hub("probe", runner=Runner(RunnerConfig(bwrap="off")))
    _, result = session_call(hub, "legacy", "hal_call", {"tool": "probe_boom"})
    text = text_of(result)
    assert result.is_error and json.loads(text)["status"] == "error"
    assert "Traceback" not in text and 'File "' not in text


@pytest.mark.parametrize("mode", ERAS)
def test_ask_waits_for_hal_approve_on_the_control_socket(make_hub, mode, monkeypatch):
    monkeypatch.setattr(control, "peer_in_harness", lambda pid: False)
    hub = make_hub()

    def approve_when_listed() -> None:
        deadline = time.monotonic() + APPROVE_WAIT_S
        while time.monotonic() < deadline:
            pending = control.list_pending()
            if pending:
                control.answer(pending[0], "allow", scope="once")
                return
            time.sleep(0.05)

    approver = threading.Thread(target=approve_when_listed, daemon=True)
    approver.start()
    hub.runner.result = "wiped"
    _, result = session_call(hub, mode, "demo_wipe", {"path": "/tmp/x"})
    approver.join(5)
    assert (text_of(result), result.is_error) == ("wiped", False)


def test_ask_with_no_answer_is_denied_with_approval_timeout(make_hub, hal_home):
    hal_home.policy.write_text("schema_version = 1\n[approval]\ntimeout_s = 1\n")
    hub = make_hub()
    _, result = session_call(hub, "legacy", "hal_call", {"tool": "demo_wipe", "arguments": {"path": "/x"}})
    data = json.loads(text_of(result))
    assert data["rule"] == "approval-timeout" and result.meta["hal"]["status"] == "denied"
    assert hub.runner.calls == []
    assert control.list_pending() == []


def test_given_session_is_used_with_its_own_approver(make_hub):
    hub = make_hub()
    session = hub.open_session(kind="box", origin="box_agent")
    _, result = session_call(hub, "legacy", "demo_wipe", {"path": "/x"}, ServeOptions(session=session))
    assert json.loads(text_of(result))["rule"] == "no-approver"
    assert {r["session"] for r in tail(n=0) if r["event"] == "decision"} == {session.id}


def test_meta_tool_schemas_match_the_spec():
    by_name = {t["name"]: t["inputSchema"] for t in META_TOOLS}
    assert by_name["hal_find_tools"]["required"] == ["query"]
    assert by_name["hal_call"]["required"] == ["tool"]
    assert all(schema["additionalProperties"] is False for schema in by_name.values())


# ---- modes, limits, annotations and `_meta` (REQ-HUB-38, REQ-HUB-39, REQ-HUB-48) --------------


def list_tools(hub, mode, options=None):
    async def main():
        async with Client(build_server(hub, options), mode=mode) as client:
            return (await client.list_tools()).tools

    return anyio.run(main)


@pytest.mark.parametrize("mode", ERAS)
def test_flat_mode_lists_every_usable_tool_by_name(make_hub, hal_home, mode):
    hal_home.policy.write_text('schema_version = 1\n[tools.demo_lights]\nverdict = "deny"\n')
    names = [t.name for t in list_tools(make_hub(), mode, ServeOptions(mode="flat"))]
    assert names == sorted(names) and "demo_wipe" in names and "hal_find_tools" in names
    assert "demo_lights" not in names and "hal_call" not in names


@pytest.mark.parametrize("mode", ERAS)
def test_flat_mode_refuses_unlisted_tool(make_hub, hal_home, mode):
    hal_home.policy.write_text('schema_version = 1\n[tools.demo_lights]\nverdict = "deny"\n')
    hub = make_hub()
    flat = ServeOptions(mode="flat")
    for name, args in (("demo_lights", {"colour": "red"}), ("hal_call", {"tool": "demo_echo"})):
        _, result = session_call(hub, mode, name, args, flat)
        data = json.loads(text_of(result))
        assert data["error_type"] == "unknown_tool" and result.meta["hal"]["status"] == "unavailable"
    assert hub.runner.calls == []


def test_listed_tools_carry_tier_annotations_and_hal_meta(make_hub):
    tools = {t.name: t for t in list_tools(make_hub(), "legacy", ServeOptions(mode="flat"))}
    wipe, echo = tools["demo_wipe"], tools["demo_echo"]
    assert wipe.meta == {"hal/tier": "act", "hal/approval": "ask"}
    assert echo.meta == {"hal/tier": "read", "hal/approval": "none"}
    assert wipe.annotations.destructive_hint is True and echo.annotations.read_only_hint is True


def test_meta_tools_carry_their_fixed_annotations(make_hub):
    tools = {t.name: t for t in list_tools(make_hub(), "2026-07-28")}
    assert tools["hal_call"].annotations.destructive_hint is True
    assert tools["hal_find_tools"].annotations.read_only_hint is True
    assert tools["hal_find_tools"].meta == {"hal/tier": "read", "hal/approval": "none"}


def test_limits_narrow_listing_and_calls_in_flat_mode(make_hub):
    hub = make_hub("demo", "routines")
    options = ServeOptions(
        mode="flat", packs=frozenset({"demo"}), tools=frozenset({"demo_echo", "demo_wipe"})
    )
    assert [t.name for t in list_tools(hub, "legacy", options)] == ["demo_echo", "demo_wipe"]
    _, result = session_call(hub, "legacy", "demo_fetch", {"url": "https://example.org/"}, options)
    assert json.loads(text_of(result))["error_type"] == "unknown_tool"


def test_limits_narrow_hal_call_and_search_in_meta_mode(make_hub):
    hub = make_hub("demo", "routines", runner=Runner(RunnerConfig(bwrap="off")))
    options = ServeOptions(packs=frozenset({"routines"}))
    _, refused = session_call(
        hub, "legacy", "hal_call", {"tool": "demo_echo", "arguments": {"text": "x"}}, options
    )
    assert json.loads(text_of(refused))["error_type"] == "unknown_tool"
    _, found = session_call(hub, "legacy", "hal_find_tools", {"query": "echo text back"}, options)
    assert all(t["pack"] == "routines" for t in json.loads(text_of(found)))
    _, modules = session_call(hub, "legacy", "hal_modules", {}, options)
    assert [m["pack"] for m in json.loads(text_of(modules))] == ["routines"]


def test_bad_serve_options_raise():
    with pytest.raises(ValueError, match="mode"):
        ServeOptions(mode="wide")
    with pytest.raises(ValueError, match="empty"):
        ServeOptions(packs=frozenset())


def test_flat_mode_instructions_drop_the_search_hint(make_hub):
    assert "hal_find_tools" not in build_server(make_hub(), ServeOptions(mode="flat")).instructions
    assert "hal_find_tools" in build_server(make_hub()).instructions
