"""Characterisation tests for `halkit.when`: the pinned results of the shared parser, and `next_occurrence`.

The phrases come from the three identical copies in calendar, clock and tasks (checked against
them on 2026-10-30). The routines fork's differing cases are not carried over: the fork is
replaced by this module, and its two differences are the fixes. The reference time is a fixed
Saturday noon (2026-10-10 12:00).
"""

from datetime import datetime, time

import pytest

from halkit.when import When, next_occurrence, parse

NOW = datetime(2026, 10, 10, 12, 0)

# text -> (start, end time of day, all_day, rrule, rest); both parsers agree on these.
COMMON = {
    "tomorrow at 3pm": ("2026-10-11 15:00", None, False, None, ""),
    "every day at 23:00": ("2026-10-10 23:00", None, False, "FREQ=DAILY", ""),
    "every monday and friday at 9am": (
        "2026-10-12 09:00",
        None,
        False,
        "FREQ=WEEKLY;BYDAY=MO,FR",
        "",
    ),
    "in 20 minutes": ("2026-10-10 12:20", None, False, None, ""),
    "tonight": ("2026-10-10 20:00", None, False, None, ""),
    "next friday 1pm for 90 minutes": ("2026-10-16 13:00", "14:30", False, None, ""),
    "every weekday": (
        "2026-10-10 00:00",
        None,
        True,
        "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        "",
    ),
    "at 7": ("2026-10-10 19:00", None, False, None, ""),
    "every 2 weeks on tuesday": ("2026-10-13 00:00", None, True, "FREQ=WEEKLY;INTERVAL=2", ""),
    "today": ("2026-10-10 00:00", None, True, None, ""),
    "on 3 november": ("2026-11-03 00:00", None, True, None, ""),
    "quarter past 4": ("2026-10-10 16:15", None, False, None, ""),
    "lunchtime": ("2026-10-10 12:30", None, False, None, ""),
    "no time here": (None, None, False, None, "no time here"),
    "every month": ("2026-10-10 00:00", None, True, "FREQ=MONTHLY", ""),
    "half past 9 tomorrow": ("2026-10-11 09:30", None, False, None, ""),
    "noon": ("2026-10-10 12:00", None, False, None, ""),
    "in 2 hours for 30 minutes": ("2026-10-10 14:00", "14:30", False, None, ""),
}


def summary(text: str) -> tuple:
    """Parses `text` against the fixed clock and flattens the result for comparison."""
    w = parse(text, NOW)
    return (
        w.start.strftime("%Y-%m-%d %H:%M") if w.start else None,
        w.end.strftime("%H:%M") if w.end else None,
        w.all_day,
        w.rrule,
        w.rest,
    )


@pytest.mark.parametrize("text", COMMON)
def test_common_phrases_parse_as_pinned(text):
    assert summary(text) == COMMON[text]


def test_the_routines_fork_cases_now_parse_the_shared_way():
    assert summary("at 6:30 in the morning") == ("2026-10-11 06:30", None, False, None, "")
    assert summary("friday 8-10pm") == ("2026-10-16 20:00", "22:00", False, None, "")


def test_alarm_style_bare_hour_can_mean_morning():
    assert parse("at 7", NOW, prefer_pm=False).start == datetime(2026, 10, 11, 7, 0)


def test_default_time_fills_a_date_given_without_a_time():
    w = parse("tomorrow", NOW, default_time=time(9, 0))
    assert (w.start, w.all_day) == (datetime(2026, 10, 11, 9, 0), False)


def test_a_parsed_when_cannot_be_changed():
    with pytest.raises(AttributeError):
        parse("tomorrow", NOW).rest = "x"  # type: ignore[misc]  # frozen on purpose
    assert isinstance(parse("noon", NOW), When)


@pytest.mark.parametrize(
    ("at", "rrule", "want"),
    [
        ("2026-10-10 09:00", "FREQ=DAILY", "2026-10-11 09:00"),
        ("2026-10-10 09:00", "FREQ=DAILY;INTERVAL=3", "2026-10-13 09:00"),
        ("2026-10-10 09:00", "FREQ=WEEKLY", "2026-10-17 09:00"),
        ("2026-10-10 09:00", "FREQ=WEEKLY;INTERVAL=2", "2026-10-24 09:00"),
        ("2026-10-12 09:00", "FREQ=WEEKLY;BYDAY=MO,FR", "2026-10-16 09:00"),
        ("2026-10-16 09:00", "FREQ=WEEKLY;BYDAY=MO,FR", "2026-10-19 09:00"),
        ("2026-10-10 09:00", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "2026-10-12 09:00"),
        ("2026-10-10 09:00", "FREQ=MONTHLY", "2026-11-10 09:00"),
        ("2026-12-15 09:00", "FREQ=MONTHLY", "2027-01-15 09:00"),
        ("2026-10-31 09:00", "FREQ=MONTHLY", "2026-12-31 09:00"),  # November has no 31st: skipped
        ("2026-10-10 09:00", "FREQ=YEARLY", "2027-10-10 09:00"),
        ("2028-02-29 09:00", "FREQ=YEARLY", "2032-02-29 09:00"),  # 2029 to 2031 have no 29 February
    ],
)
def test_next_occurrence_follows_the_calendar_rules(at, rrule, want):
    got = next_occurrence(datetime.fromisoformat(at), rrule)
    assert got == datetime.fromisoformat(want)


@pytest.mark.parametrize(
    "rrule", ["FREQ=HOURLY", "FREQ=DAILY;INTERVAL=x", "FREQ=DAILY;INTERVAL=0", "FREQ=WEEKLY;BYDAY=XX"]
)
def test_next_occurrence_refuses_rules_it_cannot_step(rrule):
    with pytest.raises(ValueError):
        next_occurrence(NOW, rrule)


def test_every_rule_the_parser_writes_can_be_stepped():
    for text in ("every day", "every monday and friday", "every 2 weeks", "monthly", "fortnightly", "yearly"):
        rule = parse(text, NOW).rrule
        assert rule and next_occurrence(NOW, rule) > NOW
