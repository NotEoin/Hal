"""Tests for `halhub.policy.model`: the shared types and the rate syntax."""

from __future__ import annotations

import dataclasses

import pytest

from halhub import _policy_standin
from halhub.policy.model import ParamSpec, Rate, RateSettings, Taint, Tier, ToolMeta


@pytest.mark.parametrize(
    ("text", "count", "window"), [("30/h", 30, 3600), ("5/s", 5, 1), ("2000/d", 2000, 86400)]
)
def test_rate_parses_count_and_unit(text, count, window):
    rate = Rate.parse(text)
    assert (rate.count, rate.window_s) == (count, window)
    assert str(rate) == text


@pytest.mark.parametrize("text", ["30", "0/h", "30/w", "-1/h", "30 per hour", "1.5/h"])
def test_rate_refuses_anything_but_count_slash_unit(text):
    with pytest.raises(ValueError, match="rate"):
        Rate.parse(text)


def test_rate_defaults_match_the_security_spec():
    assert {f.name: str(getattr(RateSettings(), f.name)) for f in dataclasses.fields(RateSettings)} == {
        "session": "2000/h",
        "asks": "40/h",
        "act": "120/h",
        "send": "30/h",
        "system": "10/h",
    }


def test_param_defaults_are_a_plain_string_of_at_most_65536_characters():
    assert ParamSpec("x") == ParamSpec("x", "string", False, None, 65536, "text", "read", False)


def test_taint_orders_from_clean_to_flagged_and_has_lowercase_labels():
    assert Taint.CLEAN < Taint.UNTRUSTED < Taint.FLAGGED
    assert [t.label for t in Taint] == ["clean", "untrusted", "flagged"]


def test_phase_one_stand_in_shares_the_policy_types():
    assert _policy_standin.ToolMeta is ToolMeta and _policy_standin.Tier is Tier
