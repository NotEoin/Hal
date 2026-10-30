"""Tests for `halgateway.llamacpp`: the sha256 lock on the llama.cpp release (REQ-SEC-78)."""

import hashlib

import pytest

from halgateway import llamacpp
from halgateway.llamacpp import LockError, verify_archive

BODY = b"synthetic archive"
NAME = "llama-b1-bin.tar.gz"


def lock(tmp_path, digest=None, tag="b1"):
    digest = digest or hashlib.sha256(BODY).hexdigest()
    path = tmp_path / "lock.toml"
    path.write_text(f'tag = "{tag}"\n[assets]\n"{NAME}" = "{digest}"\n')
    archive = tmp_path / NAME
    archive.write_bytes(BODY)
    return path, archive


def test_matching_archive_is_accepted(tmp_path):
    path, archive = lock(tmp_path)
    verify_archive(archive, "b1", path)


def test_sha256_mismatch_stops_the_install(tmp_path):
    path, archive = lock(tmp_path, digest="0" * 64)
    with pytest.raises(LockError, match="mismatch"):
        verify_archive(archive, "b1", path)


def test_archive_missing_from_the_lock_is_refused(tmp_path):
    path, archive = lock(tmp_path)
    other = tmp_path / "other.tar.gz"
    other.write_bytes(BODY)
    with pytest.raises(LockError, match="no sha256"):
        verify_archive(other, "b1", path)


def test_other_tag_is_refused(tmp_path):
    path, archive = lock(tmp_path)
    with pytest.raises(LockError, match="not the pinned"):
        verify_archive(archive, "b2", path)


def test_bad_digest_in_the_lock_is_an_error(tmp_path):
    path, _ = lock(tmp_path, digest="xyz")
    with pytest.raises(LockError, match="64 hex"):
        llamacpp.load_lock(path)


def test_repo_lock_pins_the_tag_setup_installs():
    tag, _ = llamacpp.load_lock()
    assert tag == "b11512"


def test_cli_exit_code_follows_the_check(tmp_path):
    _, archive = lock(tmp_path, digest="0" * 64)
    assert llamacpp.main(["x", "verify", "b11512", str(archive)]) == 1
