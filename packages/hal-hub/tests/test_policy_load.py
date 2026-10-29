"""Tests for `halhub.policy.load`: schema, defaults, precedence, invariants and file modes.

REQ-SEC-07 (precedence), REQ-SEC-08 (invariants), REQ-SEC-14 (errors), REQ-SEC-15 (file
safety) and REQ-SEC-102 (strict needs a container).
"""

from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path

import pytest

from halhub.policy import ConfigError, Policy, check_rules, load_policy
from halhub.policy.model import (
    ApprovalSettings,
    AuditSettings,
    EgressSettings,
    GuardSettings,
    ParamSpec,
    PathSettings,
    RateSettings,
    ReviewerSettings,
    Scope,
    Tier,
    ToolMeta,
    Verdict,
)
from halhub.policy.profiles import TEMPLATE


def write(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / "policy.toml"
    path.write_text("schema_version = 1\n" + text)
    path.chmod(mode)
    return path


def meta(name: str = "lights_on", tier: Tier = Tier.ACT, pack: str = "desktop", **kw: object) -> ToolMeta:
    return ToolMeta(name=name, pack=pack, pack_version="0.1.0", tier=tier, params=(ParamSpec("x"),), **kw)


def test_missing_file_means_the_built_in_default_profile(tmp_path):
    policy = load_policy(tmp_path / "absent.toml")
    assert policy.profile.name == "default" and policy.path is None and policy.channel is None


def test_every_setting_defaults_to_the_spec_value(tmp_path):
    policy = load_policy(tmp_path / "absent.toml")
    assert (policy.approval, policy.audit, policy.guard, policy.reviewer) == (
        ApprovalSettings(120, 600, 12, 90, True),
        AuditSettings(False, True, 90, 64),
        GuardSettings(False, "prompt-guard-2-86m", 0.5, 24000, 15, 2, "ask", "auto"),
        ReviewerSettings(False, "hal-fast", 20, True),
    )
    assert policy.paths == PathSettings(
        ("~/Documents", "~/Downloads", "~/Desktop", "~/Pictures", "~/Music", "~/Videos"), ("~/Downloads",), ()
    )
    assert policy.egress == EgressSettings(
        (), "ask", False, False, False, 3, ("web_search",), ("web_read",), ("docs.google.com", "forms.gle")
    )
    assert policy.rate == RateSettings()


def test_settings_are_read_and_typed(tmp_path):
    path = write(
        tmp_path,
        '[approval]\ntimeout_s = 30\n[egress]\ndomains = ["Docs.Python.org", "*.example.com"]\n'
        '[rate]\nsend = "5/m"\n[guard]\nthreshold = 1\n',
    )
    policy = load_policy(path)
    assert policy.approval.timeout_s == 30
    assert policy.egress.domains == ("docs.python.org", "*.example.com")
    assert str(policy.rate.send) == "5/m" and policy.guard.threshold == 1.0


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("bogus = 1\n", "bogus"),
        ("[approval]\nbogus = 1\n", "approval.bogus"),
        ("[approval]\ntimeout_s = 5\n", "approval.timeout_s"),
        ("[approval]\ntimeout_s = true\n", "approval.timeout_s"),
        ("[audit]\nmax_file_mb = 2048\n", "audit.max_file_mb"),
        ("[guard]\non_flag = 'maybe'\n", "guard.on_flag"),
        ("[guard]\nthreshold = 1.5\n", "guard.threshold"),
        ("[egress]\nunlisted = 'allow'\n", "egress.unlisted"),
        ("[egress]\ndomains = ['bad host']\n", "egress.domains"),
        ("[rate]\nsend = 'lots'\n", "rate.send"),
        ("[tools.x]\nverdict = 'yes'\n", "tools.x.verdict"),
        ("[tools.x]\ndefault = 'allow'\n", "tools.x.default"),
        ("[packs.p]\nrate = '1/h'\n", "packs.p.rate"),
        ("[tools.x]\nallow_args = { to = 'a' }\n", "tools.x.allow_args.to"),
        ("profile = 'nope'\n", "profile"),
        ("[profiles.a]\nscopes = ['always']\n", "profiles.a.scopes"),
        ("[profiles.a]\nscopes = []\n", "profiles.a.scopes"),
        ("[profiles.a]\ntiers = { magic = 'allow' }\n", "profiles.a.tiers.magic"),
        ("[profiles.a]\nextends = 'nope'\n", "profiles.a.extends"),
        ("[profiles.default]\nextends = 'strict'\n", "profiles.default.extends"),
        ("[profiles.a]\ncolour = 'red'\n", "profiles.a.colour"),
    ],
)
def test_invalid_files_raise_config_error_naming_the_key(tmp_path, text, key):
    with pytest.raises(ConfigError) as info:
        load_policy(write(tmp_path, text))
    assert info.value.key == key and str(tmp_path) in str(info.value)


def test_error_names_the_line_of_the_bad_key(tmp_path):
    path = write(tmp_path, "[approval]\nnotify = true\ntimeout_s = 1\n")
    with pytest.raises(ConfigError) as info:
        load_policy(path)
    assert info.value.line == 4 and re.search(r"policy\.toml:4: approval\.timeout_s", str(info.value))


