"""Tests for `halhub.providers` and `Hub.add_provider` (04-hub "External tool providers").

A stub provider stands in for a host MCP server upstream (02-sandbox runs the real ones); the
upstream's schema carries annotations that must change nothing (01 REQ-SEC-87, REQ-SEC-88).
"""

from __future__ import annotations

import dataclasses

import pytest
from hubkit import ScriptedApprover

from halhub._audit_standin import tail
from halhub._policy_standin import Tier
from halhub.providers import provider_meta, provider_tool
from halhub.runner import RunOutcome

SCHEMAS = {
    "list_issues": {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "owner/name"},
            "state": {"type": "string", "enum": ["open", "closed"]},
            "limit": {"type": "integer"},
            "labels": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["repo"],
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    "Create-Issue": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
}


class StubProvider:
    """A host MCP upstream: records calls and returns `reply`, or raises it when it is an exception."""

    def __init__(self, name: str = "github", reply: object = "issue list", tier: Tier = Tier.ACT) -> None:
        self.name = name
        self.default_tier = tier
        self.reply = reply
        self.calls: list[tuple[str, dict, float]] = []
        self.closed = False

    def tools(self):
        specs = [
            provider_tool(self.name, upstream, "From upstream.", schema)
            for upstream, schema in SCHEMAS.items()
        ]
        # an upstream that claims a safe tier and a reviewer must not get either
        return [dataclasses.replace(s, tier="read", reviewable=True, untrusted_output=False) for s in specs]

    def call(self, tool, args, *, timeout, cancel=None):
        self.calls.append((tool, dict(args), timeout))
        if isinstance(self.reply, Exception):
            raise self.reply
        return RunOutcome(True, result=self.reply)

    def close(self) -> None:
        self.closed = True


def test_provider_tools_are_named_and_tiered(make_hub):
    hub = make_hub()
    hub.add_provider(StubProvider())
    tools = {t.name: t for t in hub.provider_tools()}
    assert set(tools) == {"github__list_issues", "github__create_issue"}
    issue = tools["github__list_issues"]
    assert (issue.pack, issue.tier, issue.untrusted_output, issue.reviewable) == (
        "mcp-github",
        "act",
        True,
        False,
    )
    assert tools["github__create_issue"].function == "Create-Issue"


def test_scalar_params_keep_their_type_and_others_become_json():
    spec = provider_tool("github", "list_issues", "d", SCHEMAS["list_issues"])
    params = {p.name: p for p in spec.params}
    assert params["limit"].type == "integer" and params["state"].enum == ("open", "closed")
    assert params["repo"].required and not params["limit"].required
    assert params["labels"].format == "json" and params["labels"].type == "string"


def test_provider_annotations_are_ignored(make_hub):
    hub = make_hub()
    provider = StubProvider()
    hub.add_provider(provider)
    s = hub.open_session(kind="hub", origin="host_mcp")
    r = hub.call("github__list_issues", {"repo": "a/b"}, s, front_end="mcp")
    assert (r.status, r.rule) == ("denied", "no-approver") and provider.calls == []
    meta = provider_meta(hub.provider_tools()[1])
    assert meta.tier is Tier.ACT and not meta.reviewable and meta.untrusted_output


def test_provider_calls_go_through_policy(make_hub):
    hub = make_hub()
    provider = StubProvider()
    hub.add_provider(provider)
    approver = ScriptedApprover()
    s = hub.open_session(kind="hub", origin="host_mcp", approver=approver)
    args = {"repo": "a/b", "labels": '["bug", "ui"]', "limit": "5"}
    r = hub.call("github__list_issues", args, s, front_end="mcp")
    assert r.status == "ok" and r.untrusted and r.result.startswith('<tool_result tool="github__list_issues"')
    assert approver.requests[0].pack == "mcp-github"
    assert provider.calls == [("list_issues", {"repo": "a/b", "labels": ["bug", "ui"], "limit": 5}, 60.0)]
    assert [e["event"] for e in tail(n=0)][:1] == ["decision"]


def test_policy_toml_can_allow_a_provider_tool_by_name(make_hub, hal_home):
    hal_home.policy.write_text('schema_version = 1\n[tools.github__list_issues]\nverdict = "allow"\n')
    hub = make_hub()
    provider = StubProvider()
    hub.add_provider(provider)
    s = hub.open_session(kind="hub", origin="host_mcp")
    assert hub.call("github__list_issues", {"repo": "a/b"}, s, front_end="mcp").status == "ok"


def test_provider_failure_is_a_tool_error(make_hub, hal_home):
    hal_home.policy.write_text('schema_version = 1\n[packs.mcp-github]\nverdict = "allow"\n')
    hub = make_hub()
    hub.add_provider(StubProvider(reply=RuntimeError("upstream died")))
    s = hub.open_session(kind="hub", origin="host_mcp")
    r = hub.call("github__create_issue", {"title": "x"}, s, front_end="mcp")
    assert r.error_type == "tool_error" and "upstream died" in r.message


@pytest.mark.parametrize("name", ["GitHub", "git-hub", "a" * 33])
def test_bad_provider_name_is_refused(make_hub, name):
    provider = StubProvider()
    provider.name = name
    with pytest.raises(ValueError, match="provider name"):
        make_hub().add_provider(provider)


def test_second_provider_with_the_same_name_is_refused(make_hub):
    hub = make_hub()
    hub.add_provider(StubProvider())
    with pytest.raises(ValueError, match="already added"):
        hub.add_provider(StubProvider())


def test_provider_tool_with_another_prefix_is_refused(make_hub):
    provider = StubProvider()
    provider.tools = lambda: [provider_tool("gitlab", "x_y", "d", {})]
    with pytest.raises(ValueError, match="must be named github__"):
        make_hub().add_provider(provider)


def test_closing_the_hub_closes_its_providers(make_hub):
    hub = make_hub()
    provider = StubProvider()
    hub.add_provider(provider)
    hub.close()
    assert provider.closed
