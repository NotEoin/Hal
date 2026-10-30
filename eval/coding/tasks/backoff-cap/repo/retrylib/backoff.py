"""Exponential backoff."""


def delay(attempt, base=0.5, cap=30.0):
    """Returns the seconds to wait before retry number `attempt`."""
    return base * 2.0**attempt
