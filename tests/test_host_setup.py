"""Static and dry-run checks of scripts/host-setup.sh (REQ-CLI-92, REQ-RMT-19)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "host-setup.sh"
DRY_RUN_TIMEOUT_S = 30


def _dry_run(*flags: str) -> str:
    done = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", *flags],
        capture_output=True,
        text=True,
        timeout=DRY_RUN_TIMEOUT_S,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_host_setup_static_rules():
    text = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"\|\s*(sudo\s+)?(ba|z)?sh\b", code), "a download is piped into a shell"
    assert "--host-uds=open" in code
    assert "docker ps -q" in code, "Docker is restarted without checking for containers"
    assert "--max-time" in code, "downloads need a timeout"


def test_dry_run_changes_nothing_and_needs_no_root():
    out = _dry_run()
    assert "--host-uds=open" in out
    assert "restart Docker only when no container runs" in out


def test_ssh_dropin_has_the_hardening_lines_and_no_forwarding_limits_by_default():
    out = _dry_run("--ssh")
    for line in (
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "PermitRootLogin no",
        "AllowUsers ",
        "X11Forwarding no",
        "PermitUserEnvironment no",
    ):
        assert line in out
    assert "AllowTcpForwarding" not in out
    assert out.index("sshd -t") < out.index("systemctl reload ssh")


def test_forwarding_limits_are_added_only_on_request():
    out = _dry_run("--ssh", "--ssh-forwarding-limits")
    assert "AllowTcpForwarding local" in out
    assert "AllowStreamLocalForwarding no" in out


def test_forwarding_limits_without_ssh_is_refused():
    done = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "--ssh-forwarding-limits"],
        capture_output=True,
        text=True,
        timeout=DRY_RUN_TIMEOUT_S,
        check=False,
    )
    assert done.returncode == 2
