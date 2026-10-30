"""Fixture for the sshd tests: an unprivileged sshd on 127.0.0.1 (design 07, "sshd tests").

It needs no root and changes nothing on the host: its own host key, config, authorized_keys and
pid file live in a temp directory. It is started with `-D -e` because daemon mode reset every
connection in the 2026-10-24 experiment (cause not investigated). `HAL_TEST_SSHD` selects the
binary; the tests skip without one. `SetEnv` points the forced command's state at the temp dir,
so a test never writes to the real `~/.local/state/hal`.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

SSHD_DEFAULT = "/usr/sbin/sshd"
START_TIMEOUT_S = 15
SSH_TIMEOUT_S = 30  # each test also has a 60 s ceiling through the marker


def find_sshd() -> Path | None:
    """The sshd binary named by `HAL_TEST_SSHD`, else the system one, else `None`."""
    path = Path(os.environ.get("HAL_TEST_SSHD", SSHD_DEFAULT))
    return path if path.is_file() and os.access(path, os.X_OK) else None


def free_port() -> int:
    """A currently unused loopback TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def generate_key(path: Path) -> str:
    """Writes an unprotected ed25519 key pair at `path` and returns the public line."""
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path), "-C", ""],
        check=True,
        timeout=SSH_TIMEOUT_S,
    )
    return path.with_suffix(".pub").read_text().strip()


@dataclass
class Sshd:
    """A running test sshd and helpers to talk to it."""

    root: Path
    port: int
    proc: subprocess.Popen[bytes]
    log: Path
    authorized_keys: Path
    state: Path
    extra: list[subprocess.Popen[bytes]] = field(default_factory=list)

    def new_key(self, name: str) -> Path:
        """Makes a client key pair `root/name` and returns the private key path."""
        key = self.root / name
        generate_key(key)
        return key

    def authorize(self, line: str) -> None:
        """Appends one `authorized_keys` line."""
        with self.authorized_keys.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self.authorized_keys.chmod(0o600)

    def ssh_argv(self, key: Path, *options: str) -> list[str]:
        """The ssh command line to this server with `key`, ignoring the user's own config."""
        return [
            "ssh", "-F", "/dev/null", "-p", str(self.port), "-i", str(key),
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={self.root / 'kh'}",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
            *options, f"{os.environ.get('USER') or Path.home().name}@127.0.0.1",
        ]  # fmt: skip

    def run(self, key: Path, *options: str, command: str = "") -> subprocess.CompletedProcess[str]:
        """Runs `ssh ... [command]` to completion."""
        argv = self.ssh_argv(key, *options) + ([command] if command else [])
        return subprocess.run(argv, capture_output=True, text=True, timeout=SSH_TIMEOUT_S, check=False)

    def hold(self, key: Path, *options: str) -> subprocess.Popen[str]:
        """Starts `ssh -N ...` in the background; the fixture kills it at teardown."""
        proc = subprocess.Popen(
            self.ssh_argv(key, "-N", *options), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.extra.append(proc)
        return proc


def wait_listening(port: int, timeout_s: float, proc: subprocess.Popen[str] | None = None) -> bool:
    """True once something accepts on `127.0.0.1:port`; false on timeout or when `proc` exits."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.1)
    return False


@pytest.fixture
def start_sshd(tmp_path: Path) -> Iterator:
    """Factory: `start_sshd(extra_config="")` returns a running `Sshd`."""
    binary = find_sshd()
    if binary is None:
        pytest.skip("no sshd binary: set HAL_TEST_SSHD (see docs/dev/remote.md)")
    started: list[Sshd] = []

    def factory(extra_config: str = "") -> Sshd:
        root = tmp_path / f"sshd{len(started)}"
        root.mkdir(mode=0o700)
        state = root / "state"
        runtime = root / "run"
        runtime.mkdir(mode=0o700)
        generate_key(root / "hostkey")
        akeys = root / "authorized_keys"
        akeys.write_text("")
        akeys.chmod(0o600)
        port = free_port()
        helpers = ""
        # OpenSSH 10 splits the session and auth helpers out; an extracted package keeps them
        # next to the binary, the installed one finds them in /usr/lib/openssh.
        for name, keyword in (("sshd-session", "SshdSessionPath"), ("sshd-auth", "SshdAuthPath")):
            beside = binary.parent.parent / "lib" / "openssh" / name
            if beside.is_file() and binary != Path(SSHD_DEFAULT):
                helpers += f"{keyword} {beside}\n"
        (root / "sshd_config").write_text(
            f"ListenAddress 127.0.0.1\nPort {port}\nHostKey {root / 'hostkey'}\n"
            f"AuthorizedKeysFile {akeys}\nPidFile {root / 'sshd.pid'}\nUsePAM no\n"
            f"PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
            # temp directories have sticky world-writable parents, which StrictModes rejects
            f"StrictModes no\n{helpers}"
            f"SetEnv HAL_STATE_DIR={state} HAL_RUNTIME_DIR={runtime} HAL_CONFIG={root / 'none.toml'}\n"
            f"{extra_config}\n"
        )
        log = root / "sshd.log"
        with log.open("wb") as fh:
            proc = subprocess.Popen(
                [str(binary), "-D", "-e", "-f", str(root / "sshd_config")], stdout=fh, stderr=fh
            )
        server = Sshd(root, port, proc, log, akeys, state)
        started.append(server)
        if not wait_listening(port, START_TIMEOUT_S):
            pytest.fail(f"sshd did not start: {log.read_text(errors='replace')[-500:]}")
        return server

    yield factory
    for server in started:
        for child in server.extra:
            child.kill()
            child.communicate(timeout=10)
        server.proc.terminate()
        try:
            server.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.proc.kill()


@pytest.fixture
def hal_remote_exe(tmp_path: Path) -> Path:
    """An absolute launcher for `python -m halremote`, as the `command=` option needs a path."""
    exe = tmp_path / "hal-remote"
    exe.write_text(f'#!/bin/sh\nexec {sys.executable} -m halremote "$@"\n')
    exe.chmod(0o755)
    return exe


@pytest.fixture
def fake_hub(tmp_path: Path) -> Path:
    """An executable stand-in for `hal-hub` that prints its argv and the Hal env, then exits."""
    hub = tmp_path / "fake-hub"
    hub.write_text(
        '#!/bin/sh\nprintf "hub %s\\n" "$*"\n'
        'printf "client %s addr %s\\n" "$HAL_REMOTE_CLIENT" "$HAL_REMOTE_ADDR"\n'
    )
    hub.chmod(0o755)
    return hub


@pytest.fixture
def echo_server() -> Iterator[int]:
    """A TCP server on a free loopback port that sends `hello` to each connection."""
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    stop = threading.Event()

    def serve() -> None:
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            with conn:
                conn.sendall(b"hello")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield int(srv.getsockname()[1])
    stop.set()
    thread.join(timeout=2)
    srv.close()
