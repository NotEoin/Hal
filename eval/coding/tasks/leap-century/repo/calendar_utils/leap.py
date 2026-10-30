"""Leap year rules."""


def is_leap(year):
    """True when `year` is a leap year in the Gregorian calendar."""
    return year % 4 == 0
