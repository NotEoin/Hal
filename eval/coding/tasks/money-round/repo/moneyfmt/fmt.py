"""Percent of cents."""


def format_cents(amount, rate):
    """Returns `rate` percent of `amount` cents as integer cents."""
    return round(amount * rate / 100)
