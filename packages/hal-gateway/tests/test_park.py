"""Tests for `halgateway.park`: parking a server's CUDA processes with `cuda-checkpoint`."""

import subprocess

import pytest

from halgateway import park

GIB_MIB = 1024


def parker(tmp_path, apps, *, available_mib=60 * GIB_MIB, fail=None):
    """A `Parker` over a fake binary; `fail` is an `(action, pid)` that exits 1."""
    binary = tmp_path / "cuda-checkpoint"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    calls = []

    def run(argv, timeout):
        action, pid = argv[2], int(argv[4])
        calls.append((action, pid))
        if (action, pid) == fail:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    p = park.Parker(
        str(binary),
        run=run,
        apps=lambda: dict(apps),
        tree=lambda root: {root, root + 1, root + 2},
        meminfo=lambda: (64 * GIB_MIB, available_mib),
    )
    return p, calls


def test_park_locks_every_cuda_process_then_checkpoints_and_resume_reverses_it(tmp_path):
    p, calls = parker(tmp_path, {12: 20000, 11: 500, 99: 3000})  # 99 is not under the server
    assert p.park(10) == (11, 12)
    assert calls == [("lock", 11), ("lock", 12), ("checkpoint", 11), ("checkpoint", 12)]
    calls.clear()
    p.resume((12, 11))
    assert calls == [("restore", 11), ("restore", 12), ("unlock", 11), ("unlock", 12)]


def test_a_failed_lock_unlocks_what_was_locked(tmp_path):
    p, calls = parker(tmp_path, {11: 500, 12: 20000}, fail=("lock", 12))
    with pytest.raises(park.ParkError, match="lock of 12"):
        p.park(10)
    assert calls == [("lock", 11), ("lock", 12), ("unlock", 11)]


def test_park_is_refused_when_the_vram_would_not_fit_in_ram(tmp_path):
    p, calls = parker(tmp_path, {11: 20000}, available_mib=22000)
    with pytest.raises(park.ParkError, match="RAM"):
        p.park(10)
    assert calls == []


def test_park_without_a_gpu_process_or_binary_is_an_error(tmp_path):
    p, _ = parker(tmp_path, {99: 100})
    with pytest.raises(park.ParkError, match="no GPU process"):
        p.park(10)
    with pytest.raises(park.ParkError, match="not installed"):
        park.Parker("").park(10)


def test_parking_falls_back_when_off_or_failing_and_forgets_on_stop(tmp_path):
    p, _ = parker(tmp_path, {11: 500})
    assert not park.Parking(False, p).try_park("qwen", 10)
    state = park.Parking(True, p)
    assert state.try_park("qwen", 10) and state.parked
    assert state.try_resume("qwen") and not state.parked
    broken, _ = parker(tmp_path, {11: 500}, fail=("restore", 11))
    state = park.Parking(True, broken)
    state.try_park("qwen", 10)
    assert not state.try_resume("qwen") and not state.parked  # the caller restarts the server


def test_descendants_follow_parent_pids_in_proc(tmp_path):
    for pid, ppid, name in (
        (10, 1, "vllm"),
        (11, 10, "VLLM::Engine Core"),
        (12, 11, "x) 7 y"),
        (13, 1, "other"),
    ):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "stat").write_text(f"{pid} ({name}) S {ppid} 0 0\n")
    (tmp_path / "self").mkdir()
    assert park.descendants(10, tmp_path) == {10, 11, 12}


def test_gpu_processes_sums_rows_per_pid():
    def run(argv, timeout):
        return subprocess.CompletedProcess(argv, 0, b"11, 20000\n11, 100\n12, 500\nbad row\n", b"")

    assert park.gpu_processes(run) == {11: 20100, 12: 500}
