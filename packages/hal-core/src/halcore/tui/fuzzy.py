"""Fuzzy matching for pickers, completions and the palette: subsequence matches, ranked.

Pure and Textual-free, so the line UI and tests use it without a screen. A match is a
subsequence of the text, ignoring case; `score` also returns the matched indices so a list can
highlight them. Ranking favours what people mean when they type a few letters: word starts
(`mc` for `model cycle`), runs of consecutive letters, and hits inside a path's basename. Ties
keep the caller's order, so a list that is already sorted (newest first) stays that way.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from itertools import pairwise
from typing import TypeVar

T = TypeVar("T")

WORD_START = 8  # a letter at the start of a word or path part
CONSECUTIVE = 5  # a letter right after the previous match
BASENAME = 3  # per letter inside the last path part
PREFIX = 10  # the whole text starts with the query
GAP = 1  # per skipped character between the first and the last match
COVERAGE = 4  # at most this much more for a query that covers more of the basename (`calc.py` over `calc-notes.md`)
SEPARATORS = frozenset(" /_-.:@")


def _word_start(text: str, i: int) -> bool:
    return i == 0 or text[i - 1] in SEPARATORS or (text[i].isupper() and text[i - 1].islower())


def _fits(low: str, query: str, start: int) -> bool:
    """True when `query` is a subsequence of `low[start:]`."""
    pos = start
    for ch in query:
        pos = low.find(ch, pos)
        if pos < 0:
            return False
        pos += 1
    return True


def score(query: str, text: str) -> tuple[int, list[int]] | None:
    """`(score, indices)` of the best simple match of `query` in `text`, or None when it does not match.

    Each query letter takes the next word-start or consecutive occurrence when the rest of the
    query still fits after it, else the next occurrence. An empty query matches with score 0.
    """
    if not query:
        return 0, []
    low, q = text.lower(), query.lower()
    if not _fits(low, q, 0):
        return None
    indices: list[int] = []
    pos = 0
    for n, ch in enumerate(q):
        first = low.find(ch, pos)
        best = first
        i = first
        while i >= 0:
            good = _word_start(text, i) or (bool(indices) and i == indices[-1] + 1)
            if good and _fits(low, q[n + 1 :], i + 1):
                best = i
                break
            i = low.find(ch, i + 1)
        indices.append(best)
        pos = best + 1
    base = text.rfind("/") + 1
    total = sum(WORD_START for i in indices if _word_start(text, i))
    total += sum(CONSECUTIVE for a, b in pairwise(indices) if b == a + 1)
    total += sum(BASENAME for i in indices if i >= base) if base else 0
    total += PREFIX if low.startswith(q) else 0
    total -= GAP * (indices[-1] - indices[0] + 1 - len(indices))
    total += round(COVERAGE * sum(1 for i in indices if i >= base) / max(1, len(text) - base))
    return total, indices


def rank(query: str, items: Iterable[T], key: Callable[[T], str] = str, limit: int | None = None) -> list[T]:
    """The items whose `key` matches `query`, best first; ties keep their order. An empty query keeps all."""
    scored = []
    for n, item in enumerate(items):
        hit = score(query, key(item))
        if hit is not None:
            scored.append((-hit[0], n, item))
    scored.sort(key=lambda row: (row[0], row[1]))
    out = [item for _, _, item in scored]
    return out if limit is None else out[:limit]
