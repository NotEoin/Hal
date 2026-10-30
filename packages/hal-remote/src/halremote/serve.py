"""The forced command `hal-remote serve` (REQ-RMT-20 to 24, design 07 "Forced command protocol").

sshd runs it for the restricted mcp key. The verb comes from `SSH_ORIGINAL_COMMAND`, is parsed
with `shlex.split` and never reaches a shell. stdout carries protocol bytes only (one JSON line
per verb, or the hub's MCP stream after `exec`); logs go to `state_dir()/remote/serve.log` and
fatal errors to stderr. It uses nothing from `PATH`, rc files or a desktop session: the hub path
is absolute and the exec environment is built from a short allow-list.

Session cap: each `mcp-stdio` session holds an `flock` on a slot file under
`state_dir()/remote/sessions/`. The lock descriptor is inheritable, so it survives `exec` and
is released when the hub (and any child that inherited it) exits.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import platform
import shlex
import socket
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from halkit import config, paths, timeutil

from . import __version__
from .config import SERVER_TABLE
from .model import check_client_name

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 64  # sysexits EX_USAGE: unknown verb or bad invocation
EXIT_HUB_MISSING = 127  # the shell convention for "command not found"
EXIT_BUSY = 75  # sysexits EX_TEMPFAIL: try again later
VERBS = ("mcp-stdio", "hello", "check")
SCHEMA_HELLO = "hal.remote.hello/1"
SCHEMA_CHECK = "hal.remote.check/1"
SCHEMA_ERROR = "hal.remote.error/1"
SCHEMA_DELIVERY = "hal.remote.delivery/1"
GATEWAY_FORWARD = {"name": "gateway", "port": 8741, "base_path": "/v1"}  # 05-gateway's TCP port
EXEC_PATH = "/usr/bin:/bin"
PASS_THROUGH = ("XDG_RUNTIME_DIR", "HAL_CONFIG", "HAL_POLICY", "HAL_STATE_DIR", "HAL_RUNTIME_DIR")
LOG_MAX_BYTES = 1024 * 1024  # design 07, file layout: rotated at 1 MiB
LOG_KEEP = 3
Execve = Callable[[str, list[str], Mapping[str, str]], Any]


def delivery_dir() -> Path:
    """Where pending token deliveries live (`authorize` writes them, `hello` consumes them)."""
    return paths.state_dir() / "remote" / "delivery"


def _emit(obj: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")  # default ensure_ascii=True keeps the line ASCII
    sys.stdout.flush()


def _error(code: str, message: str) -> None:
    _emit({"schema": SCHEMA_ERROR, "error": code, "message": message})


def _username() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return os.environ.get("USER", "")


def _server_info() -> dict[str, str]:
    return {"hostname": socket.gethostname(), "user": _username(), "hal_version": __version__}


def take_delivery(
    client: str, ttl_s: int, now: float, directory: Path | None = None
) -> dict[str, str] | None:
    """Consumes the pending token delivery of `client`: at most once, `None` when absent or expired.

    The file is renamed before it is read, so two concurrent `hello` calls cannot both get it.
    """
    directory = directory or delivery_dir()
    src = directory / f"{client}.json"
    taken = directory / f".{client}.{os.getpid()}.taken"
    try:
        age = now - src.stat().st_mtime
        os.rename(src, taken)
    except FileNotFoundError:
        return None
    try:
        if age > ttl_s:
            log.info("delivery for %s expired after %.0f s", client, age)
            return None
        data = json.loads(taken.read_text(encoding="utf-8"))
        tokens = data.get("tokens") if isinstance(data, dict) else None
        if data.get("schema") != SCHEMA_DELIVERY or not isinstance(tokens, dict):
            raise ValueError("unexpected delivery file")
        return {str(k): str(v) for k, v in tokens.items()}
    finally:
        taken.unlink(missing_ok=True)


def _hello(client: str, ttl_s: int, now: float) -> int:
    try:
        tokens = take_delivery(client, ttl_s, now)
    except (OSError, ValueError) as exc:
        log.error("delivery for %s unreadable: %s", client, exc)
        _error("delivery_failed", "the pending token delivery could not be read")
        return EXIT_FAILED
    _emit(
        {
            "schema": SCHEMA_HELLO,
            "client": client,
            "time": timeutil.iso_z(),
            "server": _server_info(),
            "forwards": [GATEWAY_FORWARD],
            "tokens": tokens,
        }
    )
    return EXIT_OK


def _check(client: str, hub: Path) -> int:
    problems: list[str] = []
    runtime = ""
    try:
        runtime = str(paths.runtime_dir())
    except Exception as exc:  # noqa: BLE001 - any failure of the runtime rule is reported as a problem
        problems.append(f"runtime_dir: {exc}")
    executable = hub.is_absolute() and hub.is_file() and os.access(hub, os.X_OK)
    if not executable:
        problems.append(f"hub is not an executable file: {hub}")
    _emit(
        {
            "schema": SCHEMA_CHECK,
            "ok": not problems,
            "time": timeutil.iso_z(),
            "client": client,
            "python": platform.python_version(),
            "hal_version": __version__,
            "hub": {"path": str(hub), "executable": executable},
            "runtime_dir": runtime,
            "problems": problems,
        }
    )
    return EXIT_OK if not problems else EXIT_FAILED


def _private_dir(*parts: str) -> Path:
    """Creates `state_dir()/<parts>` level by level so every level is a private directory."""
    path = paths.state_dir()
    paths.ensure_private_dir(path)
    for part in parts:
        path = paths.ensure_private_dir(path / part)
    return path


def _acquire_slot(client: str, max_sessions: int) -> int | None:
    """Locks a free session slot and returns its inheritable descriptor, `None` when all are taken."""
    import fcntl

    directory = _private_dir("remote", "sessions")
    for n in range(max_sessions):
        fd = os.open(directory / f"{client}.{n}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        os.set_inheritable(fd, True)  # survives exec, so the lock lasts as long as the hub
        return fd
    return None


def build_env(client: str, environ: Mapping[str, str]) -> dict[str, str]:
    """The minimal environment of the hub (REQ-RMT-22); nothing else from sshd's is passed on."""
    env = {"PATH": EXEC_PATH, "HAL_REMOTE_CLIENT": client}
    for key in ("HOME", "USER", "LANG"):
        if key in environ:
            env[key] = environ[key]
    for key in PASS_THROUGH:
        if environ.get(key):
            env[key] = environ[key]
    addr = environ.get("SSH_CONNECTION", "").split(" ")[0]
    if addr:
        env["HAL_REMOTE_ADDR"] = addr
    return env


