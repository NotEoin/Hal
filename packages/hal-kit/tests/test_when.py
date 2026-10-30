"""Tests for times of day, ranges and lengths in plain English, against a fixed Saturday noon."""

import unittest
from datetime import datetime

SATURDAY = datetime(2026, 10, 10, 12, 0)


def span(text, **kw):
    from halkit.when import parse

    w = parse(text, SATURDAY, **kw)
    return (
        w.start.strftime("%Y-%m-%d %H:%M") if w.start else None,
        w.end.strftime("%Y-%m-%d %H:%M") if w.end else None,
    )


class RangeTest(unittest.TestCase):
    def test_end_am_pm_carries_back_to_the_start(self):
        self.assertEqual(span("tomorrow from 2 till 3:30pm"), ("2026-10-11 14:00", "2026-10-11 15:30"))
        self.assertEqual(span("tomorrow 11 till 1pm"), ("2026-10-11 11:00", "2026-10-11 13:00"))
        self.assertEqual(span("friday 8-10pm"), ("2026-10-16 20:00", "2026-10-16 22:00"))

    def test_en_and_em_dashes_separate_a_range(self):
        # Typed dashes (U+2013, U+2014) are common in pasted text; they mean the same as "-".
        for dash in ("\u2013", "\u2014"):
            self.assertEqual(span(f"friday 8{dash}10pm"), ("2026-10-16 20:00", "2026-10-16 22:00"), dash)

    def test_range_forms(self):
        cases = {
            "monday 2-4pm": ("2026-10-12 14:00", "2026-10-12 16:00"),
            "monday 2 to 4": ("2026-10-12 14:00", "2026-10-12 16:00"),
            "monday between 2 and 4": ("2026-10-12 14:00", "2026-10-12 16:00"),
            "monday from 9 until 5": ("2026-10-12 09:00", "2026-10-12 17:00"),
            "monday 10am to 2": ("2026-10-12 10:00", "2026-10-12 14:00"),
            "monday 09:00-17:30": ("2026-10-12 09:00", "2026-10-12 17:30"),
            "monday from noon till 2": ("2026-10-12 12:00", "2026-10-12 14:00"),
            "monday 2pm until 4": ("2026-10-12 14:00", "2026-10-12 16:00"),
            "tonight from 8 to 10": ("2026-10-10 20:00", "2026-10-10 22:00"),
            "sunday from 9 till 11 in the morning": ("2026-10-11 09:00", "2026-10-11 11:00"),
            "friday 10pm to 1am": ("2026-10-16 22:00", "2026-10-17 01:00"),
        }
        for text, want in cases.items():
            self.assertEqual(span(text), want, text)

    def test_range_leaves_the_title(self):
        from halkit.when import parse

        w = parse("Project meeting tomorrow from 2 till 3:30pm", SATURDAY)
        self.assertEqual(w.rest, "Project meeting")

    def test_counts_are_not_ranges(self):
        self.assertEqual(span("dinner for 2 to 4 people at 7"), ("2026-10-10 19:00", None))


class TimeOfDayTest(unittest.TestCase):
    def test_everyday_phrases(self):
        cases = {
            "Sunday at 1": "2026-10-11 13:00",
            "sunday at 1am": "2026-10-11 01:00",
            "sunday at 10": "2026-10-11 10:00",
            "next Tuesday at 9am": "2026-10-13 09:00",
            "the 3rd of March 2031 at 11am": "2031-03-03 11:00",
            "7 tomorrow morning": "2026-10-11 07:00",
            "7am tomorrow": "2026-10-11 07:00",
            "tonight at 8": "2026-10-10 20:00",
            "8 tonight": "2026-10-10 20:00",
            "tomorrow at noon": "2026-10-11 12:00",
            "monday at half past 3": "2026-10-12 15:30",
            "half past 7 tonight": "2026-10-10 19:30",
            "monday at 2:30": "2026-10-12 14:30",
            "monday at 07:30": "2026-10-12 07:30",
            "sunday at 11 at night": "2026-10-11 23:00",
            "sunday at 7 o'clock": "2026-10-11 19:00",
        }
        for text, start in cases.items():
            self.assertEqual(span(text)[0], start, text)

    def test_all_day_dates(self):
        from halkit.when import parse

        for text, day in {
            "the 3rd of March 2031": "2031-03-03",
            "3 March 2031": "2031-03-03",
            "2031-03-03": "2031-03-03",
            "next Tuesday": "2026-10-13",
        }.items():
            w = parse(text, SATURDAY)
            self.assertTrue(w.all_day, text)
            self.assertEqual(w.start.strftime("%Y-%m-%d"), day, text)

    def test_mornings_for_alarms(self):
        for text, start in {
            "tomorrow at 7": "2026-10-11 07:00",
            "6:30": "2026-10-11 06:30",
            "7 tomorrow morning": "2026-10-11 07:00",
            "at 5": "2026-10-11 05:00",
        }.items():
            self.assertEqual(span(text, prefer_pm=False)[0], start, text)

    def test_compound_lengths(self):
        self.assertEqual(
            span("monday at 2pm for 1 hour 30 minutes"), ("2026-10-12 14:00", "2026-10-12 15:30")
        )
        self.assertEqual(
            span("monday at 2pm for an hour and a half"), ("2026-10-12 14:00", "2026-10-12 15:30")
        )
        self.assertEqual(span("monday at 2pm for 90 minutes"), ("2026-10-12 14:00", "2026-10-12 15:30"))


if __name__ == "__main__":
    unittest.main()
