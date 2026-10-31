"""Tests for `halhub.policy.profiles`: built-in and channel profiles, hard-deny lists."""

from __future__ import annotations

from pathlib import Path

import pytest

from halhub.policy.model import Scope, Tier, Verdict
from halhub.policy.profiles import (
    CHANNEL_PROFILES,
    HARD_DENY_GLOBS,
    HARD_DENY_PATHS,
    PROFILES,
    PROMPT_SCOPES,
    hard_deny_paths,
)

A, Q, D = Verdict.ALLOW, Verdict.ASK, Verdict.DENY
# 01-security "Tool metadata the policy reads", tier table: read, write, act, send, system
TIER_TABLE = {
    "default": (A, A, Q, Q, Q),
    "strict": (A, Q, Q, Q, D),
    "trusted": (A, A, A, Q, Q),
    "standalone": (A, A, Q, Q, D),
}


@pytest.mark.parametrize("name", sorted(TIER_TABLE))
def test_built_in_profiles_match_the_tier_table(name):
    assert tuple(PROFILES[name].tiers[t] for t in Tier) == TIER_TABLE[name]


def test_no_built_in_profile_allows_the_system_tier_or_offers_a_routine_prompt():
    for profile in PROFILES.values():
        assert profile.tiers[Tier.SYSTEM] is not A
        assert set(profile.scopes) <= PROMPT_SCOPES


def test_standalone_is_default_minus_the_system_tier():
    default, standalone = PROFILES["default"], PROFILES["standalone"]
    assert {**default.tiers, Tier.SYSTEM: D} == dict(standalone.tiers)
    assert (standalone.act_reversible, standalone.scopes, standalone.tools) == (
        default.act_reversible,
        default.scopes,
        default.tools,
    )


def test_strict_requires_a_container_and_tightens_egress_and_guard():
    strict = PROFILES["strict"]
    assert strict.sandbox_require == "container"  # REQ-SEC-102
    assert (strict.egress_unlisted, strict.guard_on_flag) == ("deny", "deny")
    assert strict.act_reversible is Q and strict.scopes == (Scope.ONCE,) and not strict.reviewer
    assert all(p.sandbox_require == "any" for n, p in PROFILES.items() if n != "strict")


def test_default_allows_the_three_fixed_destination_sends_and_strict_asks():
    for name in ("notify_send", "briefing_deliver", "calendar_sync"):
        assert PROFILES["default"].tools[name].verdict is A
        assert PROFILES["strict"].tools[name].verdict is Q


@pytest.mark.parametrize("name", ["calendar_delete", "tasks_delete", "habits_remove", "desktop_clipboard"])
def test_shipped_tightenings_ask_in_every_profile(name):
    assert all(p.tools[name].verdict is Q for p in PROFILES.values())


@pytest.mark.parametrize("channel", sorted(CHANNEL_PROFILES))
def test_channel_profiles_deny_system_and_offer_only_once(channel):
    profile = CHANNEL_PROFILES[channel]
    assert Tier.SYSTEM in profile.denied_tiers and profile.scopes == (Scope.ONCE,)


def test_channel_specifics_follow_the_spec():
    assert CHANNEL_PROFILES["voice"].approval_timeout_s == 20
    assert CHANNEL_PROFILES["telegram"].approval_timeout_s == 600
    assert CHANNEL_PROFILES["remote"].denied_packs == {"desktop"}


def test_hard_deny_paths_expand_home_and_the_runtime_dir():
    home = Path("/home/u")
    paths = hard_deny_paths(home=home, environ={"XDG_RUNTIME_DIR": "/run/user/1000"})
    assert Path("/home/u/.ssh") in paths and Path("/home/u/.config/hal") in paths
    assert Path("/run/user/1000") in paths and Path("/etc/shadow") in paths


def test_unset_runtime_dir_is_left_out_rather_than_denying_the_root():
    paths = hard_deny_paths(home=Path("/home/u"), environ={})
    assert Path("/") not in paths and Path("") not in paths


def test_hard_deny_globs_cover_keys_and_env_files():
    assert {".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.kdbx"} <= set(HARD_DENY_GLOBS)


@pytest.mark.parametrize(
    "entry",
    [
        "~/.bash_history",
        "~/.zsh_history",
        "~/.git-credentials",
        "~/.npmrc",
        "~/.config/gcloud",
        "~/.local/share/kwalletd",
        "~/.var/app/org.mozilla.firefox",
        "~/snap/firefox",
        "~/.config/vivaldi",
        "~/.config/KeePassXC",
    ],
)
def test_hard_deny_covers_history_cloud_keyring_and_browser_profile_paths(entry):
    # review gap: these credential and history stores were not in the built-in list
    assert entry in HARD_DENY_PATHS
    assert Path("/home/u" + entry[1:]) in hard_deny_paths(home=Path("/home/u"), environ={})


def test_hard_deny_paths_have_no_duplicates():
    assert len(set(HARD_DENY_PATHS)) == len(HARD_DENY_PATHS)
