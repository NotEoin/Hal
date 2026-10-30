"""The environment of a box: a fixed set of variables plus an allowlist from the host.

Everything not named here is dropped, so a host secret can never reach the box by being in the
caller's environment (02-sandbox "Environment", REQ-BOX-33). Values are passed on the docker
argv, so none may be secret.
"""

from __future__ import annotations

from collections.abc import Mapping

from .config import NAME_RE, SECRET_NAME_RE

PASSTHROUGH = (
    "TERM",
    "COLORTERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TZ",
    "NO_COLOR",
    "FORCE_COLOR",
    "COLUMNS",
    "LINES",
)
BOX_PATH = "/opt/hal/venv/bin:/opt/hal/bin:/usr/local/bin:/usr/bin:/bin"
RELAY_HOST = "127.0.0.1"
KINDS = ("box", "shell", "code", "chat", "job", "deps")
_RELAY_KINDS = ("box", "shell")  # only these run an in-box model relay
_FIXED = {
    "HOME": "/home/hal",
    "USER": "hal",
    "LOGNAME": "hal",
    "SHELL": "/bin/bash",
    "PATH": BOX_PATH,
    "LANG": "C.UTF-8",
    "TMPDIR": "/tmp",
    "HAL_BROKER_SOCKET": "/run/hal/broker.sock",
    "HAL_MODEL_SOCKET": "/run/hal/model.sock",
    "GIT_OPTIONAL_LOCKS": "0",  # .git is read-only
    "GIT_CONFIG_NOSYSTEM": "1",
    "PIP_NO_INDEX": "1",  # no network: fail fast
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "UV_OFFLINE": "1",
    "UV_PROJECT_ENVIRONMENT": "/work/.hal/venv",
    "UV_PYTHON": "/usr/bin/python3",
    "UV_PYTHON_DOWNLOADS": "never",
    "CARGO_HOME": "/work/.hal/cargo",
    "CARGO_NET_OFFLINE": "true",
    "NPM_CONFIG_OFFLINE": "true",
    "NPM_CONFIG_UPDATE_NOTIFIER": "false",
    "NO_UPDATE_NOTIFIER": "1",
    "DO_NOT_TRACK": "1",  # RESOLUTIONS I9: no telemetry
}


def box_env(
    kind: str,
    sid: str,
    backend: str,
    host_env: Mapping[str, str],
    extra_passthrough: tuple[str, ...] | list[str] = (),
    *,
    relay_port: int = 8741,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The complete environment for a box of `kind` in session `sid`.

    Raises `ValueError` for an unknown kind or a pass-through name that is malformed or looks
    like a secret. `extra` is applied last and is the caller's own trusted values.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown box kind {kind!r}; use one of {KINDS}")
    for name in extra_passthrough:
        if not NAME_RE.match(name) or SECRET_NAME_RE.search(name):
            raise ValueError(f"cannot pass {name!r} into the box: not a plain, non-secret name")
    env = dict(_FIXED)
    env["HAL_SESSION"] = sid
    env["HAL_SANDBOX"] = backend
    if kind in _RELAY_KINDS:
        env["HAL_MODEL_URL"] = f"http://{RELAY_HOST}:{relay_port}"
    if kind == "box":
        env["HAL_HARNESS"] = "1"  # REQ-SEC-26: `hal approve` cannot run from inside
    for name in (*PASSTHROUGH, *extra_passthrough):
        if host_env.get(name):
            env[name] = host_env[name]
    env.update(extra or {})
    return env
