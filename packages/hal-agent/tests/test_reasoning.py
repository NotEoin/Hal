"""Tests for the inline reasoning splitter."""

from __future__ import annotations

import pytest

from halagent.reasoning import MARKERS, MAX_HOLD, ReasoningSplitter, split_text


def _run(chunks: list[str]) -> tuple[str, str]:
    sp = ReasoningSplitter()
    pieces = []
    for c in chunks:
        pieces += sp.feed(c)
    pieces += sp.finish()
    return (
        "".join(p.text for p in pieces if not p.reasoning),
        "".join(p.text for p in pieces if p.reasoning),
    )


@pytest.mark.parametrize("family", sorted(MARKERS))
def test_each_family_splits_reasoning_from_visible_text(family):
    start, end = MARKERS[family]
    assert split_text(f"{start}plan it{end}Done.") == ("Done.", "plan it")


@pytest.mark.parametrize("family", sorted(MARKERS))
def test_marker_split_across_every_chunk_edge_is_still_found(family):
    start, end = MARKERS[family]
    text = f"Hi {start}secret{end} bye"
    for cut in range(1, len(text)):
        assert _run([text[:cut], text[cut:]]) == ("Hi  bye", "secret"), cut


def test_one_character_chunks_give_the_same_result_as_one_chunk():
    text = "a<think>b</think>c<|channel>thoughtd<channel|>e"
    assert _run(list(text)) == split_text(text) == ("ace", "bd")


def test_unclosed_block_stays_reasoning_and_visible_text_is_empty():
    assert split_text("<think>still going") == ("", "still going")


def test_lone_angle_bracket_at_the_end_is_released_by_finish():
    assert split_text("a < b <") == ("a < b <", "")


def test_text_without_markers_passes_through_with_bounded_delay():
    sp = ReasoningSplitter()
    out = sp.feed("x" * 100)
    assert len("".join(p.text for p in out)) >= 100 - MAX_HOLD


def test_plain_text_is_emitted_at_once_when_no_marker_prefix_ends_it():
    sp = ReasoningSplitter()
    assert "".join(p.text for p in sp.feed("hello")) == "hello"


def test_unknown_family_is_refused():
    with pytest.raises(ValueError, match="family"):
        ReasoningSplitter(("nope",))


def test_longest_marker_fits_the_hold_limit():
    assert max(len(m) for pair in MARKERS.values() for m in pair) <= MAX_HOLD
