"""The hal-kit version and a lookup of every installed Hal distribution's version.

`__version__` is the single source of the hal-kit version: the build reads it from here.
"""

from importlib import metadata

__version__ = "0.1.0.dev0"

DIST_PREFIX = "hal-"  # every Hal distribution is named hal-<part>


def versions() -> dict[str, str]:
    """Maps each installed `hal-*` distribution name to its version."""
    found: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name and name.lower().startswith(DIST_PREFIX):
            found[name.lower()] = dist.version
    return dict(sorted(found.items()))
