"""Tests for the pid files `hal kill` relies on."""
import subprocess
import sys

import pytest

from halcore import pids


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    rt = tmp_path / "rt"
    rt.mkdir(mode=0o700)
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(rt))


def sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_registered_process_is_listed_and_its_file_is_removed_on_exit():
    with pids.registered("acp") as path:
        assert [r.name for r in pids.live()] == ["acp"] and path.exists()
    assert pids.live() == [] and not path.exists()


def test_bad_process_name_is_refused():
    with pytest.raises(ValueError, match="bad process name"), pids.registered("../x"):
        pass


def test_stale_file_with_a_reused_pid_is_dropped_without_signalling():
    (pids.pid_dir() / "acp-1234.pid").write_text("1234 1\n")
    assert pids.live() == []
    assert not (pids.pid_dir() / "acp-1234.pid").exists()


def test_terminate_stops_a_registered_process():
    child = sleeper()
    try:
        path = pids.pid_dir() / f"jobs-{child.pid}.pid"
        path.write_text(f"{child.pid} {pids.pidstart(child.pid)}\n")
        records = pids.live()
        assert [r.pid for r in records] == [child.pid]
        assert pids.terminate(records, grace=5) == []
        assert child.wait(timeout=5) != 0
    finally:
        child.kill()
        child.wait()


def test_terminate_kills_a_process_that_ignores_sigterm():
    child = subprocess.Popen(
        [sys.executable, "-c", "import signal,time,sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('r', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE,
    )
    try:
        child.stdout.readline()  # the handler is installed once it prints
        path = pids.pid_dir() / f"jobs-{child.pid}.pid"
        path.write_text(f"{child.pid} {pids.pidstart(child.pid)}\n")
        assert pids.terminate(pids.live(), grace=0.5) == []
    finally:
        child.kill()
        child.wait()
        child.stdout.close()
