"""Least-recently-used cache."""

from collections import OrderedDict


class LRUCache:
    """A cache that keeps at most `capacity` entries."""

    def __init__(self, capacity):
        self.capacity = capacity
        self._data = OrderedDict()

    def get(self, key):
        """Returns the value for `key` or None."""
        return self._data.get(key)

    def put(self, key, value):
        """Stores `value` under `key`."""
        self._data[key] = value
        if len(self._data) > self.capacity:
            self._data.popitem(last=True)
