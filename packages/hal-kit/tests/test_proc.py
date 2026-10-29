"""Tests for `halkit.proc`."""

import os
import subprocess
import sys
import time

import pytest

from halkit import proc


def test_run_returns_stdout_and_stderr_as_bytes():
    code = "import sys; print('out'); print('err', file=sys.stderr)"
    result = proc.run([sys.executable, "-c", code], timeout=10)
    assert (result.stdout, result.stderr, result.returncode) == (b"out\n", b"err\n", 0)


def test_run_requires_a_timeout():
    with pytest.raises(TypeError, match="timeout"):
        proc.run([sys.executable, "-c", "pass"])  # type: ignore[call-arg]  # the point of the test


def test_run_rejects_a_string_argv_because_there_is_no_shell():
    with pytest.raises(TypeError, match="no shell"):
        proc.run("echo hi", timeout=5)  # type: ignore[arg-type]  # the point of the test


def test_run_rejects_an_empty_argv():
    with pytest.raises(ValueError, match="empty"):
        proc.run([], timeout=5)


def test_run_does_not_interpret_shell_syntax():
    result = proc.run(["echo", "a;b", "$HOME"], timeout=5)
    assert result.stdout == b"a;b $HOME\n"


def test_run_raises_on_a_failing_exit_status_by_default():
    with pytest.raises(subprocess.CalledProcessError) as err:
        proc.run([sys.executable, "-c", "raise SystemExit(3)"], timeout=10)
    assert err.value.returncode == 3


def test_run_with_check_false_returns_the_failing_status():
    result = proc.run([sys.executable, "-c", "raise SystemExit(3)"], timeout=10, check=False)
    assert result.returncode == 3


def test_run_passes_input_cwd_and_env(tmp_path):
    code = "import os,sys; sys.stdout.write(os.getcwd() + os.environ['X'] + sys.stdin.read())"
    result = proc.run([sys.executable, "-c", code], timeout=10, cwd=tmp_path, env={"X": "!"}, input=b"in")
    assert result.stdout.decode() == f"{tmp_path.resolve()}!in"


def test_run_without_input_gives_the_child_an_empty_stdin():
    result = proc.run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout=10)
    assert result.stdout == b"''\n"


def test_timeout_raises_proc_timeout_with_argv_and_elapsed():
    started = time.monotonic()
    with pytest.raises(proc.ProcTimeout) as err:
        proc.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)
    assert time.monotonic() - started < 10
    assert err.value.argv[0] == sys.executable
    assert 0.3 <= err.value.elapsed < 10


def test_timeout_kills_grandchildren_in_the_process_group(tmp_path):
    pid_file = tmp_path / "pid"
    script = f"sleep 60 & echo $! > {pid_file}; wait"
    with pytest.raises(proc.ProcTimeout):
        proc.run(["sh", "-c", script], timeout=1.0)
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("the grandchild survived the timeout")