def test_malformed_toml_raises_with_its_line(tmp_path):
    with pytest.raises(ConfigError, match="malformed"):
        load_policy(write(tmp_path, "[approval\n"))


def test_schema_version_is_required_when_the_file_has_keys(tmp_path):
    path = tmp_path / "policy.toml"
    path.write_text("profile = 'strict'\n")
    path.chmod(0o600)
    with pytest.raises(ConfigError) as info:
        load_policy(path)
    assert info.value.key == "schema_version"


def test_unknown_channel_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="unknown channel"):
        load_policy(tmp_path / "absent.toml", channel="email")


def test_profile_cycle_is_refused(tmp_path):
    path = write(tmp_path, "[profiles.a]\nextends = 'b'\n[profiles.b]\nextends = 'a'\n")
    with pytest.raises(ConfigError, match="cycle"):
        load_policy(path)


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_policy_file_writable_by_others_is_refused(tmp_path, mode):
    with pytest.raises(ConfigError, match="chmod 600"):
        load_policy(write(tmp_path, "", mode=mode))


def test_policy_file_readable_by_group_is_accepted(tmp_path):
    assert load_policy(write(tmp_path, "", mode=0o640)).path is not None


@pytest.mark.skipif(os.getuid() == 0, reason="root owns everything it creates")
def test_policy_file_owned_by_another_user_is_refused(tmp_path, monkeypatch):
    path = write(tmp_path, "")
    monkeypatch.setattr(os, "getuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(ConfigError, match="owned by uid"):
        load_policy(path)


# ---- precedence (REQ-SEC-07), one case per level ---------------------------------------------


def test_override_precedence_tier_then_act_reversible(tmp_path):
    policy = load_policy(tmp_path / "absent.toml")
    assert (policy.tool(meta()).verdict, policy.tool(meta()).rule) == (Verdict.ASK, "tier:act")
    reversible = policy.tool(meta(reversible=True))
    assert (reversible.verdict, reversible.rule) == (Verdict.ALLOW, "act-reversible")


def test_override_precedence_profile_built_in_entry(tmp_path):
    tp = load_policy(tmp_path / "absent.toml").tool(meta("calendar_delete", Tier.WRITE, "organiser"))
    assert (tp.verdict, tp.rule) == (Verdict.ASK, "profile-tool:calendar_delete@default")


@pytest.mark.parametrize(
    ("text", "verdict", "rule"),
    [
        ("[packs.desktop]\nverdict = 'deny'\n", "deny", "pack:desktop"),
        (
            "[packs.desktop]\nverdict = 'deny'\n[tools.lights_on]\nverdict = 'allow'\n",
            "allow",
            "tool:lights_on",
        ),
        (
            "[tools.lights_on]\nverdict = 'allow'\n[profiles.default.packs.desktop]\nverdict = 'deny'\n",
            "deny",
            "profile-pack:desktop@default",
        ),
        (
            "[profiles.default.packs.desktop]\nverdict = 'deny'\n"
            "[profiles.default.tools.lights_on]\nverdict = 'allow'\n",
            "allow",
            "profile-tool:lights_on@default",
        ),
    ],
    ids=["pack", "tool-over-pack", "profile-pack-over-tool", "profile-tool-over-profile-pack"],
)
def test_override_precedence(tmp_path, text, verdict, rule):
    tp = load_policy(write(tmp_path, text)).tool(meta())
    assert (tp.verdict, tp.rule) == (Verdict(verdict), rule)


def test_override_precedence_channel_only_tightens(tmp_path):
    policy = load_policy(write(tmp_path, "[packs.desktop]\nverdict = 'allow'\n"), channel="remote")
    tp = policy.tool(meta())
    assert (tp.verdict, tp.rule) == (Verdict.DENY, "pack:desktop@remote")
    voice = load_policy(tmp_path / "absent.toml", channel="voice")
    system = voice.tool(meta("power_shutdown", Tier.SYSTEM, "system"))
    assert (system.verdict, system.rule) == (Verdict.DENY, "tier:system@voice")
    assert voice.approval.timeout_s == 20 and voice.allowed_scopes(meta()) == (Scope.ONCE,)


def test_custom_profile_extends_and_inherits_user_tables(tmp_path):
    text = (
        "profile = 'mine'\n[profiles.base]\nextends = 'trusted'\n"
        "[profiles.base.tools.lights_on]\nverdict = 'deny'\n"
        "[profiles.mine]\nextends = 'base'\nscopes = ['once']\n"
    )
    policy = load_policy(write(tmp_path, text))
    assert policy.profile.tiers[Tier.ACT] is Verdict.ALLOW and policy.profile.scopes == (Scope.ONCE,)
    assert policy.tool(meta()).rule == "profile-tool:lights_on@mine"


def test_tool_rule_settings_replace_path_roots_and_domains(tmp_path):
    text = "[tools.lights_on]\nrate = '3/h'\nread_roots = ['~/Music']\ndomains = ['example.org']\n"
    tp = load_policy(write(tmp_path, text)).tool(meta(domains=("hue.example",)))
    assert (str(tp.rate), tp.read_roots, tp.domains) == ("3/h", ("~/Music",), ("example.org",))


# ---- invariants (REQ-SEC-08) ------------------------------------------------------------------


def test_invariant_system_tier_allow_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as info:
        load_policy(write(tmp_path, "[profiles.default]\ntiers = { system = 'allow' }\n"))
    assert info.value.key == "profiles.default.tiers.system"


def test_invariant_allow_rule_for_a_system_tool_is_rejected_atk_28(tmp_path):
    policy = load_policy(write(tmp_path, "[tools.power_shutdown]\nverdict = 'allow'\n"))
    shutdown = meta("power_shutdown", Tier.SYSTEM, "system")
    with pytest.raises(ConfigError) as info:
        check_rules(policy, [shutdown])
    assert info.value.key == "tools.power_shutdown.verdict"
    assert policy.tool(shutdown).verdict is Verdict.ASK  # clamped even if the check is skipped


def test_invariant_pack_allow_covering_a_system_tool_is_rejected(tmp_path):
    policy = load_policy(write(tmp_path, "[packs.system]\nverdict = 'allow'\n"))
    with pytest.raises(ConfigError, match="packs.system.verdict"):
        check_rules(policy, [meta("power_shutdown", Tier.SYSTEM, "system")])


def test_invariant_system_tool_is_never_reviewable(tmp_path):
    policy = load_policy(write(tmp_path, "[tools.power_shutdown]\nreviewable = true\n"))
    shutdown = meta("power_shutdown", Tier.SYSTEM, "system")
    with pytest.raises(ConfigError, match="reviewable"):
        check_rules(policy, [shutdown])
    assert policy.tool(shutdown).reviewable is False


def test_invariant_runs_code_tool_is_denied_unless_asked_for_by_name(tmp_path):
    shell = meta("shell_run", Tier.SYSTEM, "system", runs_code=True)
    assert load_policy(tmp_path / "absent.toml").tool(shell).rule == "runs-code"
    assert (
        load_policy(write(tmp_path, "[packs.system]\nverdict = 'ask'\n")).tool(shell).verdict is Verdict.DENY
    )
    asked = load_policy(write(tmp_path, "[tools.shell_run]\nverdict = 'ask'\n"))
    assert asked.tool(shell).verdict is Verdict.ASK
    assert (
        load_policy(write(tmp_path, "[tools.shell_run]\nverdict = 'ask'\n"), channel="voice")
        .tool(shell)
        .verdict
        is Verdict.DENY
    )


def test_invariant_runs_code_allow_rule_is_rejected(tmp_path):
    policy = load_policy(write(tmp_path, "[tools.test_run]\nverdict = 'allow'\n"))
    with pytest.raises(ConfigError, match="runs code"):
        check_rules(policy, [meta("test_run", Tier.ACT, "work", runs_code=True)])


def test_invariant_a_built_in_policy_object_cannot_allow_a_system_tool():
    tp = Policy(profile=load_policy(Path("/nonexistent/policy.toml"), profile="trusted").profile).tool(
        meta("power_shutdown", Tier.SYSTEM, "system")
    )
    assert tp.verdict is Verdict.ASK


# ---- profiles ----------------------------------------------------------------------------------


def test_strict_requires_container_backend(tmp_path):
    strict = load_policy(tmp_path / "absent.toml", profile="strict")
    assert strict.sandbox_require == "container"
    assert (strict.egress.unlisted, strict.guard.on_flag) == ("deny", "deny")
    assert load_policy(tmp_path / "absent.toml").sandbox_require == "any"


def test_strict_keeps_its_tightenings_against_a_looser_file(tmp_path):
    path = write(tmp_path, "profile = 'strict'\n[egress]\nunlisted = 'ask'\n[guard]\non_flag = 'ask'\n")
    policy = load_policy(path)
    assert (policy.egress.unlisted, policy.guard.on_flag) == ("deny", "deny")


def test_shipped_profile_entries_apply_by_tool_name(tmp_path):
    default = load_policy(tmp_path / "absent.toml")
    send = meta("notify_send", Tier.SEND, "comms")
    assert (default.tool(send).verdict, default.tool(send).rule) == (
        Verdict.ALLOW,
        "profile-tool:notify_send@default",
    )
    assert load_policy(tmp_path / "absent.toml", profile="strict").tool(send).verdict is Verdict.ASK


def test_init_template_loads_as_is_and_with_every_line_uncommented(tmp_path):
    path = tmp_path / "policy.toml"
    path.write_text(TEMPLATE)
    path.chmod(0o600)
    as_is, built_in = load_policy(path), load_policy(tmp_path / "absent.toml")
    assert as_is.path == path
    assert dataclasses.replace(as_is, path=None) == built_in
    uncommented = re.sub(r"^# (?=[\[a-z])", "", TEMPLATE, flags=re.M)
    path.write_text(uncommented)
    policy = load_policy(path)
    assert policy.profile.name == "default" and "careful" not in policy.profile.name
    assert policy.tools["web_read"].domains == ("docs.python.org",)
