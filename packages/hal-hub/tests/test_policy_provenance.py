"""Tests for search-result URL recording (`halhub.policy.provenance`, REQ-SEC-92)."""

from __future__ import annotations

from halhub.policy.provenance import MAX_URLS_PER_RESULT, SearchUrls, extract_urls

SID = "0123456789ab"


def test_urls_are_extracted_in_canonical_form():
    text = "See HTTPS://Example.ORG:443/Path?x=1#frag and http://a.example.com:8080/b."
    assert extract_urls(text) == ["https://example.org/Path?x=1", "http://a.example.com:8080/b"]


def test_markdown_and_sentence_punctuation_is_trimmed():
    text = '[docs](https://docs.python.org/3/), or <https://example.org/a>; also "https://e.org/q".'
    assert extract_urls(text) == ["https://docs.python.org/3/", "https://example.org/a", "https://e.org/q"]


def test_non_http_schemes_and_hostless_urls_are_ignored():
    assert extract_urls("ftp://example.org/x file:///etc/passwd https:///nohost") == []


def test_duplicates_are_counted_once():
    assert extract_urls("https://e.org/a https://E.org/a https://e.org/a#x") == ["https://e.org/a"]


def test_one_result_contributes_at_most_fifty_urls():
    text = " ".join(f"https://e.org/{i}" for i in range(MAX_URLS_PER_RESULT + 10))
    urls = extract_urls(text)
    assert len(urls) == MAX_URLS_PER_RESULT
    assert urls[-1] == f"https://e.org/{MAX_URLS_PER_RESULT - 1}"


def test_urls_are_kept_per_session():
    store = SearchUrls()
    assert store.record(SID, "https://e.org/a") == 1
    assert store.urls(SID) == frozenset({"https://e.org/a"})
    assert store.urls("ba9876543210") == frozenset()


def test_session_cap_keeps_the_earliest_urls():
    store = SearchUrls(per_session=3)
    store.record(SID, "https://e.org/1 https://e.org/2")
    assert store.record(SID, "https://e.org/2 https://e.org/3 https://e.org/4") == 1
    assert store.urls(SID) == frozenset({"https://e.org/1", "https://e.org/2", "https://e.org/3"})


def test_forgotten_session_has_no_urls():
    store = SearchUrls()
    store.record(SID, "https://e.org/a")
    store.forget(SID)
    assert store.urls(SID) == frozenset()
