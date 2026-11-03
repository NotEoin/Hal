"""Tests for `halcore.tui.fuzzy`: subsequence matching, highlight indices and ranking."""

from halcore.tui import fuzzy


def test_subsequence_matches_and_returns_spans():
    hit = fuzzy.score("cmp", "/compact")
    assert hit is not None
    _, indices = hit
    assert ["/compact"[i] for i in indices] == ["c", "m", "p"]


def test_match_ignores_case():
    assert fuzzy.score("README", "docs/readme.md") is not None


def test_no_match_returns_none():
    assert fuzzy.score("xyz", "/compact") is None
    assert fuzzy.score("tc", "ct") is None


def test_empty_query_matches_everything_with_score_zero():
    assert fuzzy.score("", "anything") == (0, [])


def test_word_start_and_basename_matches_rank_first():
    paths = ["src/calculator/main.py", "docs/mcalc.txt", "src/calc.py"]
    assert fuzzy.rank("calc", paths)[0] == "src/calc.py"
    assert fuzzy.rank("mc", ["/compact", "/model cycle"])[0] == "/model cycle"


def test_word_start_is_preferred_when_the_rest_still_fits():
    _, indices = fuzzy.score("mc", "/model cycle")
    assert indices == [1, 7]


def test_rank_keeps_order_on_ties_and_honours_the_limit():
    items = ["b1", "a1", "c1"]
    assert fuzzy.rank("", items) == items
    assert fuzzy.rank("1", items, limit=2) == ["b1", "a1"]


def test_rank_uses_the_key():
    rows = [("x", "/theme"), ("y", "/model")]
    assert fuzzy.rank("mod", rows, key=lambda r: r[1]) == [("y", "/model")]