def _mcp_stdio(client: str, hub: Path, environ: Mapping[str, str], max_sessions: int, execve: Execve) -> int:
    try:
        paths.runtime_dir()
    except Exception as exc:  # noqa: BLE001 - reported to the client as a JSON error, never guessed around
        log.error("runtime dir: %s", exc)
        _error("runtime_dir", str(exc))
        return EXIT_FAILED
    if not (hub.is_absolute() and hub.is_file() and os.access(hub, os.X_OK)):
        log.error("hub missing: %s", hub)
        _error("hub_missing", f"hal-hub is not executable at {hub}")
        return EXIT_HUB_MISSING
    if _acquire_slot(client, max_sessions) is None:
        log.warning("session cap %d reached for %s", max_sessions, client)
        _error("busy", f"{max_sessions} sessions are already open for this client")
        return EXIT_BUSY
    argv = [str(hub), "mcp", "--transport", "stdio", "--channel", "remote", "--origin", f"remote:{client}"]
    log.info("exec hub for %s", client)
    execve(str(hub), argv, build_env(client, environ))
    return EXIT_FAILED  # only reached when a patched `execve` returns


def _setup_logging() -> None:
    """Logs to the rotated file; a log that cannot be opened must not stop the session."""
    try:
        directory = _private_dir("remote")
        handler = logging.handlers.RotatingFileHandler(
            directory / "serve.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_KEEP, encoding="utf-8"
        )
    except OSError as exc:
        sys.stderr.write(f"hal-remote serve: cannot open the log file: {exc}\n")
        return
    handler.setFormatter(logging.Formatter("%(asctime)sZ %(levelname)s %(message)s"))
    root = logging.getLogger("halremote")
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    execve: Execve = os.execve,
    now: float | None = None,
) -> int:
    """Runs the forced command and returns its exit code (does not return after a successful exec)."""
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="hal-remote serve", add_help=False, exit_on_error=False)
    parser.add_argument("--client", required=True)
    parser.add_argument("--hub", required=True)
    try:
        args = parser.parse_args(list(argv or []))
        client = check_client_name(args.client)
    except (argparse.ArgumentError, SystemExit, ValueError) as exc:
        _error("bad_invocation", f"serve needs --client NAME --hub PATH: {exc}")
        return EXIT_USAGE
    _setup_logging()
    try:
        words = shlex.split(environ.get("SSH_ORIGINAL_COMMAND", ""))
    except ValueError:
        words = ["?"]
    verb = words[0] if len(words) == 1 else ("mcp-stdio" if not words else "?")
    log.info("client %s verb %s", client, verb)
    try:
        cfg = config.load(SERVER_TABLE, environ=environ)
    except config.ConfigError as exc:
        log.error("config: %s", exc)
        _error("config", str(exc))
        return EXIT_FAILED
    if verb == "hello":
        return _hello(client, int(cfg["delivery_ttl_s"]), time.time() if now is None else now)
    if verb == "check":
        return _check(client, Path(args.hub))
    if verb == "mcp-stdio":
        return _mcp_stdio(client, Path(args.hub), environ, int(cfg["max_sessions"]), execve)
    _error("unknown_verb", "verb must be one of " + ", ".join(VERBS))
    return EXIT_USAGE
