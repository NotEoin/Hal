#!/usr/bin/env python3
"""`hal-box-entry`: the box's entrypoint (02-sandbox REQ-BOX-34, REQ-BOX-36).

Stdlib only; it runs as a lone file in the image and as a console script on the host. It seeds
`$HOME`, holds the lifeline open, optionally starts the relay, runs the child in its own process
group and ends it when the host side closes the lifeline. Logs go to a file, never stdout, because
stdout is the ACP stream or the CLI's terminal.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import importlib.util
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

LIFELINE_SOCKET = "/run/hal/lifeline.sock"
LOG_PATH = "/tmp/hal-box-entry.log"  # noqa: S108 - inside the box, where /tmp is a private tmpfs
CONNECT_TRIES = 20
CONNECT_PAUSE_S = 0.1
DEFAULT_GRACE_S = 5.0
PROBE_TIMEOUT_S = 5.0
SELFTEST_TIMEOUT_S = 5.0
EXIT_USAGE = 64
EXIT_UNAVAILABLE = 69
EXIT_INTERNAL = 70
EXIT_LIFELINE = 143
SEED_FILE_MODE = 0o600
SEED_DIR_MODE = 0o700
IMAGE_SEED_GLOB = "/opt/hal/box/*/home-seed"
SESSION_SEED = "/run/hal/home-seed"
SELFTEST_TOOLS = (
    "bash",
    "git",
    "rg",
    "fd",
    "make",
    "python3",
    "uv",
    "sed",
    "gawk",
    "patch",
    "file",
    "less",
    "pkg-config",
)

log = logging.getLogger("hal-box-entry")


def copy_seed(seed: Path, home: Path) -> int:
    """Copies `seed` into `home` (files 0600, dirs 0700), skipping symlinks; returns files copied."""
    if not seed.is_dir() or seed.is_symlink():
        return 0
    copied = 0
    for root, dirs, files in os.walk(seed, followlinks=False):
        rel = Path(root).relative_to(seed)
        target = home / rel
        target.mkdir(mode=SEED_DIR_MODE, parents=True, exist_ok=True)
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for name in files:
            src = Path(root) / name
            if src.is_symlink() or not src.is_file():
                continue
            shutil.copyfile(src, target / name)
            os.chmod(target / name, SEED_FILE_MODE)
            copied += 1
    return copied


def connect_lifeline(
    path: str, tries: int = CONNECT_TRIES, pause: float = CONNECT_PAUSE_S
) -> socket.socket | None:
    """Connects to the lifeline socket, retrying; None when it never answers."""
    for attempt in range(tries):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
            return sock
        except OSError:
            sock.close()
            if attempt + 1 < tries:
                time.sleep(pause)
    return None


def _kill_group(child: subprocess.Popen[bytes], grace_s: float) -> None:
    """SIGTERM the child's process group, SIGKILL it after `grace_s`."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(child.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if child.poll() is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, signal.SIGKILL)


def _watch(
    sock: socket.socket, child: subprocess.Popen[bytes], grace_s: float, fired: threading.Event
) -> None:
    """Reads the lifeline; on EOF, an error or a stop message ends the child."""
    buf = b""
    while True:
        try:
            data = sock.recv(4096)
        except OSError:
            data = b""
        if not data:
            break
        buf += data
        stop = False
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            with contextlib.suppress(ValueError):
                msg = json.loads(line)
                if isinstance(msg, dict) and msg.get("hal") == "stop":
                    grace_s = float(msg.get("grace_s", grace_s))
                    stop = True
        if stop:
            break
    if child.poll() is None:
        fired.set()
        _kill_group(child, grace_s)


