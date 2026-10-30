"""The `[sandbox]` config tables (02-sandbox "Configuration") and the `[box]` table.

`lockdown` has no environment variable and no command-line flag on purpose: RESOLUTIONS I2 says
only the user changes it, on the host, in the file or with `hal config set`, so nothing a box
or a tool call can reach is able to raise its own privileges.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from halkit import config

GIB = 1024**3
LOCKDOWN_LEVELS = ("strict", "standard", "relaxed", "off")
DEFAULT_PIDS = 1024  # REQ-BOX-20
DEFAULT_SCRATCH_MAX_BYTES = 2 * GIB  # RESOLUTIONS I3: the scratchpad is size-capped
DEFAULT_MAX_COPY_BYTES = 2 * GIB  # REQ-BOX-56


NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
APT_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")  # Debian package names
SECRET_NAME_RE = re.compile(r"(TOKEN|KEY|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION)", re.I)
MAX_MESSAGE_BYTES = 4 * 1024 * 1024  # the `mcp` SDK request cap
DEFAULT_CHECKPOINT_FILE_BYTES = 50 * 1024**2
DEFAULT_NONGIT_TOTAL_BYTES = 512 * 1024**2
DEFAULT_REGISTRIES = [
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "index.crates.io",
    "static.crates.io",
    "crates.io",
]


def _positive(value: object) -> str | None:
    ok = isinstance(value, int | float) and not isinstance(value, bool) and value > 0
    return None if ok else "must be a positive number"


def _size(value: object) -> str | None:
    if value == "auto":
        return None
    if isinstance(value, str) and value[:-1].isdigit() and value[-1:] in "kmg":
        return None
    return 'must be "auto" or a size such as "512m" or "8g"'


def _plain_size(value: object) -> str | None:
    return None if value != "auto" and _size(value) is None else 'must be a size such as "512m"'


def _str_list(value: object) -> str | None:
    ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
    return None if ok else "must be a list of strings"


def _apt_names(value: object) -> str | None:
    bad = _str_list(value)
    if bad:
        return bad
    wrong = [v for v in value if not APT_RE.match(v)]  # type: ignore[union-attr]  # list checked above
    return f"bad apt package name {wrong[0]!r}" if wrong else None


def _env_names(value: object) -> str | None:
    bad = _str_list(value)
    if bad:
        return bad
    for name in value:  # type: ignore[union-attr]  # list checked above
        if not NAME_RE.match(name):
            return f"{name!r} is not an environment variable name"
        if SECRET_NAME_RE.search(name):
            return f"{name!r} looks like a secret; secrets never enter the box"
    return None


def _mask(value: object) -> str | None:
    if value == "default":
        return None
    bad = _str_list(value)
    return None if bad is None else 'must be "default" or a list of glob patterns'


def _non_negative(value: object) -> str | None:
    ok = isinstance(value, int | float) and not isinstance(value, bool) and value >= 0
    return None if ok else "must be >= 0"


def _port(value: object) -> str | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536
    return None if ok else "must be a TCP port"


K = config.Key
_SUBTABLES = ("deps", "limits", "transport", "checkpoints", "isolated")  # separate halkit tables

SANDBOX_TABLE = config.register(
    config.Table(
        name="sandbox",
        owner="hal-sandbox",
        keys=(
            K(
                "lockdown",
                str,
                "standard",
                choices=LOCKDOWN_LEVELS,
                doc="strict | standard | relaxed | off; changed by the user on the host only",
            ),
            K("backend", str, "auto", choices=("auto", "docker", "bwrap", "none"), doc="Box backend"),
            K("runtime", str, "auto", choices=("auto", "runc", "runsc"), doc="Docker runtime"),
            K("require", bool, False, doc="Refuse to run with backend none"),
            K("docker", str, "docker", doc="Docker CLI; env HAL_DOCKER overrides"),
            K("bwrap", str, "bwrap", doc="bwrap binary; env HAL_BWRAP overrides"),
            K("image", str, "auto", doc='"auto" or "<ref>@sha256:<digest>"'),
            K("flavor", str, "full", choices=("full", "slim"), doc="Image flavor"),
            K("node", bool, False, doc="node and npm in the image"),
            K("extra_apt_packages", list, [], check=_apt_names, doc="Apt packages added at build time"),
            K("env_passthrough", list, [], check=_env_names, doc="Extra env names copied into the box"),
            K("mount_uv_cache", bool, False, doc="Bind the uv cache read-only"),
            K("mask", (str, list), "default", check=_mask, doc='"default" or glob patterns; [] turns off'),
            K("scratch_max_bytes", int, DEFAULT_SCRATCH_MAX_BYTES, check=_positive, doc="Cap for /scratch"),
            *(K(sub, dict, {}, doc=f"The [sandbox.{sub}] table") for sub in _SUBTABLES),
        ),
    )
)

DEPS_TABLE = config.register(
    config.Table(
        name="sandbox.deps",
        owner="hal-sandbox",
        keys=(
            K("registries", list, DEFAULT_REGISTRIES, check=_str_list, doc="Hosts the prep box may reach"),
            K("timeout_s", int, 900, check=_positive, doc="Dependency prep time limit"),
        ),
    )
)

LIMITS_TABLE = config.register(
    config.Table(
        name="sandbox.limits",
        owner="hal-sandbox",
        keys=(
            K("memory", str, "auto", check=_size, doc='Box memory limit, "auto" or e.g. "8g"'),
            K("cpus", (int, float), 0, check=_non_negative, doc="CPUs, 0 = auto"),
            K("pids", int, DEFAULT_PIDS, check=_positive, doc="Max processes in the box"),
            K("nofile", int, 8192, check=_positive, doc="Max open files"),
            K("tmp_size", str, "1g", check=_plain_size, doc="Size of the /tmp tmpfs"),
            K("home_size", str, "512m", check=_plain_size, doc="Size of the home tmpfs"),
            K("stop_timeout_s", int, 10, check=_positive, doc="Grace before the box is killed"),
            K("oom_score_adj", int, 500, doc="OOM score; the box dies before the desktop"),
        ),
    )
)

TRANSPORT_TABLE = config.register(
    config.Table(
        name="sandbox.transport",
        owner="hal-sandbox",
        keys=(
            K("max_message_bytes", int, MAX_MESSAGE_BYTES, check=_positive, doc="Largest broker message"),
            K("max_connections", int, 8, check=_positive, doc="Concurrent broker connections"),
            K("partial_timeout_s", int, 30, check=_positive, doc="Time to finish a started message"),
        ),
    )
)

CHECKPOINTS_TABLE = config.register(
    config.Table(
        name="sandbox.checkpoints",
        owner="hal-sandbox",
        keys=(
            K("enabled", bool, True, doc="Take checkpoints"),
            K("max_file_bytes", int, DEFAULT_CHECKPOINT_FILE_BYTES, check=_positive, doc="Skip larger files"),
            K("nongit_max_total_bytes", int, DEFAULT_NONGIT_TOTAL_BYTES, check=_positive, doc="Shadow cap"),
            K("keep_sessions", int, 20, check=_positive, doc="Sessions of checkpoints kept"),
            K("max_age_days", int, 14, check=_positive, doc="Days of checkpoints kept"),
            K("git_timeout_s", int, 60, check=_positive, doc="Time limit for one host git call"),
        ),
    )
)

ISOLATED_TABLE = config.register(
    config.Table(
        name="sandbox.isolated",
        owner="hal-sandbox",
        keys=(
            K("max_copy_bytes", int, DEFAULT_MAX_COPY_BYTES, check=_positive, doc="Cap for an isolated copy"),
        ),
    )
)

BOX_TABLE = config.register(
    config.Table(
        name="box",
        owner="hal-sandbox",
        keys=(
            K("auto_approve", bool, True, doc="Agent CLIs in the box skip their own prompts"),
            K("model", str, "", doc='Model name for box CLIs; "" = hal-code'),
            K("max_output_tokens", int, 8192, check=_positive, doc="Output cap for box CLIs"),
            K("relay_port", int, 8741, check=_port, doc="Loopback port of the in-box model relay"),
            K("raw_terminal", bool, False, doc="Box output reaches the terminal unfiltered (warned)"),
        ),
    )
)

_TABLES = {
    "sandbox": SANDBOX_TABLE,
    "deps": DEPS_TABLE,
    "limits": LIMITS_TABLE,
    "transport": TRANSPORT_TABLE,
    "checkpoints": CHECKPOINTS_TABLE,
    "isolated": ISOLATED_TABLE,
    "box": BOX_TABLE,
}


def load(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Mapping[str, object]:
    """The resolved `[sandbox]` settings."""
    return config.load(SANDBOX_TABLE, path=path, environ=environ)


def load_all(
    path: Path | None = None, environ: Mapping[str, str] | None = None
) -> dict[str, Mapping[str, object]]:
    """Every table keyed by its last name part: `sandbox`, `limits`, `transport`, `isolated`, `box`..."""
    return {k: config.load(t, path=path, environ=environ) for k, t in _TABLES.items()}
