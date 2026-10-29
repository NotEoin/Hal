"""Tests for `halkit.secrets`."""

import os
import stat

import pytest

from halkit import secrets as hal_secrets


def make(path, text, mode=0o600):
    path.write_text(text)
    path.chmod(mode)
    return path


def test_read_secret_strips_one_trailing_newline(tmp_path):
    assert hal_secrets.read_secret(make(tmp_path / "s", "abc\n")) == "abc"


def test_read_secret_strips_only_one_newline(tmp_path):
    assert hal_secrets.read_secret(make(tmp_path / "s", "abc\n\n")) == "abc\n"


def test_read_secret_accepts_a_file_without_a_newline(tmp_path):
    assert hal_secrets.read_secret(make(tmp_path / "s", "abc")) == "abc"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o664, 0o644])
def test_read_secret_refuses_a_file_readable_by_others(tmp_path, mode):
    with pytest.raises(hal_secrets.UnsafeSecretError, match="chmod 600"):
        hal_secrets.read_secret(make(tmp_path / "s", "abc", mode))


def test_read_secret_refuses_a_file_owned_by_another_user(tmp_path, monkeypatch):
    path = make(tmp_path / "s", "abc")
    monkeypatch.setattr(os, "getuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(hal_secrets.UnsafeSecretError, match="owned by uid"):
        hal_secrets.read_secret(path)


def test_read_secret_raises_file_not_found_for_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        hal_secrets.read_secret(tmp_path / "none")


def test_read_secret_rejects_an_empty_file(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        hal_secrets.read_secret(make(tmp_path / "s", "\n"))


def test_read_secret_never_puts_the_value_in_an_error(tmp_path):
    with pytest.raises(hal_secrets.UnsafeSecretError) as err:
        hal_secrets.read_secret(make(tmp_path / "s", "topsecretvalue", 0o644))
    assert "topsecretvalue" not in str(err.value)


def test_generate_token_creates_a_private_file_and_returns_the_token(tmp_path):
    path = tmp_path / "d" / "token"
    token = hal_secrets.generate_token(path)
    assert path.read_text() == token + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(token) >= 43  # 32 bytes in url-safe base64


def test_generate_token_returns_the_existing_token_unchanged(tmp_path):
    path = make(tmp_path / "token", "keepme\n")
    assert hal_secrets.generate_token(path) == "keepme"
    assert path.read_text() == "keepme\n"


def test_generate_token_refuses_an_existing_unsafe_file(tmp_path):
    path = make(tmp_path / "token", "x\n", 0o644)
    with pytest.raises(hal_secrets.UnsafeSecretError):
        hal_secrets.generate_token(path)


def test_generate_token_gives_each_new_file_a_different_token(tmp_path):
    assert hal_secrets.generate_token(tmp_path / "a") != hal_secrets.generate_token(tmp_path / "b")


def test_generate_token_rejects_too_little_randomness(tmp_path):
    with pytest.raises(ValueError, match="16 bytes"):
        hal_secrets.generate_token(tmp_path / "t", nbytes=8)


def test_generate_token_leaves_no_temp_files_behind(tmp_path):
    hal_secrets.generate_token(tmp_path / "token")
    assert [p.name for p in tmp_path.iterdir()] == ["token"]
