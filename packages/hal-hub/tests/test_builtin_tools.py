"""Tests for `halhub.builtin.tools`: `hal_find_tools` and `hal_modules` answer for the calling session.

They run in-process through `Hub.call` with a real runner, so these tests also cover the hub
binding the call's session and the runner carrying it into its in-process thread.
"""

from __future__ import annotations

import pytest

from halhub.builtin import tools
from halhub.runner import Runner, RunnerConfig


@pytest.fixture
def hub(make_hub):
    return make_hub(runner=Runner(RunnerConfig(bwrap="off")))


def test_find_tools_returns_tier_approval_and_parameters(hub):
    s = hub.open_session(kind="chat", origin="cli")
    r = hub.call("hal_find_tools", {"query": "pretend to delete a file", "limit": "3"}, s, front_end="python")
    assert r.status == "ok"
    first = r.result[0]
    assert (first["name"], first["tier"], first["approval"]) == ("demo_wipe", "act", "ask")
    assert first["parameters"]["required"] == ["path"] and len(r.result) <= 3


def test_find_tools_leaves_out_tools_the_session_may_not_use(hub, hal_home):
    hal_home.policy.write_text('schema_version = 1\n[tools.demo_wipe]\nverdict = "deny"\n')
    s = hub.open_session(kind="chat", origin="cli")
    r = hub.call("hal_find_tools", {"query": "pretend to delete a file"}, s, front_end="python")
    assert r.status == "ok" and "demo_wipe" not in [t["name"] for t in r.result]


def test_modules_lists_each_module_with_status_and_usable_tools(hub):
    s = hub.open_session(kind="chat", origin="cli")
    r = hub.call("hal_modules", {}, s, front_end="python")
    by_name = {m["module"]: m for m in r.result}
    assert by_name["hub"]["tools"] == ["hal_find_tools", "hal_modules"]
    assert by_name["demo"]["status"] == "ok" and "demo_echo" in by_name["demo"]["tools"]


def test_builtin_tools_outside_a_hub_call_raise():
    with pytest.raises(RuntimeError, match=r"Hub\.call"):
        tools.modules()
