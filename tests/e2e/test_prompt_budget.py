"""Scripted sessions S1 to S5 under the `default` profile raise at most 0, 0, 0, 0, 1 prompts.

01-security REQ-SEC-91 and "Prompt budget". Hermetic: the model is a fixed list of tool calls,
the packs are synthetic `ToolMeta` values with the tiers of 08's per-tool table, and the
verdicts come from the real `decide` over the real `default` profile. A tier or default change
that adds a prompt fails here. In-box work (S1, S2) never reaches the hub, so it costs nothing.
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

SESSION = "0123456789ab"
TEXT = (ParamSpec("text"),)
URL = (ParamSpec("url", format="url", required=True),)


def tool(name: str, tier: Tier, params=TEXT, **flags) -> ToolMeta:
    return ToolMeta(name, "synthetic", "0.1.0", tier, params, **flags)


CATALOGUE = {
    m.name: m
    for m in (
        tool("calendar_agenda", Tier.READ),
        tool("calendar_add", Tier.WRITE),
        tool("calendar_sync", Tier.SEND),
        tool("mail_unread", Tier.READ),
        tool("mail_read", Tier.READ),
        tool("mail_summarise", Tier.READ),
        tool("mail_draft", Tier.ACT, reversible=True),
        tool("web_search", Tier.READ, network=True, untrusted_output=True),
        tool("web_read", Tier.SEND, URL, network=True, untrusted_output=True),
        tool("memory_remember", Tier.WRITE),
    )
}
RESULTS = [f"https://example.org/result-{i}" for i in range(4)]
SEARCH_RESULT_TEXT = "\n".join(RESULTS)
SCRIPTS: dict[str, list[tuple[str, dict]]] = {
    "S1": [],  # read, grep, edit and run happen in the box; nothing reaches the hub
    "S2": [],  # the workspace scan flag on a changed Makefile is advice, not a prompt
    "S3": [
        ("calendar_agenda", {"text": "tomorrow"}),
        ("calendar_add", {"text": "dentist"}),
        ("calendar_sync", {"text": ""}),  # a send tool, allowed by name (fixed destination)
    ],
    "S4": [("mail_unread", {}), ("mail_read", {"text": "1"}), ("mail_summarise", {"text": "1"})]
    + [("mail_draft", {"text": f"reply {i}"}) for i in range(3)],
    "S5": [("web_search", {"text": "x"})]
    + [("web_read", {"url": u}) for u in RESULTS]
    + [("web_read", {"url": "https://other.example.net/page"}), ("memory_remember", {"text": "notes"})],
}
BUDGET = {"S1": 0, "S2": 0, "S3": 0, "S4": 0, "S5": 1}


@pytest.fixture
def default_policy(tmp_path: Path):
    path = tmp_path / "policy.toml"
    path.write_text("schema_version = 1\n")
    path.chmod(0o600)
    return load_policy(path)


def prompts(script: list[tuple[str, dict]], pol) -> int:
    urls = SearchUrls()
    urls.record(SESSION, SEARCH_RESULT_TEXT)
    ctx = Context(
        now=datetime(2026, 10, 30, tzinfo=UTC),
        session_kind="chat",
        taint=Taint.CLEAN,
        grants=(),
        rate=RateSnapshot(),
        approver_available=True,
        paths={},
        workspaces=(),
        home=Path("/home/u"),
    )
    count = 0
    for i, (name, args) in enumerate(script):
        call = ToolCall(name, args, SESSION, Origin.TUI, f"c_{i:016x}")
        verdict = decide(call, CATALOGUE[name], pol, replace(ctx, search_urls=urls.urls(SESSION))).verdict
        count += verdict is Verdict.ASK
        assert verdict is not Verdict.DENY, f"{name} was denied"
    return count


@pytest.mark.parametrize("session", sorted(BUDGET))
def test_scripted_session_stays_within_its_prompt_budget(session, default_policy):
    assert prompts(SCRIPTS[session], default_policy) <= BUDGET[session]


def test_s5_asks_exactly_for_the_url_that_was_not_in_the_results(default_policy):
    assert prompts(SCRIPTS["S5"], default_policy) == 1