def run(
    child_argv: list[str],
    *,
    lifeline: str = LIFELINE_SOCKET,
    seeds: list[Path] | None = None,
    home: Path | None = None,
    relay_port: int | None = None,
    relay_socket: str = "/run/hal/model.sock",
    grace_s: float = DEFAULT_GRACE_S,
    tries: int = CONNECT_TRIES,
) -> int:
    """Runs `child_argv` under the lifeline; returns the exit status."""
    home = home or Path(os.environ.get("HOME", "/home/hal"))
    for seed in (
        seeds if seeds is not None else [*map(Path, sorted(_glob(IMAGE_SEED_GLOB))), Path(SESSION_SEED)]
    ):
        copy_seed(seed, home)
    sock = connect_lifeline(lifeline, tries)
    if sock is None:
        sys.stderr.write(f"hal-box-entry: lifeline {lifeline} unavailable\n")
        return EXIT_UNAVAILABLE
    if relay_port is not None:
        _load_sibling("hal_relay").Relay(relay_port, relay_socket).start()
    try:
        child = subprocess.Popen(child_argv, start_new_session=True)  # noqa: S603 - the host chose the argv
    except OSError as exc:
        sys.stderr.write(f"hal-box-entry: cannot start {child_argv[0]}: {exc}\n")
        return EXIT_UNAVAILABLE
    fired = threading.Event()
    threading.Thread(target=_watch, args=(sock, child, grace_s, fired), daemon=True).start()

    def forward(signum: int, _frame: object) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    code = child.wait()
    if fired.is_set():
        return EXIT_LIFELINE
    return code if code >= 0 else 128 - code


def _load_sibling(name: str) -> ModuleType:
    """Loads `<name>.py` next to this file; works as a lone file in the image and in the package."""
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _glob(pattern: str) -> list[str]:
    return glob.glob(pattern)


def probe(socket_path: str) -> int:
    """Connects to `socket_path` and reads one line; 0 on success (backs the runsc probe)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(PROBE_TIMEOUT_S)
    try:
        sock.connect(socket_path)
        sock.recv(1024)
    except OSError as exc:
        sys.stderr.write(f"hal-box-entry: probe failed: {exc}\n")
        return EXIT_UNAVAILABLE
    return 0


def selftest() -> int:
    """Checks the image tools; prints one JSON object of versions; 0 when all are present."""
    versions: dict[str, str] = {}
    missing: list[str] = []
    for tool in SELFTEST_TOOLS:
        path = shutil.which(tool)
        if path is None:
            missing.append(tool)
            continue
        try:
            out = subprocess.run(  # noqa: S603 - fixed tool names
                [path, "--version"], capture_output=True, text=True, timeout=SELFTEST_TIMEOUT_S, check=False
            )
            versions[tool] = (out.stdout or out.stderr).splitlines()[0] if (out.stdout or out.stderr) else ""
        except (OSError, subprocess.SubprocessError):
            missing.append(tool)
    sys.stdout.write(json.dumps({"versions": versions, "missing": missing}) + "\n")
    return 0 if not missing else EXIT_UNAVAILABLE


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the exit status."""
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(filename=LOG_PATH, level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="hal-box-entry")
    parser.add_argument("--relay-port", type=int)
    parser.add_argument("--relay-socket", default="/run/hal/model.sock")
    parser.add_argument("--seed", action="append", type=Path)
    parser.add_argument("--grace", type=float, default=DEFAULT_GRACE_S)
    parser.add_argument("--lifeline", default=LIFELINE_SOCKET)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--probe", metavar="SOCKET")
    if "--" in argv:
        cut = argv.index("--")
        argv, child = argv[:cut], argv[cut + 1 :]
    else:
        child = []
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else 0
    if args.selftest:
        return selftest()
    if args.probe:
        return probe(args.probe)
    if not child:
        sys.stderr.write("hal-box-entry: no child command after --\n")
        return EXIT_USAGE
    try:
        return run(
            child,
            lifeline=args.lifeline,
            seeds=args.seed,
            relay_port=args.relay_port,
            relay_socket=args.relay_socket,
            grace_s=args.grace,
        )
    except Exception:  # noqa: BLE001 - logged and mapped to the internal-error exit code
        log.exception("internal error")
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
