"""Tests for `scripts/install.sh` (REQ-CLI-89): static rules and a dry run in a temp HOME."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "install.sh"


def test_install_sh_has_no_curl_pipe_or_sudo():
    text = "\n".join(line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
    assert "sudo" not in text and "curl" not in text and "wget" not in text


def run(home, *args):
    env = {**os.environ, "HOME": str(home), "XDG_DATA_HOME": str(home / "data")}
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60, check=False)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is needed for the installer")
def test_dry_run_prints_the_steps_twice_identically_and_changes_nothing(tmp_path):
    first, second = run(tmp_path, "--dry-run"), run(tmp_path, "--dry-run")
    assert first.returncode == 0 and first.stdout == second.stdout
    assert "uv venv" in first.stdout and "ln -sfn" in first.stdout
    assert list(tmp_path.iterdir()) == []


def test_unknown_option_is_a_usage_error(tmp_path):
    done = run(tmp_path, "--bogus")
    assert done.returncode == 2 and "unknown option" in done.stderr
