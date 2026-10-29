"""Tests for the policy's URL and domain rules (REQ-SEC-10), including attack cases ATK-23 and ATK-42,
and the `url:from-search` provenance rule (REQ-SEC-92).

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
from halhub.policy.provenance import SearchUrls

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


# ---- url:from-search (REQ-SEC-92) -------------------------------------------------------------

SEARCH_RESULT = (
    "1. Example A - https://Example.org:443/a?q=1#top\n"
    "2. Form - https://docs.google.com/forms/d/x/formResponse?entry.1=hi\n"
)
SYSTEM_FETCH = ToolMeta(
    "web_read", "system", "0.1.0", Tier.SYSTEM, (ParamSpec("url", format="url", required=True),)
)


def search_ctx(session: str = "0123456789ab", text: str = SEARCH_RESULT, **kw: object) -> Context:
    urls = SearchUrls()
    urls.record(session, text)
    return replace(CTX, search_urls=urls.urls("0123456789ab"), **kw)


def run_ctx(meta: ToolMeta, value: str, pol, ctx: Context):
    call = ToolCall(meta.name, {"url": value}, "0123456789ab", Origin.TUI, "c_" + "0" * 16)
    return decide(call, meta, pol, ctx)


def test_search_result_url_is_allowed_once_canonical(tmp_path):
    d = run_ctx(WEB_READ, "https://example.org/a?q=1", policy(tmp_path), search_ctx())
    assert (d.verdict, d.rule) == (Verdict.ALLOW, "url:from-search")


def test_url_differing_in_path_or_query_still_asks(tmp_path):
    pol = policy(tmp_path)
    for url in ("https://example.org/a?q=2", "https://example.org/A?q=1", "https://example.org/a"):
        d = run_ctx(WEB_READ, url, pol, search_ctx())
        assert (d.verdict, d.rule) == (Verdict.ASK, "tier:send"), url


def test_url_from_other_session_still_asks(tmp_path):
    d = run_ctx(WEB_READ, "https://example.org/a?q=1", policy(tmp_path), search_ctx(session="ba9876543210"))
    assert (d.verdict, d.rule) == (Verdict.ASK, "tier:send")


def test_excluded_host_still_asks(tmp_path):
    url = "https://docs.google.com/forms/d/x/formResponse?entry.1=hi"
    d = run_ctx(WEB_READ, url, policy(tmp_path), search_ctx())
    assert (d.verdict, d.rule) == (Verdict.ASK, "tier:send")


def test_wildcard_exclusion_covers_subdomains(tmp_path):
    pol = policy(tmp_path, "[egress]\nfrom_search_exclude = ['*.example.org']\n")
    ctx = search_ctx(text="https://www.example.org/x")
    d = run_ctx(WEB_READ, "https://www.example.org/x", pol, ctx)
    assert (d.verdict, d.rule) == (Verdict.ASK, "tier:send")


def test_tool_not_listed_in_from_search_still_asks(tmp_path):
    pol = policy(tmp_path, "[egress]\nfrom_search = []\n")
    d = run_ctx(WEB_READ, "https://example.org/a?q=1", pol, search_ctx())
    assert (d.verdict, d.rule) == (Verdict.ASK, "tier:send")


def test_search_result_never_allows_a_system_tool(tmp_path):
    d = run_ctx(SYSTEM_FETCH, "https://example.org/a?q=1", policy(tmp_path), search_ctx())
    assert d.verdict is Verdict.ASK
    assert d.rule != "url:from-search"


def test_flagged_turn_asks_even_for_a_search_result(tmp_path):
    ctx = search_ctx(taint=Taint.FLAGGED)
    d = run_ctx(WEB_READ, "https://example.org/a?q=1", policy(tmp_path), ctx)
    assert (d.verdict, d.rule, d.fresh_only) == (Verdict.ASK, "flagged-turn", True)


def test_search_result_does_not_bypass_url_checks(tmp_path):
    ctx = search_ctx(text="http://example.org/a")
    d = run_ctx(WEB_READ, "http://example.org/a", policy(tmp_path), ctx)
    assert (d.verdict, d.rule) == (Verdict.DENY, "url:scheme")


def test_search_result_does_not_beat_unlisted_deny(tmp_path):
    pol = policy(tmp_path, "[egress]\nunlisted = 'deny'\n")
    d = run_ctx(WEB_READ, "https://example.org/a?q=1", pol, search_ctx())
    assert (d.verdict, d.rule) == (Verdict.DENY, "url:unlisted")
