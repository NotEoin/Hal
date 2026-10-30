"""Tests for secret masking discovery."""

import os

import pytest

from halhub.policy.profiles import HARD_DENY_GLOBS
from halsandbox.mask import MASK_DEFAULT, MaskError, find_masked, resolve_patterns


def _touch(root, rel, text="x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_matches_and_mount_list(tmp_path):
    _touch(tmp_path, ".env")
    _touch(tmp_path, "deploy/server.pem")
    _touch(tmp_path, "src/app.py")
    assert find_masked(tmp_path, MASK_DEFAULT) == (".env", "deploy/server.pem")


def test_secret_under_ignored_directory_is_masked(tmp_path):
    _touch(tmp_path, ".gitignore", "secrets/\n")
    _touch(tmp_path, "secrets/prod.key")
    assert find_masked(tmp_path, MASK_DEFAULT) == ("secrets/prod.key",)


def test_the_five_skip_directories_are_not_walked(tmp_path):
    for d in (".git", ".hal", "node_modules", ".venv", "venv"):
        _touch(tmp_path, f"{d}/.env")
    assert find_masked(tmp_path, MASK_DEFAULT) == ()


def test_default_extends_the_hub_hard_deny_list():
    assert set(HARD_DENY_GLOBS) <= set(MASK_DEFAULT) and ".npmrc" in MASK_DEFAULT
    assert resolve_patterns("default") is MASK_DEFAULT and resolve_patterns([]) == ()


def test_symlink_inside_is_masked_at_target_and_outside_is_skipped(tmp_path):
    work = tmp_path / "work"
    _touch(work, "real/data.txt")
    _touch(tmp_path, "outside.pem")
    os.symlink(work / "real" / "data.txt", work / "token.pem")
    os.symlink(tmp_path / "outside.pem", work / "out.pem")
    assert find_masked(work, ["*.pem"]) == ("real/data.txt",)


def test_too_many_matches_fail_loudly(tmp_path):
    for i in range(5):
        _touch(tmp_path, f"k{i}.key")
    with pytest.raises(MaskError, match="--unmask"):
        find_masked(tmp_path, ["*.key"], max_files=3)


def test_a_slow_walk_fails_loudly(tmp_path):
    _touch(tmp_path, "a/b")
    with pytest.raises(MaskError, match="took over"):
        find_masked(tmp_path, ["*.key"], timeout_s=-1)


def test_empty_pattern_list_turns_masking_off(tmp_path):
    _touch(tmp_path, ".env")
    assert find_masked(tmp_path, []) == ()
