"""Ranks tools against a request so an assistant sees only the few it needs (04-hub.md, "Search").

The v1 algorithm, kept: tokens are `[a-z0-9]+` after lower-casing and replacing `_` with a
space; a tool's document is its name twice, its module name, its description, its keywords
and its module's keywords; scoring is Okapi BM25 with naive plural stripping, and matches
under `SCORE_FLOOR` of the best score are dropped. Stdlib only. Design decisions:

- The synonym table holds only generic rewrites that name no pack (REQ-HUB-34). A pack's
  vocabulary comes from its manifest `keywords`, so installing a pack brings its words and the
  hub knows no pack by name.
- The caller passes only the tools its session may use; this module knows nothing of policy.
- Ties sort by tool name, so results are deterministic.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence

from .manifest import ToolSpec

__all__ = ["B", "K1", "SCORE_FLOOR", "SYNONYMS", "rank"]

K1 = 1.2  # Okapi BM25 term-frequency saturation, the usual default
B = 0.75  # Okapi BM25 length normalisation, the usual default
SCORE_FLOOR = 0.25  # share of the best score; matches that weak came only from common words in v1 use
PLURAL_MIN_LENGTH = 4  # "bus" and "gas" are not plurals; v1 stripped only longer words

# Generic everyday phrasing -> the word tools use (04-hub "Search"). Never a pack or module name.
SYNONYMS: Mapping[str, str] = {
    "remind": "reminder",
    "appointment": "event",
    "meeting": "event",
    "todo": "task",
    "chore": "task",
    "search": "find",
    "color": "colour",
    "louder": "volume",
    "quieter": "volume",
    "recall": "remember",
}

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("_", " "))


def _expand(query: str) -> list[str]:
    """The query's words plus their singular forms and the synonyms of both."""
    words = _tokens(query)
    out = list(words)
    for word in words:
        forms = [word]
        if word.endswith("s") and len(word) >= PLURAL_MIN_LENGTH:
            forms.append(word[:-1])
            out.append(word[:-1])
        for form in forms:
            out += _tokens(SYNONYMS.get(form, ""))
    return out


def rank(
    query: str,
    tools: Sequence[ToolSpec],
    limit: int = 8,
    *,
    module_keywords: Mapping[str, Sequence[str]] | None = None,
) -> list[ToolSpec]:
    """Up to `limit` tools most relevant to `query`, best first; ties sort by name.

    `module_keywords` maps a module name to its manifest `keywords`, which count for each of
    its tools.
    """
    extra = module_keywords or {}
    docs = [
        _tokens(
            f"{t.name} {t.name} {t.module} {t.description} {' '.join(t.keywords)} "
            f"{' '.join(extra.get(t.module, ()))}"
        )
        for t in tools
    ]
    if not docs:
        return []
    average = sum(len(d) for d in docs) / len(docs)
    df = Counter(word for doc in docs for word in set(doc))
    query_words = _expand(query)
    scored: list[tuple[float, str, ToolSpec]] = []
    for tool, doc in zip(tools, docs, strict=True):
        tf = Counter(doc)
        score = 0.0
        for word in query_words:
            if word not in tf:
                continue
            idf = math.log(1 + (len(docs) - df[word] + 0.5) / (df[word] + 0.5))
            norm = K1 * (1 - B + B * len(doc) / average)
            score += idf * tf[word] * (K1 + 1) / (tf[word] + norm)
        if score > 0:
            scored.append((score, tool.name, tool))
    scored.sort(key=lambda item: (-item[0], item[1]))
    floor = scored[0][0] * SCORE_FLOOR if scored else 0.0
    return [tool for score, _, tool in scored[:limit] if score >= floor]
