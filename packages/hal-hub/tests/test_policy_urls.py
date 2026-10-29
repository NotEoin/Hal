"""Tests for the policy's URL and domain rules (REQ-SEC-10), including attack cases ATK-23 and ATK-42.

`decide` checks URLs without DNS: the purity test in `test_policy_decide.py` covers that, and
here a name that would resolve to a private address is still only judged by its text.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from halhub.policy import decide, load_policy
from halhub.policy.model import (
    Context,
    Origin,
    ParamSpec,
    RateSnapshot,
    Taint,
    Tier,
    ToolCall,
    ToolMeta,
    Verdict,
)

WEB_READ = ToolMeta(
    "web_read", "online", "0.1.0", Tier.SEND, (ParamSpec("url", format="url", required=True),)
)
FEEDS_SAVE = ToolMeta(
    "feeds_save",
    "online",
    "0.1.0",
    Tier.WRITE,
    (ParamSpec("url", format="url", required=True),),
    network=True,
)
PING = ToolMeta(
    "net_ping", "system", "0.1.0", Tier.SYSTEM, (ParamSpec("host", format="host", required=True),)
)
DOCS = ToolMeta(
    "docs_fetch",
    "online",
    "0.1.0",
    Tier.SEND,
    (ParamSpec("url", format="url", required=True),),
    domains=("docs.python.org",),
)
CTX = Context(
    now=datetime(2026, 10, 29, tzinfo=UTC),
    session_kind="chat",
    taint=Taint.CLEAN,
    grants=(),
    rate=RateSnapshot(),
    approver_available=True,
    paths={},
    workspaces=(),
    home=Path("/home/u"),
)


def policy(tmp_path: Path, text: str = "", **kw: object):
    path = tmp_path / "policy.toml"
    path.write_text("schema_version = 1\n" + text)
    path.chmod(0o600)
    return load_policy(path, **kw)


def run(meta: ToolMeta, value: str, pol, param: str = "url"):
    return decide(
        ToolCall(meta.name, {param: value}, "0123456789ab", Origin.TUI, "c_" + "0" * 16), meta, pol, CTX
    )


@pytest.mark.parametrize(
    ("url", "rule"),
    [
        ("file:///etc/passwd", "url:scheme"),
        ("http://127.0.0.1:8740/api/ask", "url:scheme"),
        ("https://127.0.0.1:8740/api/ask", "url:ip-literal"),
        ("https://2130706433/", "url:ip-literal"),
        ("https://[::ffff:127.0.0.1]/", "url:ip-literal"),
        ("https://u:p@example.org/", "url:userinfo"),
        ("https://printer.local/", "url:private"),
        ("https://intranet/", "url:private"),
    ],
)
def test_ssrf_and_file_urls_are_denied_before_any_lookup_atk_23(tmp_path, url, rule):
    d = run(WEB_READ, url, policy(tmp_path))
    assert (d.verdict, d.rule) == (Verdict.DENY, rule)


def test_allowed_scheme_and_literal_settings_only_relax_their_rule(tmp_path):
    loose = policy(tmp_path, "[egress]\nallow_http = true\nallow_ip_literals = true\n")
    assert run(WEB_READ, "http://example.org/", loose).rule == "tier:send"
    assert run(WEB_READ, "https://10.0.0.5/", loose).rule == "url:private"


def test_listed_domain_turns_ask_into_allow(tmp_path):
    pol = policy(tmp_path, "[tools.web_read]\ndomains = ['docs.python.org']\n")
    d = run(WEB_READ, "https://docs.python.org/3/", pol)
    assert (d.verdict, d.rule) == (Verdict.ALLOW, "domain-allowlist")


def test_global_and_pack_declared_domains_count_too(tmp_path):
    assert run(
        WEB_READ, "https://x.example.com/", policy(tmp_path, "[egress]\ndomains = ['*.example.com']\n")
    ).rule == ("domain-allowlist")
    assert run(DOCS, "https://docs.python.org/3/", policy(tmp_path)).rule == "domain-allowlist"


def test_unlisted_host_keeps_ask_with_the_full_url_atk_42(tmp_path):
    d = run(WEB_READ, "https://evil.example/?q=c4n4ry", policy(tmp_path))
    assert (d.verdict, d.rule, d.args["url"]) == (Verdict.ASK, "tier:send", "https://evil.example/?q=c4n4ry")


def test_unlisted_host_turns_an_allowed_tool_into_ask(tmp_path):
    d = run(FEEDS_SAVE, "https://feeds.example/rss", policy(tmp_path))
    assert (d.verdict, d.rule) == (Verdict.ASK, "url:unlisted")


def test_unlisted_host_is_denied_when_egress_says_so(tmp_path):
    assert run(WEB_READ, "https://evil.example/", policy(tmp_path, "[egress]\nunlisted = 'deny'\n")).rule == (
        "url:unlisted"
    )
    strict = policy(tmp_path, "", profile="strict")
    assert run(WEB_READ, "https://evil.example/", strict).verdict is Verdict.DENY


def test_listed_host_never_allows_a_system_tool(tmp_path):
    pol = policy(tmp_path, "[egress]\ndomains = ['example.org']\n")
    d = run(PING, "example.org", pol, param="host")
    assert (d.verdict, d.rule) == (Verdict.ASK, "tier:system")


@pytest.mark.parametrize("host", ["example.org/x", "u@example.org", "", "localhost", "10.0.0.1"])
def test_host_arguments_get_the_same_checks(tmp_path, host):
    assert run(PING, host, policy(tmp_path), param="host").verdict is Verdict.DENY


def test_flagged_turn_still_asks_for_a_listed_send(tmp_path):
    pol = policy(tmp_path, "[tools.web_read]\ndomains = ['docs.python.org']\n")
    meta = WEB_READ
    call = ToolCall(
        meta.name, {"url": "https://docs.python.org/"}, "0123456789ab", Origin.TUI, "c_" + "0" * 16
    )
    d = decide(call, meta, pol, replace(CTX, taint=Taint.FLAGGED))
    assert (d.verdict, d.rule) == (Verdict.ASK, "flagged-turn")
