"""Semantic version comparison."""


def _split(text):
    core, _, pre = text.partition("-")
    return tuple(int(p) for p in core.split(".")), pre


def compare(a, b):
    """Returns -1, 0 or 1 as `a` is lower than, equal to or higher than `b`."""
    (ca, pa), (cb, pb) = _split(a), _split(b)
    if ca != cb:
        return -1 if ca < cb else 1
    if pa == pb:
        return 0
    return -1 if pa < pb else 1
