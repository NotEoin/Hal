"""Tests for the box-side programs: entry, relay and bridge (REQ-BOX-34 to REQ-BOX-38)."""

import ast
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from halsandbox import boxside
from halsandbox.boxside import hal_box_entry, hal_relay
from halsandbox.lifeline import Lifeline

BOXSIDE = Path(boxside.__file__).parent
SID = "0123456789ab"


def test_boxside_modules_import_only_stdlib():
    for path in BOXSIDE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                assert top in sys.stdlib_module_names or top == "__future__", f"{path.name} imports {name}"


def entry(sockdir: Path, child: list[str], *extra: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "halsandbox.boxside.hal_box_entry", "--lifeline", str(sockdir / "l.sock"),
         "--seed", str(sockdir / "noseed"), *extra, "--", *child],
        env={**os.environ, "HOME": str(sockdir)},
    )  # fmt: skip


def test_child_exit_code_is_the_entry_exit_code(sockdir):
    lf = Lifeline(sockdir / "l.sock", SID)
    lf.start()
    try:
        proc = entry(sockdir, [sys.executable, "-c", "raise SystemExit(7)"])
        assert proc.wait(timeout=20) == 7
    finally:
        lf.close()


def test_lifeline_close_terminates_child(sockdir):
    lf = Lifeline(sockdir / "l.sock", SID)
    lf.start()
    proc = entry(sockdir, [sys.executable, "-c", "import time; time.sleep(60)"], "--grace", "1")
    try:
        deadline = time.monotonic() + 10
        while lf.connections() == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        lf.close()
        assert proc.wait(timeout=15) == hal_box_entry.EXIT_LIFELINE
    finally:
        proc.kill()


def test_stop_message_kills_a_child_that_ignores_sigterm(sockdir):
    lf = Lifeline(sockdir / "l.sock", SID)
    lf.start()
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    proc = entry(sockdir, [sys.executable, "-c", code], "--grace", "1")
    try:
        deadline = time.monotonic() + 10
        while lf.connections() == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        lf.request_stop(1.0)
        assert proc.wait(timeout=15) == hal_box_entry.EXIT_LIFELINE
    finally:
        lf.close()
        proc.kill()


def test_missing_lifeline_exits_69_without_starting_the_child(sockdir):
    marker = sockdir / "ran"
    rc = hal_box_entry.run(
        ["touch", str(marker)], lifeline=str(sockdir / "none.sock"), seeds=[], home=sockdir, tries=2
    )
    assert rc == hal_box_entry.EXIT_UNAVAILABLE and not marker.exists()


def test_no_child_command_is_a_usage_error(capsys):
    assert hal_box_entry.main([]) == hal_box_entry.EXIT_USAGE


def test_seed_copy_sets_modes_and_skips_symlinks(tmp_path):
    seed, home = tmp_path / "seed", tmp_path / "home"
    (seed / "d").mkdir(parents=True)
    (seed / "d" / "a.txt").write_text("x")
    (seed / "evil").symlink_to("/etc/passwd")
    (seed / "dir_link").symlink_to(tmp_path)
    assert hal_box_entry.copy_seed(seed, home) == 1
    assert (home / "d" / "a.txt").stat().st_mode & 0o777 == 0o600
    assert (home / "d").stat().st_mode & 0o777 == 0o700
    assert not (home / "evil").exists() and not (home / "dir_link").exists()


def test_probe_reads_one_line_from_the_socket(sockdir):
    path = sockdir / "p.sock"
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen(1)
    threading.Thread(target=lambda: srv.accept()[0].sendall(b"ok\n"), daemon=True).start()
    assert hal_box_entry.probe(str(path)) == 0
    assert hal_box_entry.probe(str(sockdir / "gone.sock")) == hal_box_entry.EXIT_UNAVAILABLE


class Echo:
    """A unix socket server that echoes after a short pause per chunk."""

    def __init__(self, path: Path) -> None:
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(str(path))
        self.srv.listen(8)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    @staticmethod
    def _one(conn: socket.socket) -> None:
        while data := conn.recv(4096):
            conn.sendall(data)
        conn.close()


def test_relay_streams_without_buffering(sockdir):
    Echo(sockdir / "m.sock")
    relay = hal_relay.Relay(0, str(sockdir / "m.sock"))
    port = relay.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            for i in range(3):
                t0 = time.monotonic()
                c.sendall(b"ping%d" % i)
                assert c.recv(16) == b"ping%d" % i
                assert time.monotonic() - t0 < 0.5
    finally:
        relay.stop()


def test_relay_listens_on_loopback_only(sockdir):
    relay = hal_relay.Relay(0, str(sockdir / "m.sock"))
    relay.start()
    try:
        assert relay._server.getsockname()[0] == "127.0.0.1"
    finally:
        relay.stop()


def test_relay_closes_connections_over_the_cap(sockdir):
    Echo(sockdir / "m.sock")
    relay = hal_relay.Relay(0, str(sockdir / "m.sock"), max_conns=1)
    port = relay.start()
    try:
        first = socket.create_connection(("127.0.0.1", port), timeout=5)
        first.sendall(b"a")
        assert first.recv(4) == b"a"
        second = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert second.recv(4) == b""
        first.close()
        second.close()
    finally:
        relay.stop()


def test_bridge_splices_stdin_and_stdout_to_the_socket(sockdir):
    Echo(sockdir / "b.sock")
    out = subprocess.run(
        [sys.executable, "-m", "halsandbox.boxside.hal_mcp_bridge", "--socket", str(sockdir / "b.sock")],
        input=b'{"jsonrpc":"2.0"}\n', capture_output=True, timeout=20,
    )  # fmt: skip
    assert out.returncode == 0 and out.stdout == b'{"jsonrpc":"2.0"}\n'


def test_bridge_exits_69_and_writes_nothing_to_stdout_when_unreachable(sockdir):
    out = subprocess.run(
        [sys.executable, "-m", "halsandbox.boxside.hal_mcp_bridge", "--socket", str(sockdir / "no.sock")],
        input=b"", capture_output=True, timeout=20,
    )  # fmt: skip
    assert out.returncode == 69 and out.stdout == b"" and b"cannot connect to" in out.stderr


def test_selftest_prints_json_with_a_missing_list(capsys):
    hal_box_entry.selftest()
    assert set(json.loads(capsys.readouterr().out)) == {"versions", "missing"}
