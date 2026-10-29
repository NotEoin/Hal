"""Tests for `halhub._policy_standin`: the phase-1 copy of 01-security's policy interface.

Each case is one row of 01-security.md ("Decision algorithm", "Built-in profiles", "Channel
profiles", "Approval binding") for the parts the stand-in implements. 01's own
`test_policy_*.py` files replace this file in phase 2.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from halhub._policy_standin import (
    Context,
    Grant,
    Origin,
    ParamSpec,
    Policy,
    RateLimiter,
    RateSnapshot,
    Scope,
    Taint,
    Tier,
    ToolCall,
    ToolMeta,
    Verdict,
    approval_key,
    canonical_json,
    decide,
    load_policy,
    prepare,
    security_digest,
)
from halkit.config import ConfigError
from halkit.testing import FakeClock

VECTORS = Path(__file__).resolve().parents[3] / "tests" / "data" / "approval_vectors.json"
SID = "0123456789ab"
HOME = Path("/home/tester")


def meta(name: str = "demo_tool", tier: Tier = Tier.ACT, *params: ParamSpec, **flags: object) -> ToolMeta:
    return ToolMeta(name=name, pack="demo", pack_version="0.1.0", tier=tier, params=params, **flags)


def call(tool: ToolMeta, args: dict, **kwargs: object) -> ToolCall:
    fields = {"session": SID, "origin": Origin.CLI, "call_id": "c_0000000000000001"}
    fields.update(kwargs)
    return ToolCall(tool=tool.name, args=args, **fields)


def ctx(clock: FakeClock | None = None, **kwargs: object) -> Context:
    fields: dict[str, object] = {
        "now": (clock or FakeClock()).now(),
        "session_kind": "chat",
        "taint": Taint.CLEAN,
        "grants": (),
        "rate": RateSnapshot(),
        "approver_available": True,
        "paths": {},
        "workspaces": (),
        "home": HOME,
    }
    fields.update(kwargs)
    return Context(**fields)


def grant(tool: ToolMeta, args: dict, scope: Scope = Scope.ONCE, **kwargs: object) -> Grant:
    clock = FakeClock()
    fields: dict[str, object] = {
        "id": "g_0000000000000001",
        "scope": scope,
        "key": approval_key(tool.pack, tool.name, args),
        "pack": tool.pack,
        "tool": tool.name,
        "session": SID,
        "routine_id": None,
        "steps_sha256": None,
        "step_keys": {},
        "meta_sha256": security_digest(tool),
        "created": clock.now(),
        "expires": clock.now() + timedelta(seconds=600),
        "used": False,
        "source": "test",
    }
    fields.update(kwargs)
    return Grant(**fields)


@pytest.mark.parametrize(
    ("profile", "tier", "verdict"),
    [
        ("default", Tier.READ, Verdict.ALLOW),
        ("default", Tier.WRITE, Verdict.ALLOW),
        ("default", Tier.ACT, Verdict.ASK),
        ("default", Tier.SEND, Verdict.ASK),
        ("default", Tier.SYSTEM, Verdict.ASK),
        ("strict", Tier.WRITE, Verdict.ASK),
        ("strict", Tier.SYSTEM, Verdict.DENY),
        ("trusted", Tier.ACT, Verdict.ALLOW),
        ("trusted", Tier.SEND, Verdict.ASK),
        ("standalone", Tier.SYSTEM, Verdict.DENY),
    ],
)
def test_tier_table_gives_each_profiles_verdict(profile, tier, verdict):
    assert load_policy(profile=profile).tool(meta(tier=tier)).verdict is verdict


def test_reversible_act_is_allowed_by_default_and_asks_in_strict():
    lights = meta(tier=Tier.ACT, reversible=True)
    assert load_policy().tool(lights).rule == "act-reversible"
    assert load_policy().tool(lights).verdict is Verdict.ALLOW
    assert load_policy(profile="strict").tool(lights).verdict is Verdict.ASK


def test_builtin_send_entries_allow_by_name_except_in_strict():
    notify = meta("notify_send", Tier.SEND)
    assert load_policy().tool(notify).rule == "profile-tool:notify_send@default"
    assert load_policy().tool(notify).verdict is Verdict.ALLOW
    assert load_policy(profile="strict").tool(notify).verdict is Verdict.ASK


def test_runs_code_is_denied_unless_the_user_asks_for_it_by_name(hal_home):
    shell = meta("shell_run", Tier.SYSTEM, runs_code=True)
    assert load_policy().tool(shell).rule == "runs-code"
    hal_home.policy.write_text('schema_version = 1\n[tools.shell_run]\nverdict = "ask"\n')
    assert load_policy().tool(shell).verdict is Verdict.ASK


def test_system_tool_never_resolves_to_allow(hal_home):
    hal_home.policy.write_text(
        'schema_version = 1\n[tools.power_off]\nverdict = "allow"\nreviewable = true\n'
    )
    tp = load_policy().tool(meta("power_off", Tier.SYSTEM))
    assert tp.verdict is Verdict.ASK and not tp.reviewable


@pytest.mark.parametrize("channel", ["voice", "telegram", "remote"])
def test_channel_denies_system_and_offers_once_only(channel):
    policy = load_policy(channel=channel)
    assert policy.tool(meta(tier=Tier.SYSTEM)).rule == f"tier:system@{channel}"
    assert policy.allowed_scopes(meta(tier=Tier.ACT)) == (Scope.ONCE,)


def test_remote_channel_denies_the_desktop_pack():
    tool = ToolMeta("desktop_type", "desktop", "0.1.0", Tier.ACT, ())
    assert load_policy(channel="remote").tool(tool).verdict is Verdict.DENY


def test_voice_channel_shortens_the_approval_timeout():
    assert load_policy(channel="voice").approval.timeout_s == 20


def test_policy_file_overrides_by_pack_then_tool(hal_home):
    hal_home.policy.write_text(
        'schema_version = 1\n[packs.demo]\nverdict = "deny"\n[tools.demo_tool]\nverdict = "allow"\n'
    )
    policy = load_policy()
    assert policy.tool(meta()).rule == "tool:demo_tool"
    assert policy.tool(meta("demo_other")).rule == "pack:demo"


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("schema_version = 1\nfoo = 1\n", "foo"),
        ("schema_version = 1\n[paths]\nread_roots = []\n", "paths"),
        ("schema_version = 1\n[approval]\ntimeout_s = true\n", "approval.timeout_s"),
        ("schema_version = 1\n[tools.x]\nallow_args = {}\n", "tools.x.allow_args"),
        ('schema_version = 1\n[tools.x]\nverdict = "always"\n', "tools.x.verdict"),
        ('schema_version = 1\nprofile = "lax"\n', "profile"),
        ('profile = "default"\n', "schema_version"),
    ],
)
def test_bad_or_unsupported_policy_key_is_a_config_error_naming_it(hal_home, text, key):
    hal_home.policy.write_text(text)
    with pytest.raises(ConfigError) as info:
        load_policy()
    assert info.value.key == key and str(hal_home.policy) in str(info.value)


def test_malformed_policy_names_file_and_line(hal_home):
    hal_home.policy.write_text("schema_version = 1\n[approval\n")
    with pytest.raises(ConfigError, match=r"policy\.toml:2"):
        load_policy()


def test_unknown_tool_is_denied():
    d = decide(ToolCall("nope_tool", {}, SID, Origin.CLI, "c_1"), None, Policy(), ctx())
    assert d.verdict is Verdict.DENY and d.rule == "unknown-tool"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"text": "a", "extra": 1}, "unknown"),
        ({}, "missing"),
        ({"text": 3}, "type"),
        ({"text": "a", "n": True}, "type"),
        ({"text": "a", "colour": "pink"}, "enum"),
        ({"text": "x" * 11}, "length"),
        ({"text": "a\x00b"}, "nul"),
        ({"text": "a", "data": "{nope"}, "json"),
        ({"text": "a", "cmd": "ls\nrm"}, "command"),
        ({"text": "a", "url": "-oProxyCommand"}, "option"),
    ],
)
def test_each_bad_argument_gives_its_bad_args_code(args, code):
    tool = meta(
        "demo_tool",
        Tier.READ,
        ParamSpec("text", required=True, max_length=10),
        ParamSpec("n", type="integer"),
        ParamSpec("colour", enum=("red", "blue")),
        ParamSpec("data", format="json"),
        ParamSpec("cmd", format="command"),
        ParamSpec("url", format="url"),
    )
    d = decide(call(tool, args), tool, Policy(), ctx())
    assert d.verdict is Verdict.DENY and d.rule == f"bad-args:{code}" and d.reason


def test_confirmed_flag_is_stripped_recorded_and_never_approves():
    tool = meta(tier=Tier.SEND)
    d = decide(call(tool, {"_confirmed": True, "_x": 1}), tool, Policy(), ctx())
    assert d.verdict is Verdict.ASK and d.stripped == ("_confirmed", "_x")
    assert d.self_approval == ("_confirmed",) and "_confirmed" not in d.args


def test_unknown_approval_like_name_counts_as_self_approval():
    tool = meta(tier=Tier.SEND)
    d = decide(call(tool, {"approved": True}), tool, Policy(), ctx())
    assert d.rule == "bad-args:unknown" and d.self_approval == ("approved",)


@pytest.mark.parametrize(
    ("url", "rule"),
    [
        ("http://example.org", "url:scheme"),
        ("file:///etc/passwd", "url:scheme"),
        ("https://user:pw@example.org/", "url:userinfo"),
        ("https://127.0.0.1/", "url:ip-literal"),
        ("https://printer.local/", "url:private"),
        ("https://intranet/", "url:private"),
    ],
)
def test_url_checks_deny_unsafe_urls(url, rule):
    tool = meta("web_read", Tier.SEND, ParamSpec("url", format="url"))
    d = decide(call(tool, {"url": url}), tool, Policy(), ctx())
    assert d.verdict is Verdict.DENY and d.rule == rule


def test_http_is_allowed_with_allow_http(hal_home):
    hal_home.policy.write_text("schema_version = 1\n[egress]\nallow_http = true\n")
    tool = meta("web_read", Tier.SEND, ParamSpec("url", format="url"))
    assert (
        decide(call(tool, {"url": "http://example.org"}), tool, load_policy(), ctx()).verdict is Verdict.ASK
    )


def test_prepare_resolves_home_and_refuses_relative_paths(tmp_path):
    tool = meta("demo_tool", Tier.ACT, ParamSpec("a", format="path"), ParamSpec("b", format="path"))
    resolved = prepare(call(tool, {"a": "~/x", "b": "rel/x"}), tool, home=tmp_path)
    assert resolved["a"].resolved == tmp_path.resolve() / "x"
    assert resolved["b"].resolved is None and resolved["b"].error == "relative"


def test_decision_args_carry_the_resolved_path(tmp_path):
    tool = meta("demo_tool", Tier.ACT, ParamSpec("path", format="path"))
    c = call(tool, {"path": "~/f"})
    d = decide(c, tool, Policy(), ctx(paths=prepare(c, tool, home=tmp_path)))
    assert d.args == {"path": str(tmp_path.resolve() / "f")}
    assert d.key == approval_key("demo", "demo_tool", d.args)


def test_unresolved_path_is_denied():
    tool = meta("demo_tool", Tier.ACT, ParamSpec("path", format="path"))
    c = call(tool, {"path": "rel"})
    assert decide(c, tool, Policy(), ctx(paths=prepare(c, tool, home=HOME))).rule == "path:invalid"


def test_matching_once_grant_allows_and_names_the_grant():
    tool = meta()
    d = decide(call(tool, {}), tool, Policy(), ctx(grants=(grant(tool, {}),)))
    assert d.verdict is Verdict.ALLOW and d.grant_id == "g_0000000000000001"


@pytest.mark.parametrize(
    "change",
    [
        {"used": True},
        {"session": "ffffffffffff"},
        {"key": "sha256:other"},
        {"meta_sha256": "sha256:old"},
        {"expires": FakeClock().now()},
    ],
)
def test_grant_that_differs_in_any_bound_does_not_match(change):
    tool = meta()
    d = decide(call(tool, {}), tool, Policy(), ctx(grants=(grant(tool, {}, **change),)))
    assert d.verdict is Verdict.ASK


def test_flagged_turn_ignores_grants_and_asks_for_allowed_act():
    lights = meta(tier=Tier.ACT, reversible=True)
    d = decide(call(lights, {}), lights, Policy(), ctx(taint=Taint.FLAGGED, grants=(grant(lights, {}),)))
    assert d.verdict is Verdict.ASK and d.rule == "flagged-turn" and d.fresh_only


def test_no_approver_turns_ask_into_deny():
    tool = meta()
    assert decide(call(tool, {}), tool, Policy(), ctx(approver_available=False)).rule == "no-approver"


def test_reviewable_needs_reviewer_enabled_and_no_send_tier(hal_home):
    tool = meta(reviewable=True)
    assert not decide(call(tool, {}), tool, Policy(), ctx()).reviewable
    hal_home.policy.write_text("schema_version = 1\n[reviewer]\nenabled = true\n")
    policy = load_policy()
    assert decide(call(tool, {}), tool, policy, ctx()).reviewable
    send = meta(tier=Tier.SEND, reviewable=True)
    assert not decide(call(send, {}), send, policy, ctx()).reviewable


def test_ask_offers_the_profile_scopes():
    tool = meta()
    assert decide(call(tool, {}), tool, Policy(), ctx()).scopes == (Scope.ONCE, Scope.SESSION)
    assert decide(call(tool, {}), tool, load_policy(profile="strict"), ctx()).scopes == (Scope.ONCE,)


def test_security_digest_changes_with_security_fields_only():
    base = meta("demo_tool", Tier.ACT, ParamSpec("a"))
    assert security_digest(base) == security_digest(ToolMeta(**{**base.__dict__, "pack_version": "9.9"}))
    assert security_digest(base) != security_digest(ToolMeta(**{**base.__dict__, "tier": Tier.SEND}))
    assert security_digest(base) != security_digest(meta("demo_tool", Tier.ACT, ParamSpec("a", max_length=5)))


def test_shared_vectors_reproduce_canonical_form_and_key():
    for vector in json.loads(VECTORS.read_text()):
        assert canonical_json(vector["args"]).decode() == vector["canonical"]
        assert approval_key(vector["pack"], vector["tool"], vector["args"]) == vector["key"]


def test_rate_limiter_counts_session_tier_and_tool():
    rate = RateLimiter()
    rate.record(meta(), asked=True)
    assert rate.snapshot().counts == {"session": 1, "asks": 1, "act": 1, "tool:demo_tool": 1}
