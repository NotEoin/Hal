"""Tests for `halcore.evaluate`: placeholders, answer checks and the shipped scenarios.

Run with: uv run pytest packages/hal-core
"""
import unittest
from datetime import date

import halcore_env  # noqa: F401  (sets up the import path and a throwaway Hal state)
from halcore import evaluate


class EvalTest(unittest.TestCase):
    def test_placeholders(self):
        sat = date(2026, 10, 10)
        self.assertEqual(evaluate.fill("{date:thursday}T10:00", sat), "2026-10-15T10:00")
        self.assertEqual(evaluate.fill("{date:tomorrow}", sat), "2026-10-11")
        self.assertEqual(evaluate.fill("{weekday:today}", sat), "Saturday")
        self.assertEqual(evaluate.fill("{day:saturday}", sat), "17")

    def test_checks(self):
        case = {"tools": ["calendar_add"], "args": {"calendar_add": {"title": "(?i)dentist"}},
                "reply": "(?i)thursday", "max_chars": 50}
        good = [{"tools": [{"name": "calendar_add", "args": {"title": "Dentist"}, "confirm": False}]}]
        self.assertEqual(evaluate.check(case, good, None, "Booked for Thursday."), [])
        bad = [{"tools": []}]
        problems = evaluate.check(case, bad, None, "x" * 60)
        self.assertEqual(len(problems), 3)

    def test_confirm_first(self):
        case = {"confirm_first": ["system_kill"]}
        unasked = [{"tools": [{"name": "system_kill", "args": {}, "confirm": False}]}]
        self.assertTrue(evaluate.check(case, unasked, None, "done"))

    def test_scenarios_parse(self):
        cases = evaluate.load_cases()
        self.assertGreater(len(cases), 30)
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        for c in cases:
            self.assertTrue(c.get("ask") or c.get("turns"), c["id"])


if __name__ == "__main__":
    unittest.main()
