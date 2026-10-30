"""Tests for `halremote.keys`: parsing, fingerprints and ed25519 generation."""

import base64
import shutil
import stat
import struct
import subprocess
from pathlib import Path

import pytest

from halremote import keys

SSH_KEYGEN = shutil.which("ssh-keygen")


def _blob(key_type: str = "ssh-ed25519", payload: bytes = b"\x01" * 32) -> str:
    raw = struct.pack(">I", len(key_type)) + key_type.encode() + struct.pack(">I", len(payload)) + payload
    return base64.b64encode(raw).decode()


def _line(comment: str = "laptop") -> str:
    return f"ssh-ed25519 {_blob()} {comment}".rstrip()


def test_valid_key_is_parsed_with_its_comment():
    key = keys.parse_public_key(_line("my laptop"))
    assert (key.type, key.comment) == ("ssh-ed25519", "my laptop")
    assert key.line() == _line("my laptop")


def test_key_without_comment_is_accepted():
    assert keys.parse_public_key(_line("")).comment == ""


@pytest.mark.parametrize(
    "line",
    [
        "",
        "ssh-ed25519",
        "ssh-rsa AAAA laptop",
        "ssh-ed25519 !!!notbase64 laptop",
        f"ssh-ed25519 {_blob('ssh-rsa')} laptop",
        f"ssh-ed25519 {_blob()}\nssh-ed25519 {_blob()}",
        "ssh-ed25519 " + base64.b64encode(b"\x00\x00").decode() + " x",
        f"ssh-ed25519 {_blob()} bad\x07comment",
        f"ssh-ed25519 {_blob()} " + "c" * (keys.MAX_COMMENT_LEN + 1),
    ],
)
def test_malformed_or_unsupported_key_is_rejected(line):
    with pytest.raises(keys.KeyFormatError):
        keys.parse_public_key(line)


def test_generate_runs_keygen_with_ed25519_no_passphrase_and_refuses_existing(tmp_path):
    calls = []

    def runner(argv, *, timeout, check):
        calls.append((argv, timeout))
        Path(argv[-1]).write_text("private")
        Path(argv[-1] + ".pub").write_text(_line("tunnel") + "\n")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    path = tmp_path / "profile" / "tunnel_ed25519"
    key = keys.generate(path, comment="tunnel", ssh_keygen=Path("/usr/bin/ssh-keygen"), runner=runner)
    argv, timeout = calls[0]
    assert argv[argv.index("-t") + 1] == "ed25519"
    assert argv[argv.index("-N") + 1] == ""
    assert argv[argv.index("-f") + 1] == str(path)
    assert timeout == keys.KEYGEN_TIMEOUT_S
    assert key.comment == "tunnel"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        keys.generate(path, comment="tunnel", ssh_keygen=Path("/usr/bin/ssh-keygen"), runner=runner)
    assert len(calls) == 1


@pytest.mark.skipif(SSH_KEYGEN is None, reason="ssh-keygen is not installed")
def test_fingerprint_matches_ssh_keygen_for_a_generated_key(tmp_path):
    path = tmp_path / "k"
    key = keys.generate(path, comment="t", ssh_keygen=Path(SSH_KEYGEN))
    out = subprocess.run(
        [SSH_KEYGEN, "-lf", str(path) + ".pub"], capture_output=True, text=True, timeout=30, check=True
    ).stdout
    assert key.fingerprint() == out.split()[1]
    assert (tmp_path / "k").stat().st_mode & 0o077 == 0
