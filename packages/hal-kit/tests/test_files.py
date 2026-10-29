"""Tests for `halkit.files`."""

import json
import os
import stat
import threading

import pytest

from halkit import files

CAFE = "caf" + chr(0xE9)  # built from the code point so the file stays ASCII


def test_write_atomic_creates_the_file_with_the_default_mode(tmp_path):
    target = tmp_path / "a" / "f.txt"
    files.write_atomic(target, "hello")
    assert target.read_text() == "hello"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_atomic_applies_the_requested_mode(tmp_path):
    files.write_atomic(tmp_path / "f", b"x", mode=0o644)
    assert stat.S_IMODE((tmp_path / "f").stat().st_mode) == 0o644


def test_write_atomic_replaces_existing_content_and_mode(tmp_path):
    target = tmp_path / "f"
    target.write_text("old")
    target.chmod(0o666)
    files.write_atomic(target, "new")
    assert target.read_text() == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_atomic_leaves_old_content_and_no_temp_file_when_the_write_fails(tmp_path, monkeypatch):
    target = tmp_path / "f"
    target.write_text("old")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        files.write_atomic(target, "new")
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["f"]


def test_write_atomic_accepts_bytes_and_encodes_text_as_utf8(tmp_path):
    files.write_atomic(tmp_path / "b", b"\x00\x01")
    files.write_atomic(tmp_path / "t", CAFE)
    assert (tmp_path / "b").read_bytes() == b"\x00\x01"
    assert (tmp_path / "t").read_bytes() == CAFE.encode()


def test_append_jsonl_writes_one_line_per_record(tmp_path):
    log = tmp_path / "log.jsonl"
    files.append_jsonl(log, {"n": 1})
    files.append_jsonl(log, {"n": 2})
    assert [json.loads(line) for line in log.read_text().splitlines()] == [{"n": 1}, {"n": 2}]
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_append_jsonl_output_is_ascii_even_for_non_ascii_values(tmp_path):
    log = tmp_path / "log.jsonl"
    files.append_jsonl(log, {"name": CAFE})
    assert log.read_bytes().isascii()
    assert json.loads(log.read_text())["name"] == CAFE


def test_append_jsonl_rotates_when_the_file_reaches_max_bytes(tmp_path):
    log = tmp_path / "log.jsonl"
    for n in range(6):
        files.append_jsonl(log, {"n": n}, max_bytes=1, keep=3)
    assert json.loads(log.read_text())["n"] == 5
    assert json.loads((tmp_path / "log.jsonl.1").read_text())["n"] == 4
    assert (tmp_path / "log.jsonl.3").exists()
    assert not (tmp_path / "log.jsonl.4").exists()


def test_append_jsonl_keeps_only_the_newest_rotated_files(tmp_path):
    log = tmp_path / "log.jsonl"
    for n in range(20):
        files.append_jsonl(log, {"n": n}, max_bytes=1, keep=2)
    rotated = sorted(p.name for p in tmp_path.glob("log.jsonl.*") if not p.name.endswith(".lock"))
    assert rotated == ["log.jsonl.1", "log.jsonl.2"]


def test_append_jsonl_with_keep_zero_discards_the_full_log(tmp_path):
    log = tmp_path / "log.jsonl"
    files.append_jsonl(log, {"n": 1}, max_bytes=1, keep=0)
    files.append_jsonl(log, {"n": 2}, max_bytes=1, keep=0)
    assert json.loads(log.read_text())["n"] == 2
    assert not (tmp_path / "log.jsonl.1").exists()


def test_append_jsonl_from_many_threads_never_interleaves_lines(tmp_path):
    log = tmp_path / "log.jsonl"
    payload = "x" * 2000

    def work(worker: int) -> None:
        for n in range(20):
            files.append_jsonl(log, {"w": worker, "n": n, "pad": payload})

    threads = [threading.Thread(target=work, args=(w,)) for w in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == 120


def test_append_jsonl_times_out_when_another_process_holds_the_lock(tmp_path, monkeypatch):
    import fcntl

    log = tmp_path / "log.jsonl"
    holder = os.open(tmp_path / "log.jsonl.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(holder, fcntl.LOCK_EX)
    monkeypatch.setattr(files, "LOCK_TIMEOUT_S", 0.05)
    try:
        with pytest.raises(files.LockTimeout, match="still locked"):
            files.append_jsonl(log, {"n": 1})
    finally:
        os.close(holder)
    assert not log.exists()


def test_locked_blocks_appends_to_the_same_file_until_released(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setattr(files, "LOCK_TIMEOUT_S", 0.05)
    with files.locked(log):
        result: list[BaseException] = []
        writer = threading.Thread(target=lambda: _append_catching(log, result))
        writer.start()
        writer.join(timeout=5)
    assert isinstance(result[0], files.LockTimeout)
    files.append_jsonl(log, {"n": 2})
    assert json.loads(log.read_text()) == {"n": 2}


def _append_catching(log, result):
    try:
        files.append_jsonl(log, {"n": 1})
    except files.LockTimeout as exc:
        result.append(exc)
