"""Understands dates, times, durations and repeats written in plain English, and steps repeat rules.

    parse("dentist thursday at 10 for an hour")
    -> When(start=<Thu 10:00>, end=<Thu 11:00>, all_day=False, rrule=None, rest="dentist")

UK conventions: day before month in numeric dates, 24-hour or am/pm times.
A bare hour from 1 to 7 ("at 3") means the afternoon unless the morning is named ("7 tomorrow
morning") or the caller asks otherwise (alarms do). Ranges ("from 2 till 3:30pm", "2-4pm",
"between 2 and 4") set the end; the end's am or pm carries back to the start. Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from halkit.timeutil import utc_now

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_ABBR = {d[:3]: i for i, d in enumerate(WEEKDAYS)} | {
    "tues": 1,
    "thur": 3,
    "thurs": 3,
    "weds": 2,
}
MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]
MONTH_ABBR = {m[:3]: i + 1 for i, m in enumerate(MONTHS)} | {"sept": 9}
NUMBERS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "forty-five": 45,
    "ninety": 90,
    "half an": 0.5,
    "a couple of": 2,
    "a few": 3,
}
PARTS = {
    "morning": time(9),
    "afternoon": time(14),
    "evening": time(18),
    "tonight": time(20),
    "night": time(21),
    "lunchtime": time(12, 30),
    "noon": time(12),
    "midday": time(12),
    "midnight": time(0),
}
UNITS = {
    "minute": 60,
    "min": 60,
    "mins": 60,
    "hour": 3600,
    "hr": 3600,
    "hrs": 3600,
    "day": 86400,
    "week": 604800,
    "fortnight": 1209600,
}
DAY_NAME = r"(?:mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)(?:day|nesday|rsday|urday|sday)?"
MONTH_NAME = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
NUM = (
    r"(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen"
    r"|twenty|thirty|forty|forty-five|ninety|half an|a couple of|a few)"
)


@dataclass(frozen=True)
class When:
    """A parsed phrase: when it starts and ends, whether it is all-day, its repeat, and the rest."""

    start: datetime | None
    end: datetime | None = None
    all_day: bool = False
    rrule: str | None = None
    rest: str = ""


def _num(word: str) -> float:
    word = word.strip().lower()
    return float(word) if re.fullmatch(r"\d+(\.\d+)?", word) else float(NUMBERS.get(word, 1))


def _weekday(name: str) -> int:
    return DAY_ABBR[name.lower()[:3] if name.lower()[:4] not in DAY_ABBR else name.lower()[:4]]


class _Text:
    """The input, from which recognised phrases are cut as they are parsed."""

    def __init__(self, text: str) -> None:
        self.s = " " + re.sub(r"\s+", " ", text.strip()) + " "

    def take(self, pattern: str) -> re.Match | None:
        m = re.search(pattern, self.s, re.I)
        if m:
            self.s = self.s[: m.start()] + " " + self.s[m.end() :]
        return m

    @property
    def rest(self) -> str:
        out = re.sub(r"\s+", " ", self.s).strip(" ,.-")
        if re.fullmatch(r"(?:on|at|for|from|by|in)", out, re.I):
            return ""
        return re.sub(
            r"^(?:on|at|for|from|by|in)\s+|\s+(?:on|at|for|from|by|in)$", "", out, flags=re.I
        ).strip()


# Words for a part of the day: whether an hour said with them is am or pm, and the time they mean alone.
HINTS = {"morning": "a", "afternoon": "p", "evening": "p", "tonight": "p", "night": "p"}
PART = r"(?:\b(?:in\s+the|this|at)\s+)?\b(?P<part>morning|afternoon|evening|tonight|night)\b"
NAMED = {"noon": time(12), "midday": time(12), "midnight": time(0), "lunchtime": time(12, 30)}
NOT_A_TIME = r"(?!\s*(?:minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|people|of|st|nd|rd|th|%|/))"
# One end of a range: "2", "3:30pm", "10 am", "noon".
END = (
    r"(?:(?P<h{n}>\d{{1,2}})(?:[:.](?P<m{n}>\d{{2}}))?\s*(?P<a{n}>[ap]\.?m\.?)?(?![\d:])"
    r"|(?P<n{n}>noon|midday|midnight))"
)
RANGES = (
    rf"\bbetween\s+{END.format(n=1)}\s+and\s+{END.format(n=2)}" + NOT_A_TIME,
    rf"(?:\b(?:from|at|@)\s*)?(?<![\d/:.-]){END.format(n=1)}\s*(?:-|\u2013|\u2014|\bto\b|\btill?\b|\buntil\b)\s*"
    rf"{END.format(n=2)}" + NOT_A_TIME,
)
TIME = (
    r"(?:\b(?:at|@)\s*)?(?:(?P<rel>quarter past|half past|quarter to)\s+(?P<rh>\d{1,2})"
    r"|(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?)"
    r"|(?P<h2>\d{1,2})[:.](?P<m2>\d{2})"
    r"|\bat\s+(?P<h3>\d{1,2})\b" + NOT_A_TIME + r"(?:\s*o'?clock)?"
    r"|\b(?P<h4>\d{1,2})\s*o'?clock"
    r"|\b(?P<named>noon|midday|midnight|lunchtime)\b)"
)


def _hour(hour: str | int, minute: str | int | None, ap: str, prefer_pm: bool) -> time:
    """Turns an hour said with 'a' (am), 'p' (pm) or '' (neither) into a time of day.

    With neither, an hour from 1 to 7 is the afternoon when `prefer_pm`, else the morning; a
    leading zero ("07:30") or an hour past 12 is read as the 24-hour clock.
    """
    h, mins = int(hour), int(minute or 0)
    if ap == "p" and h < 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and prefer_pm and 1 <= h <= 7 and not str(hour).startswith("0"):
        h += 12
    return time(h % 24, mins % 60)


def _candidates(hour: str, minute: str) -> list[time]:
    """The times an hour without am or pm could mean: both halves of the day for 1 to 12."""
    hour, minute = int(hour), int(minute)
    if hour == 0 or hour > 12:
        return [time(hour % 24, minute)]
    return [time(hour % 12, minute), time(hour % 12 + 12, minute)]


def _find_range(t: _Text, hint: str, prefer_pm: bool) -> tuple[time, time] | None:
    """Reads 'from 2 till 3:30pm', '2-4pm', '10am to 2', 'between 2 and 4' as (start, end).

    An end's am or pm carries back to a start without one: '2 till 3:30pm' starts at 14:00.
    """
    m = t.take(RANGES[0]) or t.take(RANGES[1])
    if not m:
        return None

    def side(n: str) -> tuple[str, str, str] | time:
        if m[f"n{n}"]:
            return NAMED[m[f"n{n}"].lower()]
        ap = (m[f"a{n}"] or "")[:1].lower()
        if not ap and m[f"h{n}"].startswith("0"):
            ap = "a"  # "09:00" is the 24-hour clock
        return m[f"h{n}"], m[f"m{n}"] or "0", ap

    a, b = side("1"), side("2")
    if isinstance(b, tuple) and not b[2] and hint:
        b = (b[0], b[1], hint)
    if isinstance(a, time) and isinstance(b, time):
        return a, b
    if (isinstance(a, tuple) and a[2]) or isinstance(a, time):
        start = a if isinstance(a, time) else _hour(*a, prefer_pm)
        if isinstance(b, time):
            return start, b
        if b[2]:
            return start, _hour(*b, prefer_pm)
        later = [c for c in _candidates(b[0], b[1]) if c > start]
        return start, later[0] if later else _hour(*b, prefer_pm)
    # The start has no am or pm.
    if isinstance(b, time) or b[2]:
        end = b if isinstance(b, time) else _hour(*b, prefer_pm)
        earlier = [c for c in _candidates(a[0], a[1]) if c < end]
        return (earlier[-1] if earlier else _hour(*a, prefer_pm)), end
    start = _hour(*a, prefer_pm)
    later = [c for c in _candidates(b[0], b[1]) if c > start]
    return start, later[0] if later else _hour(*b, prefer_pm)


def _find_time(t: _Text, prefer_pm: bool = True) -> tuple[time | None, time | None]:
    """Finds the time of day, and the end time when a range is given, cutting both from the text.

    A part of the day sets am or pm for an hour said with it ('7 tomorrow morning', 'tonight at 8');
    alone it means a typical time ('evening' is 18:00).
    """
    part = re.search(PART, t.s, re.I)
    hint = HINTS[part["part"].lower()] if part else ""
    rng = _find_range(t, hint, prefer_pm)
    if rng:
        t.take(PART)
        return rng
    # A bare hour beside the part of the day: "7 tomorrow morning" (once "tomorrow" is cut), "8 tonight".
    m = t.take(
        r"(?:\b(?:at|@)\s*)?(?<!past )(?<!to )\b(?P<h4>\d{1,2})(?:[:.](?P<m4>\d{2}))?\s*(?:o'?clock\s*)?"
        + PART
    )
    if not m:
        t.take(PART)
        m = t.take(TIME)
    if not m:
        return (PARTS[part["part"].lower()] if part else None), None
    g = m.groupdict()
    if g.get("named"):
        return NAMED[g["named"].lower()], None
    if g.get("rel"):
        mins = {"quarter past": 15, "half past": 30, "quarter to": -15}[g["rel"].lower()]
        base = datetime.combine(date.today(), _hour(g["rh"], "0", hint, prefer_pm)) + timedelta(minutes=mins)
        return base.time(), None
    if g.get("h"):
        return _hour(g["h"], g["m"], g["ap"][:1].lower(), prefer_pm), None
    if g.get("h2"):
        return _hour(g["h2"], g["m2"], hint, prefer_pm), None
    return _hour(g.get("h3") or g["h4"], g.get("m4"), hint, prefer_pm), None


def _find_date(t: _Text, now: datetime) -> tuple[date | None, bool]:
    """Returns (date, explicit): explicit is False when no date phrase was found."""
    today = now.date()
    if t.take(r"\bday after tomorrow\b"):
        return today + timedelta(days=2), True
    if t.take(r"\btomorrow\b|\btmrw\b"):
        return today + timedelta(days=1), True
    if t.take(r"\byesterday\b"):
        return today - timedelta(days=1), True
    if t.take(r"\btoday\b"):
        return today, True
    if re.search(r"\btonight\b", t.s, re.I):
        # Left in place so the time parser reads it as the evening.
        return today, True
    m = t.take(r"\b(\d{4})-(\d{2})-(\d{2})\b")
    if m:
        return date(int(m[1]), int(m[2]), int(m[3])), True
    m = t.take(
        rf"\b(?:on\s+)?(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTH_NAME})(?:\s+(\d{{4}}))?\b"
    )
    if not m:
        m2 = t.take(rf"\b({MONTH_NAME})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b")
        if m2:
            m = (None, m2[2], m2[1], m2[3])
    if m:
        day, month = (
            int(m[1]),
            MONTH_ABBR[m[2].lower()[:4] if m[2].lower()[:4] in MONTH_ABBR else m[2].lower()[:3]],
        )
        year = int(m[3]) if m[3] else today.year
        d = date(year, month, day)
        if not m[3] and d < today:
            d = date(year + 1, month, day)
        return d, True
    m = t.take(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
    if m:
        year = int(m[3]) if m[3] else today.year
        year += 2000 if year < 100 else 0
        d = date(year, int(m[2]), int(m[1]))
        if not m[3] and d < today:
            d = date(year + 1, int(m[2]), int(m[1]))
        return d, True
    m = t.take(r"\b(?:on\s+)?the\s+(\d{1,2})(?:st|nd|rd|th)\b")
    if m:
        day = int(m[1])
        d = date(today.year, today.month, day)
        if d < today:
            d = date(today.year + (today.month == 12), today.month % 12 + 1, day)
        return d, True
    m = t.take(rf"\b(next|this|on|coming)?\s*({DAY_NAME})\b")
    if m:
        target = _weekday(m[2])
        ahead = (target - today.weekday()) % 7
        if (m[1] or "").lower() == "next" and ahead == 0:
            ahead = 7
        elif (
            (m[1] or "").lower() == "next" and ahead < 7 and today.weekday() < 5 and target > today.weekday()
        ):
            # "next Thursday" said on a Monday means the Thursday after this one.
            ahead += 7
        return today + timedelta(days=ahead or 0), True
    m = t.take(r"\bnext week\b")
    if m:
        return today + timedelta(days=7 - today.weekday()), True
    return None, False


def _find_offset(t: _Text) -> timedelta | None:
    m = t.take(rf"\bin\s+{NUM}\s*(minute|min|mins|hour|hr|hrs|day|week|fortnight)s?\b")
    if m:
        return timedelta(seconds=_num(m[1]) * UNITS[m[2].lower()])
    if t.take(r"\bin half an hour\b"):
        return timedelta(minutes=30)
    return None


def _find_duration(t: _Text) -> timedelta | None:
    """Reads 'for an hour', 'for 90 minutes', 'for 1 hour 30 minutes', 'for an hour and a half'."""
    m = t.take(
        rf"\bfor\s+{NUM}\s*(?P<half1>and a half\s+)?(?P<unit>minute|min|mins|hour|hr|hrs|day|week)s?"
        r"(?P<half2>\s+and\s+a\s+half)?(?:,?\s*(?:and\s+)?(?P<extra>\d+)\s*(?:minutes?|mins?))?\b"
    )
    if m:
        n = _num(m[1]) + (0.5 if m["half1"] or m["half2"] else 0)
        return timedelta(seconds=n * UNITS[m["unit"].lower()] + int(m["extra"] or 0) * 60)
    if t.take(r"\bfor half an hour\b"):
        return timedelta(minutes=30)
    return None


def _find_repeat(t: _Text) -> str | None:
    m = t.take(r"\bevery\s+weekday\b|\bweekdays\b")
    if m:
        return "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    m = t.take(rf"\b(?:every|each)\s+((?:{DAY_NAME})(?:\s*(?:,|and)\s*(?:{DAY_NAME}))*)\b")
    if m:
        days = re.findall(DAY_NAME, m[1], re.I)
        codes = ",".join(["MO", "TU", "WE", "TH", "FR", "SA", "SU"][_weekday(d)] for d in days)
        return f"FREQ=WEEKLY;BYDAY={codes}"
    m = t.take(rf"\bevery\s+{NUM}?\s*(day|week|month|year)s?\b")
    if m:
        freq = {"day": "DAILY", "week": "WEEKLY", "month": "MONTHLY", "year": "YEARLY"}[m[2].lower()]
        n = int(_num(m[1])) if m[1] else 1
        return f"FREQ={freq}" + (f";INTERVAL={n}" if n > 1 else "")
    m = t.take(r"\b(daily|weekly|fortnightly|monthly|yearly|annually)\b")
    if m:
        return {
            "daily": "FREQ=DAILY",
            "weekly": "FREQ=WEEKLY",
            "fortnightly": "FREQ=WEEKLY;INTERVAL=2",
            "monthly": "FREQ=MONTHLY",
            "yearly": "FREQ=YEARLY",
            "annually": "FREQ=YEARLY",
        }[m[1].lower()]
    return None


BYDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
MAX_MONTH_STEPS = 48  # a rule on the 29th to 31st always has a valid month within four years


def next_occurrence(at: datetime, rrule: str) -> datetime:
    """The first occurrence of `rrule` after `at`, the previous occurrence.

    Handles `FREQ` DAILY, WEEKLY (with `BYDAY`), MONTHLY and YEARLY, and `INTERVAL`. Raises
    `ValueError` for any other frequency or a malformed `INTERVAL` or `BYDAY`.
    """
    rule = dict(part.split("=", 1) for part in rrule.split(";") if "=" in part)
    freq = rule.get("FREQ", "DAILY")
    try:
        interval = int(rule.get("INTERVAL", 1))
    except ValueError:
        raise ValueError(f"bad INTERVAL in {rrule!r}") from None
    if interval < 1:
        raise ValueError(f"INTERVAL must be at least 1 in {rrule!r}")
    if freq == "DAILY":
        return at + timedelta(days=interval)
    if freq == "WEEKLY":
        if "BYDAY" not in rule:
            return at + timedelta(weeks=interval)
        codes = rule["BYDAY"].split(",")
        if not codes or any(c[-2:] not in BYDAY_CODES for c in codes):
            raise ValueError(f"bad BYDAY in {rrule!r}")
        days = sorted(BYDAY_CODES.index(c[-2:]) for c in codes)
        later = [d for d in days if d > at.weekday()]
        if later:
            return at + timedelta(days=later[0] - at.weekday())
        return at + timedelta(days=7 * interval - at.weekday() + days[0])
    if freq in ("MONTHLY", "YEARLY"):
        step = interval if freq == "MONTHLY" else 12 * interval
        for k in range(1, MAX_MONTH_STEPS + 1):
            month = at.month - 1 + k * step
            try:
                return at.replace(year=at.year + month // 12, month=month % 12 + 1)
            except ValueError:  # no such day in that month: calendar skips it
                continue
        raise ValueError(f"no valid date within {MAX_MONTH_STEPS} steps for {rrule!r}")
    raise ValueError(f"unsupported FREQ {freq!r} in {rrule!r}")


def parse(
    text: str, now: datetime | None = None, default_time: time | None = None, prefer_pm: bool = True
) -> When:
    """Parses the date, time, length and repeat in `text`; `rest` is what is left.

    Args:
        text: For example "lunch with Sam next friday 1pm for 90 minutes" or "tomorrow from 2 till 3:30pm".
        now: The reference time; the current time by default.
        default_time: Time to use when a date is given without one; None makes it all-day.
        prefer_pm: Whether a bare hour from 1 to 7 ("at 3") means the afternoon; alarms want False.
    """
    now = now or utc_now().astimezone().replace(tzinfo=None, second=0, microsecond=0)  # naive local time
    t = _Text(text)
    rrule = _find_repeat(t)
    offset = _find_offset(t)
    duration = _find_duration(t)
    if offset is not None:
        start = now + offset
        return When(start, start + duration if duration else None, False, rrule, t.rest)
    d, explicit = _find_date(t, now)
    clock, until = _find_time(t, prefer_pm)
    if d is None and clock is None:
        if rrule:
            d = now.date()
        else:
            return When(None, rest=t.rest)
    if d is None:
        d = now.date()
        if datetime.combine(d, clock) < now and not rrule:
            d += timedelta(days=1)
    if clock is None:
        if default_time is None:
            return When(datetime.combine(d, time()), None, True, rrule, t.rest)
        clock = default_time
    start = datetime.combine(d, clock)
    if rrule and "BYDAY" in rrule and not explicit:
        codes = rrule.split("BYDAY=")[1].split(",")
        while ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][start.weekday()] not in codes or start < now:
            start += timedelta(days=1)
    end = None
    if until is not None:
        end = datetime.combine(start.date(), until)
        if end <= start:
            end += timedelta(days=1)
    elif duration:
        end = start + duration
    return When(start, end, False, rrule, t.rest)
