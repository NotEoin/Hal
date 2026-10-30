"""The `[sandbox]` config table.

`lockdown` has no environment variable and no command-line flag on purpose: RESOLUTIONS I2 says
only the user changes it, on the host, in the file or with `hal config set`, so nothing a box
or a tool call can reach is able to raise its own privileges.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from halkit import config

GIB = 1024**3
LOCKDOWN_LEVELS = ("strict", "standard", "relaxed", "off")
DEFAULT_PIDS = 1024  # REQ-BOX-20
DEFAULT_SCRATCH_MAX_BYTES = 2 * GIB  # RESOLUTIONS I3: the scratchpad is size-capped
DEFAULT_MAX_COPY_BYTES = 2 * GIB  # REQ-BOX-56


def _positive(value: object) -> str | None:
    ok = isinstance(value, int | float) and not isinstance(value, bool) and value > 0
    return None if ok else "must be a positive number"


def _size(value: object) -> str | None:
    if value == "auto":
        return None
    if isinstance(value, str) and value[:-1].isdigit() and value[-1:] in "kmg":
        return None
    return 'must be "auto" or a size such as "512m" or "8g"'


SANDBOX_TABLE = config.register(
    config.Table(
        name="sandbox",
        owner="hal-sandbox",
        keys=(
            config.Key(
                "lockdown",
                str,
                "standard",
                choices=LOCKDOWN_LEVELS,
                doc="strict | standard | relaxed | off; changed by the user on the host only",
            ),
            config.Key("memory", str, "auto", check=_size, doc='Box memory limit, "auto" or e.g. "8g"'),
            config.Key(
                "cpus",
                (int, float),
                0,
                check=lambda v: None if v >= 0 else "must be >= 0",
                doc="CPUs, 0 = auto",
            ),
            config.Key("pids", int, DEFAULT_PIDS, check=_positive, doc="Max processes in the box"),
            config.Key("tmp_size", str, "1g", check=_size, doc="Size of the /tmp tmpfs"),
            config.Key(
                "scratch_max_bytes", int, DEFAULT_SCRATCH_MAX_BYTES, check=_positive, doc="Cap for /scratch"
            ),
            config.Key(
                "max_copy_bytes", int, DEFAULT_MAX_COPY_BYTES, check=_positive, doc="Cap for an isolated copy"
            ),
        ),
    )
)


def load(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Mapping[str, object]:
    """The resolved `[sandbox]` settings."""
    return config.load(SANDBOX_TABLE, path=path, environ=environ)
