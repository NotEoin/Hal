"""Joining paths under a base."""

import os


def safe_join(base, *parts):
    """Returns the joined path; raises ValueError when it leaves `base`."""
    path = os.path.normpath(os.path.join(base, *parts))
    if not path.startswith(os.path.normpath(base)):
        raise ValueError(f"path escapes base: {path}")
    return path
