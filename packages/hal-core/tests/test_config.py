"""Tests for `halcore.config`: hal-hub comes from the installed package, never from a sys.path search.

Run with: uv run pytest packages/hal-core
"""
import importlib.util
import sys
import unittest

import halcore_env  # noqa: F401  (sets up the import path and a throwaway Hal state)
from halcore import config


class HubLookupTest(unittest.TestCase):
    def test_hub_is_the_halhub_module(self):
        import halhub

        self.assertIs(config.hub(), halhub)

    def test_hub_follows_a_replaced_module_in_sys_modules(self):
        import halhub

        stale = sys.modules.pop("halhub")
        try:
            fresh = config.hub()
            self.assertIsNot(fresh, stale)
        finally:
            sys.modules["halhub"] = halhub

    def test_the_old_sys_path_lookup_module_is_gone(self):
        self.assertIsNone(importlib.util.find_spec("halcore.hub"))


if __name__ == "__main__":
    unittest.main()
