"""Parses key = value lines."""


def parse(text):
    """Returns a dict of the key = value lines."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in ";#":
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result
