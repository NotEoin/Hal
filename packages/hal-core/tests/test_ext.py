"""Tests for `halcore.ext`: editing the disabled list keeps the rest of the config.

Run with: uv run pytest packages/hal-core
"""
import os
import unittest
from pathlib import Path

import halcore_env  # noqa: F401  (sets up the import path and a throwaway Hal state)
from halcore import ext


class ExtTest(unittest.TestCase):
    def test_set_disabled_keeps_the_rest(self):
        Path(os.environ["HAL_CONFIG"]).write_text('[hal]\nvault = "v"\n\n[hub]\npinned = ["a"]\n')
        ext.set_disabled(["phone"])
        ext.set_disabled(["phone", "mail"])
        text = Path(os.environ["HAL_CONFIG"]).read_text()
        self.assertIn('disabled = ["mail", "phone"]', text)
        self.assertIn('pinned = ["a"]', text)
        self.assertEqual(text.count("disabled"), 1)


if __name__ == "__main__":
    unittest.main()
