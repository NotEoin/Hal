"""Tests for `halkit.testing.resolver`."""

import pytest

from halkit.testing import FakeResolver


def test_resolver_answers_from_its_table_and_records_each_lookup():
    resolver = FakeResolver({"Good.Example": ["127.0.0.1"]})
    assert resolver("good.example") == ["127.0.0.1"]
    assert resolver.lookups == ["good.example"]


def test_unknown_name_fails_like_a_dns_error():
    with pytest.raises(OSError, match="no entry"):
        FakeResolver({})("missing.example")
