"""A tiny CSV writer."""


def write_row(fields):
    """Returns one CSV line (without the newline) for the list of fields."""
    return ",".join(str(f) for f in fields)
