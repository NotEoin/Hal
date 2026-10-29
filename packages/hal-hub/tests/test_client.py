"""Tests for `halhub.client.call` (REQ-HUB-73): nested inside a worker, a process hub elsewhere."""

from __future__ import annotations

import json

import pytest
from hubkit import SpyRunner, add_probe_key

from halhub import client
from halhub._audit_standin import tail
from halhub.hub import RoutineRun
from halhub.runner import Runner, RunnerConfig, RunOutcome


@pytest.fixture
def process_hub(make_hub, monkeypatch):
    """Points the client's process hub at a test hub; the client forgets it afterwards."""

    def use(**kwargs):
        hub = make_hub("demo", "routines", **kwargs)
        monkeypatch.setattr(client, "_process_hub", lambda: hub)
        return hub

    yield use
    client._sessions.clear()


def test_allowed_call_returns_the_result(process_hub):
    process_hub(runner=SpyRunner(result="hi"))
    assert client.call("demo_echo", {"text": "hi"}, caller="briefing") == "hi"
    assert next(tail())["origin"] == "cli"


def test_unattended_ask_raises_tool_denied(process_hub):
    spy = SpyRunner()
    process_hub(runner=spy)
    with pytest.raises(client.ToolDenied) as info:
        client.call("demo_wipe", {"path": "/tmp/a"}, caller="routines", unattended=True)
    assert info.value.rule == "no-approver" and spy.calls == []


def test_tool_error_raises_tool_failed(process_hub):
    process_hub(runner=SpyRunner(result=RunOutcome(False, error_type="tool_error", message="boom")))
    with pytest.raises(client.ToolFailed) as info:
        client.call("demo_echo", {"text": "hi"}, caller="briefing")
    assert (info.value.error_type, info.value.message) == ("tool_error", "boom")


def test_invalid_arguments_raise_tool_failed(process_hub):
    process_hub()
    with pytest.raises(client.ToolFailed) as info:
        client.call("demo_echo", {}, caller="briefing")
    assert info.value.error_type == "invalid_arguments"


def test_routine_run_uses_the_routine_origin(process_hub):
    process_hub(runner=SpyRunner())
    steps = json.dumps([{"tool": "demo_echo", "args": {"text": "hi"}}])
    client.call("demo_echo", {"text": "hi"}, caller="routines", routine=RoutineRun("morning", steps))
    assert next(tail())["origin"] == "routine"


def test_timeout_caps_the_tools_own_timeout(process_hub):
    spy = SpyRunner()
    process_hub(runner=spy)
    client.call("demo_echo", {"text": "hi"}, caller="briefing", timeout=0.5)
    assert spy.timeouts == [0.5]


def test_caller_must_be_the_workers_module_inside_a_worker(monkeypatch):
    monkeypatch.setenv("HAL_HUB_WORKER", "1")
    monkeypatch.setenv("HAL_MODULE", "probe")
    with pytest.raises(ValueError, match="not this worker's module"):
        client.call("demo_echo", {"text": "hi"}, caller="demo")


def test_client_inside_worker_uses_nested_channel(make_hub, hal_home, tmp_path):
    add_probe_key(hal_home.config, tmp_path)
    hub = make_hub("demo", "probe", runner=Runner(RunnerConfig(kill_grace=0.3, bwrap="off")))
    s = hub.open_session(kind="hub", origin="cli")
    args = {"tool": "demo_echo", "args": json.dumps({"text": "hi"})}
    assert hub.call("probe_client", args, s, front_end="python").result == "hi"
    nested = [r for r in tail(n=0) if r["event"] == "decision"][-1]
    assert nested["tool"] == "demo_echo" and nested["session"] == s.id


def test_reset_closes_the_process_hub(monkeypatch):
    closed = []

    class FakeHub:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(client, "_hub", FakeHub())
    client.reset()
    assert closed == [True] and client._hub is None
