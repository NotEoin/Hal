"""Where Hal keeps its files: config, policy, state, data and the per-user runtime directory.

One module decides every path so packages never build their own. Environment variables are
read at call time, never at import, so tests and daemons see the current environment. The
user id is read on first use, never at import, so this module imports on Windows.

`HAL_STATE_DIR`, `HAL_DATA_DIR` and `HAL_RUNTIME_DIR` move a whole install, so two installs with
different values share nothing (ported from the per-install state directory of hal-private,
commit b279740). The older `HAL_STATE` and `HAL_DATA` still work and log one warning.

Runtime directory (resolution F6), first match wins: `HAL_RUNTIME_DIR`, then
`$XDG_RUNTIME_DIR/hal`, then
`/run/user/<uid>/hal` when `/run/user/<uid>` exists, then `%LOCALAPPDATA%\\hal\\run` on
Windows. There is no `/tmp` fallback: a world-writable parent is not a place for sockets.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path

log = logging.getLogger(__name__)

# A sockaddr_un path holds 108 bytes including the NUL terminator.
MAX_SOCKET_PATH_BYTES = 107
RUN_USER_ROOT = Path("/run/user")  # systemd-logind's per-user runtime parent
SESSION_ID_RE = re.compile(r"[0-9a-f]{12}")  # 12 lowercase hex characters (resolution E2)
PRIVATE_DIR_MODE = 0o700


class UnsafePathError(Exception):
    """A directory is owned by another user or open to group or others."""


class RuntimeDirMissing(Exception):
    """No rule yields a runtime directory on this machine."""


class SocketPathTooLong(ValueError):
    """A unix socket path exceeds what `sockaddr_un` can hold."""


class PathNotAllowed(ValueError):
    """A path is outside every allowed root or inside a denied one."""


def _inside(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def confine(path: str, roots: Sequence[Path], deny: Sequence[Path]) -> Path:
    """The resolved `path`, if it is inside a root in `roots` and inside no entry of `deny`.

    Expands `~`, resolves symlinks and `..` (a missing tail is resolved as far as it exists, so a
    new file can be confined), then compares whole path components, never string prefixes:
    `/data/ab` is not inside `/data/a`. Raises `PathNotAllowed` otherwise, and when `roots` is
    empty. `deny` wins over `roots`. Opening the result safely is `halkit.fsafe.open_beneath`.
    """
    if not path or "\0" in path:
        raise PathNotAllowed("empty path or NUL byte")
    resolved = Path(path).expanduser().resolve(strict=False)
    allowed = [Path(r).expanduser().resolve(strict=False) for r in roots]
    if not any(_inside(resolved, root) for root in allowed):
        raise PathNotAllowed(f"{resolved} is outside the allowed roots")
    for entry in deny:
        if _inside(resolved, Path(entry).expanduser().resolve(strict=False)):
            raise PathNotAllowed(f"{resolved} is inside the denied path {entry}")
    return resolved


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


_warned: set[str] = set()  # deprecated variables already reported in this process


def _deprecated(old: str, new: str) -> str:
    """Returns the value of the deprecated variable `old`, warning once per process."""
    value = _env(old)
    if value and old not in _warned:
        _warned.add(old)
        log.warning("%s is deprecated; use %s", old, new)
    return value


def _xdg(var: str, fallback: str) -> Path:
    value = _env(var)
    return Path(value).expanduser() if value else Path.home() / fallback


def _is_windows() -> bool:
    return os.name == "nt"


def _uid() -> int:
    return os.getuid()


def config_file() -> Path:
    """`HAL_CONFIG`, else `$XDG_CONFIG_HOME/hal/config.toml`."""
    override = _env("HAL_CONFIG")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_CONFIG_HOME", ".config") / "hal" / "config.toml"


def config_dir() -> Path:
    """The directory that holds `config_file()`."""
    return config_file().parent


def policy_file() -> Path:
    """`HAL_POLICY`, else `policy.toml` beside the config file."""
    override = _env("HAL_POLICY")
    if override:
        return Path(override).expanduser()
    return config_dir() / "policy.toml"


def state_dir() -> Path:
    """`HAL_STATE_DIR`, else `HAL_STATE` (deprecated), else `$XDG_STATE_HOME/hal`."""
    override = _env("HAL_STATE_DIR") or _deprecated("HAL_STATE", "HAL_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_STATE_HOME", ".local/state") / "hal"


def data_dir() -> Path:
    """`HAL_DATA_DIR`, else `HAL_DATA` (deprecated), else `$XDG_DATA_HOME/hal`."""
    override = _env("HAL_DATA_DIR") or _deprecated("HAL_DATA", "HAL_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_DATA_HOME", ".local/share") / "hal"


def runtime_dir() -> Path:
    """The per-user runtime directory for sockets and session files (rule in the module header).

    Raises `UnsafePathError` when the directory exists but is owned by another user or is open
    to group or others, and `RuntimeDirMissing` when no rule applies.
    """
    path = _runtime_candidate()
    if path.exists():
        _check_private(path)
    return path


def _runtime_candidate() -> Path:
    override = _env("HAL_RUNTIME_DIR")
    if override:
        return Path(override).expanduser()
    xdg = _env("XDG_RUNTIME_DIR")
    if _is_windows():
        local = _env("LOCALAPPDATA")
        if local:
            return Path(local) / "hal" / "run"
    elif xdg:
        return Path(xdg) / "hal"
    else:
        user_root = RUN_USER_ROOT / str(_uid())
        if user_root.is_dir():
            return user_root / "hal"
    raise RuntimeDirMissing("no runtime directory found: set HAL_RUNTIME_DIR to a directory you own")


def session_dir(session_id: str) -> Path:
    """`runtime_dir()/s/<session_id>`; raises `ValueError` unless the id is 12 lowercase hex characters."""
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"bad session id {session_id!r}: expected 12 lowercase hex characters")
    return runtime_dir() / "s" / session_id


def _check_private(path: Path) -> None:
    if _is_windows():
        return  # POSIX owner and mode bits do not describe Windows access control
    info = path.stat()
    if info.st_uid != _uid():
        raise UnsafePathError(f"{path} is owned by uid {info.st_uid}, not by you")
    if info.st_mode & 0o077:
        mode = info.st_mode & 0o777
        raise UnsafePathError(f"{path} has mode {mode:04o}; it must be {PRIVATE_DIR_MODE:04o}")


def ensure_private_dir(path: Path) -> Path:
    """Creates `path` with mode 0700 if missing and returns it.

    Raises `UnsafePathError` when it already exists with another owner or open permissions.
    The check never loosens or repairs an existing directory.
    """
    path = Path(path)
    if path.exists():
        _check_private(path)
        return path
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    _check_private(path)
    return path


def check_socket_path(path: Path) -> Path:
    """Returns `path` unchanged; raises `SocketPathTooLong` above 107 bytes."""
    size = len(os.fsencode(path))
    if size > MAX_SOCKET_PATH_BYTES:
        raise SocketPathTooLong(f"socket path is {size} bytes, the limit is {MAX_SOCKET_PATH_BYTES}: {path}")
    return Path(path)
