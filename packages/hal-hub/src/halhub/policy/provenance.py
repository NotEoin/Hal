"""Search-result URL recording for the `url:from-search` rule (01-security REQ-SEC-92, F6).

After a tool in `[egress].search_tools` returns, the host records the canonical form
(`halkit.netguard.canonical_url`) of every `http` or `https` URL in the result text, under the
calling session. The broker passes `SearchUrls.urls(session)` as `Context.search_urls`, so a
URL seen in one session never counts in another.

Caps: at most `MAX_URLS_PER_RESULT` URLs from one result and `MAX_URLS_PER_SESSION` per
session (01-security, "Decision algorithm"). Past the session cap new URLs are dropped, not
rotated in: an attacker-controlled result cannot push out the URLs the user's own searches
produced, and a dropped URL only costs a prompt.
"""

from __future__ import annotations

import logging
import re
import threading

from halkit import netguard

log = logging.getLogger(__name__)

MAX_URLS_PER_RESULT = 50  # 01-security, "Decision algorithm"
MAX_URLS_PER_SESSION = 500  # 01-security, "Decision algorithm"
# a URL ends at whitespace, a quote, an angle bracket or a backtick; the characters that
# usually close a sentence or a Markdown link are trimmed from the end afterwards
_URL_RE = re.compile(r"""(?i)\bhttps?://[^\s"'<>`]+""")
_TRAILING = ".,;:!?)]}*_"


def extract_urls(text: str, *, limit: int = MAX_URLS_PER_RESULT) -> list[str]:
    """The distinct canonical `http(s)` URLs in `text`, in order of appearance, at most `limit`.

    A URL `canonical_url` rejects (no usable host) is skipped.
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(text):
        raw = match.group(0).rstrip(_TRAILING)
        try:
            url = netguard.canonical_url(raw)
        except ValueError:
            continue
        if url not in seen:
            seen.add(url)
            found.append(url)
            if len(found) >= limit:
                break
    return found


class SearchUrls:
    """Canonical search-result URLs per session; safe to share between threads."""

    def __init__(self, *, per_session: int = MAX_URLS_PER_SESSION) -> None:
        self._per_session = per_session
        self._by_session: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def record(self, session: str, text: str) -> int:
        """Records the URLs in one search result for `session`; returns how many were new."""
        urls = extract_urls(text)
        added = 0
        with self._lock:
            known = self._by_session.setdefault(session, set())
            for url in urls:
                if url in known:
                    continue
                if len(known) >= self._per_session:
                    log.info(
                        "session %s reached %d search URLs; later ones still ask", session, self._per_session
                    )
                    break
                known.add(url)
                added += 1
        return added

    def urls(self, session: str) -> frozenset[str]:
        """The URLs recorded for `session`, for `Context.search_urls`."""
        with self._lock:
            return frozenset(self._by_session.get(session, ()))

    def forget(self, session: str) -> None:
        """Drops a finished session's URLs."""
        with self._lock:
            self._by_session.pop(session, None)
