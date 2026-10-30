"""Tests for `halsandbox.backends.bwrap`: golden argv, scope wrapper, binds; a bwrap-marked live run."""

import shutil
import subprocess
from pathlib import Path

import pytest

from halsandbox.backends.bwrap import bwrap_launch, user_manager_running
from halsandbox.backends.docker import LaunchSpec
from halsandbox.gitlayout import GitMount
from halsandbox.limits import Limits

GOLDEN = Path(__file__).parent / "golden"
LIMITS = Limits(memory_bytes=4 * 2**30, cpus=2.0, pids=512, tmp_bytes=2**30, home_bytes=2**29)
KW = dict(prefixes=["/usr", "/opt/py", "/opt/venv"], venv_bin="/opt/venv/bin")


def make(**over) -> LaunchSpec:
    base = dict(
        kind="code", sid="0123456789ab", image="x", runtime="runc", work=Path("/w/proj"),
        session_root=Path("/r/hal/s/0123456789ab"), uid=1000, gid=1000, limits=LIMITS,
        env={"HOME": "/home/hal", "HAL_SANDBOX": "bwrap"}, child=["hal-agent", "--acp"],
        pid=1, pidstart=1, started="2026-10-30T10:00:00Z",
        git_mounts=(GitMount(Path("/w/proj/.git"), "/work/.git"),),
        masked=(".env",), scratch=Path("/st/scratch/k"),
    )  # fmt: skip
    base.update(over)
    return LaunchSpec(**base)


def test_argv_matches_the_golden_file():
    got = "\n".join(bwrap_launch(make(), **KW).argv) + "\n"
    assert got == (GOLDEN / "bwrap_code.txt").read_text()


def test_scope_wrapper_when_user_manager_present():
    argv = list(bwrap_launch(make(scope=True), **KW).argv)
    assert argv[:5] == ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
    assert "MemoryMax=4294967296" in argv and "TasksMax=512" in argv and "CPUQuota=200%" in argv
    assert argv[argv.index("--") + 1] == "bwrap"


def test_no_scope_means_no_systemd_run():
    assert "systemd-run" not in bwrap_launch(make(), **KW).argv


def test_split_usr_binds_each_directory_instead_of_symlinks():
    argv = list(bwrap_launch(make(), merged_usr=False, **KW).argv)
    assert "--symlink" not in argv and argv.count("--ro-bind-try") >= 4


def test_mask_and_git_binds_are_read_only_and_work_is_writable():
    argv = list(bwrap_launch(make(), **KW).argv)
    assert ["--bind", "/w/proj", "/work"] == argv[argv.index("--bind") : argv.index("--bind") + 3]
    assert ["--ro-bind", "/r/hal/s/0123456789ab/empty", "/work/.env"] == [
        argv[i] for i in range(argv.index("/work/.env") - 2, argv.index("/work/.env") + 1)
    ]
    assert ["--ro-bind", "/w/proj/.git", "/work/.git"] == argv[
        argv.index("/w/proj/.git") - 1 : argv.index("/w/proj/.git") + 2
    ]


def test_python_prefixes_under_usr_are_not_bound_twice():
    argv = list(bwrap_launch(make(), **KW).argv)
    assert argv.count("/usr") == 2 and "/opt/py" in argv and "/opt/venv" in argv


def test_environment_is_cleared_then_set_with_the_venv_first_on_path():
    argv = list(bwrap_launch(make(), **KW).argv)
    assert "--clearenv" in argv
    i = argv.index("PATH")
    assert argv[i - 1] == "--setenv" and argv[i + 1].startswith("/opt/venv/bin:")


def test_no_user_manager_when_systemctl_is_missing():
    assert user_manager_running("/nonexistent/systemctl") is False


@pytest.mark.bwrap
def test_box_has_no_network_and_a_read_only_usr(tmp_path):
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bwrap not installed")
    work = tmp_path / "w"
    work.mkdir()
    root = tmp_path / "s"
    for d in ("etc", "run"):
        (root / d).mkdir(parents=True)
    (root / "etc" / "passwd").write_text("hal:x:1000:1000::/home/hal:/bin/sh\n")
    (root / "etc" / "group").write_text("hal:x:1000:\n")
    argv = list(
        bwrap_launch(
            make(work=work, session_root=root, git_mounts=(), masked=(), scratch=None, bwrap=bwrap),
            prefixes=["/usr"],
            venv_bin="/usr/bin",
        ).argv
    )
    cut = argv.index("--chdir")
    code = (
        "import socket,sys\n"
        "try: socket.create_connection(('1.1.1.1',53),timeout=2); print('NET')\n"
        "except OSError: print('nonet')\n"
        "try: open('/usr/x','w'); print('WROTE')\n"
        "except OSError: print('ro')\n"
        "open('/work/ok','w').close(); print('rw ok')\n"
    )
    probe = subprocess.run([*argv[:cut], "--chdir", "/work", "--", "/usr/bin/python3", "-c", code],
                           capture_output=True, text=True, timeout=60)  # fmt: skip
    if probe.returncode != 0 and "rw ok" not in probe.stdout:
        pytest.skip(f"bwrap cannot run here: {probe.stderr.strip()[:200]}")
    assert "nonet" in probe.stdout and "ro\n" in probe.stdout and (work / "ok").exists()


def test_a_session_without_a_work_tree_binds_only_scratch_and_runs_there():
    argv = list(
        bwrap_launch(make(kind="chat", work=None, git_mounts=(), masked=(), scratch=Path("/st/s")), **KW).argv
    )
    assert "/work" not in argv
    assert argv[argv.index("--chdir") + 1] == "/scratch"
    assert argv[argv.index("/st/s") - 1] == "--bind"
