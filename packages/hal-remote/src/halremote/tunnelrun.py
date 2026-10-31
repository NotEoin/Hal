"""The body of `hal-remote tunnel-run`: wires the real supervisor and signals (design 07, Tunnel supervisor).

Kept apart from `cli` because it owns logging to `tunnel.log` and the stop signals, not output.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import random
import signal
import threading

from halkit import paths

from . import pairing, sshconf, supervisor
from .model import profile_paths

LOG_FILE = "tunnel.log"
LOG_MAX_BYTES = 1024 * 1024  # design 07, file layout: rotated at 1 MiB
LOG_KEEP = 3


def default_mode() -> supervisor.Mode:
    """`systemd` when systemd started this process (it sets `INVOCATION_ID`), else `foreground`."""
    return "systemd" if os.environ.get("INVOCATION_ID") else "foreground"


def run(profile_name: str, mode: supervisor.Mode, ssh_override: str = "") -> int:
    """Runs the supervisor of `profile_name` until SIGTERM or SIGINT; returns its exit code."""
    profile = pairing.load_profile(profile_name)
    pp = profile_paths(profile_name)
    paths.ensure_private_dir(pp.state)
    handler = logging.handlers.RotatingFileHandler(
        pp.state / LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_KEEP, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)sZ %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("halremote")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        sup = supervisor.Supervisor(
            profile,
            pp,
            ssh=sshconf.find_ssh(ssh_override),
            clock=supervisor.RealClock(),
            spawner=supervisor.SubprocessSpawner(),
            prober=supervisor.HttpProber(),
            rng=random.Random(),  # noqa: S311 - backoff jitter, not a secret
            mode=mode,
        )
        return sup.run(stop)
    finally:
        root.removeHandler(handler)
        handler.close()
