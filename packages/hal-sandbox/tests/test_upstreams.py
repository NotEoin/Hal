"""Tests for host MCP upstreams: config parsing, tool naming, failure isolation, environment."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from halhub.runner import RunOutcome
from halsandbox.broker import DEFAULT_COMMAND, upstream_command
from halsandbox.errors import SandboxError
from halsandbox.upstreams import McpUpstream, UpstreamConfig, parse_upstreams, upstream_env

STUB = str(Path(__file__).parent / "fixtures" / "stub_mcp_server.py")


def cfg(**kw) -> UpstreamConfig:
    return UpstreamConfig(name=kw.pop("name", "stub"), command=sys.executable, args=(STUB,), **kw)


@pytest.fixture
def up():
    made = []

    def make(**kw):
        made.append(McpUpstream(cfg(**kw)))
        return made[-1]

    yield make
    for u in made:
        u.close()


def test_only_enabled_host_entries_become_upstreams():
    servers = {
        "mail": {"host": True, "command": "mailmcp", "args": ["-x"], "timeout_s": 5},
        "off": {"host": True, "enabled": False, "command": "x"},
        "inbox": {"command": "x"},
    }
    got = parse_upstreams(servers)
    assert [(u.name, u.command, u.args, u.timeout_s) for u in got] == [("mail", "mailmcp", ("-x",), 5.0)]


@pytest.mark.parametrize(
    "name, raw",
    [
        ("hal-desktop", {"host": True, "command": "x"}),
        ("mail", {"host": True, "url": "http://x"}),
        ("mail", {"host": True}),
    ],
)
def test_unusable_host_entries_are_refused_with_the_reason(name, raw):
    with pytest.raises(SandboxError, match=name):
        parse_upstreams({name: raw})


def test_upstream_tools_are_prefixed_and_marked_untrusted(up):
    tools = {t.name: t for t in up().tools()}
    assert set(tools) == {"stub__echo", "stub__boom", "stub__die", "stub__env"}
    assert tools["stub__echo"].pack == "mcp-stub"
    assert tools["stub__echo"].untrusted_output and not tools["stub__echo"].reviewable


def test_tools_allow_and_deny_apply_before_tools_are_offered(up):
    assert [t.name for t in up(tools_allow=("echo",)).tools()] == ["stub__echo"]
    assert "stub__die" not in {t.name for t in up(tools_deny=("die",)).tools()}


def test_call_returns_the_text_and_an_error_result_is_an_error_outcome(up):
    u = up()
    assert u.call("echo", {"text": "hi"}, timeout=10) == RunOutcome(True, result="hi")
    out = u.call("boom", {}, timeout=10)
    assert not out.ok and "boom" in (out.message or "")


def test_a_crashing_upstream_fails_one_call_then_restarts_once(up):
    u = up()
    assert not u.call("die", {}, timeout=10).ok
    assert u.call("echo", {"text": "again"}, timeout=10).result == "again"  # restart 1 of 1
    assert not u.call("die", {}, timeout=10).ok
    out = u.call("echo", {"text": "x"}, timeout=10)
    assert not out.ok and "unavailable" in (out.message or "")


def test_a_missing_command_gives_an_unavailable_upstream_not_an_exception():
    u = McpUpstream(UpstreamConfig(name="gone", command="/nonexistent/mcp-server"))
    try:
        assert u.failed and u.tools() == []
        assert not u.call("x", {}, timeout=5).ok
    finally:
        u.close()


def test_upstream_environment_is_the_base_plus_the_entry_env(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/s")
    monkeypatch.setenv("HAL_SECRET_TOKEN", "synthetic")
    env = upstream_env(cfg(env={"MAIL_USER": "me"}))
    assert env["SSH_AUTH_SOCK"] == "/s" and env["MAIL_USER"] == "me"
    assert "HAL_SECRET_TOKEN" not in env


def test_the_upstream_process_sees_no_host_secret_variable(up, monkeypatch):
    monkeypatch.setenv("HAL_SECRET_TOKEN", "synthetic")
    names = json.loads(up().call("env", {}, timeout=10).result)
    assert "HAL_SECRET_TOKEN" not in names


def test_upstream_command_runs_the_hub_under_hubchild():
    cmd = upstream_command(Path("/x/u.json"), [*DEFAULT_COMMAND, "--profile", "p"])
    assert cmd[1:6] == ["-m", "halsandbox.hubchild", "/x/u.json", "--", "mcp"]
    assert cmd[-2:] == ["--profile", "p"]
    with pytest.raises(ValueError):
        upstream_command(Path("/x"), ["other"])


def test_install_adds_each_upstream_to_every_new_hub_and_skips_a_failed_one(monkeypatch):
    from halhub import cli
    from halsandbox import upstreams

    monkeypatch.setattr(cli, "HUB_HOOKS", [])
    upstreams.install([cfg(name="good"), UpstreamConfig(name="bad", command="/nonexistent/x")])

    class FakeHub:
        added: list[str] = []

        def add_provider(self, provider):
            self.added.append(provider.name)
            provider.close()

    hub = FakeHub()
    cli.HUB_HOOKS[0](hub)
    assert hub.added == ["good"]


def test_hubchild_without_arguments_prints_usage_and_exits_2(capsys):
    from halsandbox import hubchild

    assert hubchild.main([]) == 2
    assert "usage" in capsys.readouterr().err
