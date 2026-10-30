import pytest

from cachex.lru import LRUCache


def test_oldest_key_is_evicted_first():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert cache.get("a") is None
    assert cache.get("b") == 2 and cache.get("c") == 3


def test_get_refreshes_the_key():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")
    cache.put("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1


def test_put_on_existing_key_replaces_and_refreshes():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 9)
    cache.put("c", 3)
    assert cache.get("a") == 9 and cache.get("b") is None


def test_zero_capacity_is_rejected():
    with pytest.raises(ValueError):
        LRUCache(0)
