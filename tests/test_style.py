"""Style gates for the whole repository.

Part 1 (REQ-SYS-20): `scripts/check-ascii.sh` passes on this checkout, and the script itself
reports what it should, tested on throwaway git repositories.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from halkit import proc

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-ascii.sh"
EM_DASH = chr(0x2014)  # built from the code point so this file stays ASCII
E_ACUTE = chr(0xE9)
GATE_TIMEOUT_S = 60  # the script runs git and grep over a few hundred files


def run_gate(root: Path) -> subprocess.CompletedProcess[bytes]:
    return proc.run(["bash", str(root / "scripts" / "check-ascii.sh")], timeout=GATE_TIMEOUT_S, check=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small git repository holding a copy of the gate script."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "check-ascii.sh")
    proc.run(["git", "init", "-q", str(tmp_path)], timeout=GATE_TIMEOUT_S)
    return tmp_path


def test_this_checkout_passes_the_ascii_gate():
    result = run_gate(REPO)
    assert result.returncode == 0, result.stderr.decode()


def test_ascii_text_passes(repo):
    (repo / "a.md").write_text("plain text\n")
    result = run_gate(repo)
    assert result.returncode == 0 and b"ok" in result.stdout


def test_non_ascii_text_fails_naming_file_and_line(repo):
    (repo / "a.md").write_text(f"fine\nbad {EM_DASH} here\n")
    result = run_gate(repo)
    assert result.returncode == 1
    assert b"check-ascii: a.md:2:" in result.stderr


def test_non_ascii_in_a_tracked_file_fails(repo):
    (repo / "a.py").write_text(f"x = '{E_ACUTE}'\n")
    proc.run(["git", "-C", str(repo), "add", "a.py"], timeout=GATE_TIMEOUT_S)
    assert run_gate(repo).returncode == 1


def test_non_ascii_file_name_fails(repo):
    (repo / f"caf{E_ACUTE}.txt").write_text("ascii content\n")
    result = run_gate(repo)
    assert result.returncode == 1 and b"non-ASCII file name" in result.stderr


def test_exempt_file_is_skipped_with_its_reason_comment(repo):
    (repo / "README.md").write_text(f"{EM_DASH} badge\n")
    (repo / "scripts" / "ascii-exempt.txt").write_text("# header\nREADME.md  # public README\n")
    assert run_gate(repo).returncode == 0


def test_exemption_covers_only_the_named_file(repo):
    (repo / "README.md").write_text(f"{EM_DASH}\n")
    (repo / "OTHER.md").write_text(f"{EM_DASH}\n")
    (repo / "scripts" / "ascii-exempt.txt").write_text("README.md  # public README\n")
    result = run_gate(repo)
    assert result.returncode == 1 and b"OTHER.md" in result.stderr and b"README.md:" not in result.stderr


def test_binary_files_are_skipped(repo):
    (repo / "pic.gif").write_bytes(b"GIF89a\x00\xff\xfe\x80\x81")
    assert run_gate(repo).returncode == 0


def test_ignored_files_are_not_checked(repo):
    (repo / ".gitignore").write_text("build/\n")
    (repo / "build").mkdir()
    (repo / "build" / "out.txt").write_text(f"{EM_DASH}\n")
    assert run_gate(repo).returncode == 0


def test_outside_a_git_checkout_the_gate_refuses_to_run(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "check-ascii.sh")
    assert run_gate(tmp_path).returncode == 2
