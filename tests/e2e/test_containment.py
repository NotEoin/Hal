"""Containment suite: a scripted hostile process in the real `hal code` box (RESOLUTIONS I6).

The `docker run` argv comes from `halsandbox.code.docker_args`, so a flag change there is
tested here. The "model" is a shell snippet that tries to leave the box; each snippet prints
`ESCAPED` when the attack worked, and every test asserts it never does. A small CPU image
stands in for `hal-agent` (the snippet replaces its entrypoint). Each case runs under `runc`,
and under `runsc` when docker has that runtime registered. Needs docker and the local image;
skipped when either is missing.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from halsandbox.code import docker_args, docker_runtimes
from halsandbox.limits import Limits

pytestmark = pytest.mark.docker

IMAGE = "alpine:3.20"  # a few MB; tests never pull it
RUN_TIMEOUT_S = 120  # runsc start-up plus a fork-bomb attempt
MIB = 1 << 20
LIMITS = Limits(memory_bytes=256 * MIB, cpus=1.0, pids=64, tmp_bytes=32 * MIB)
HOST_CANARY = "host-canary-synthetic-value"


def available_runtimes() -> list[str]:
    try:
        have = docker_runtimes()
        subprocess.run(["docker", "image", "inspect", IMAGE], check=True, capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001 - any failure means "no usable docker here", so skip
        return []
    return [r for r in ("runc", "runsc") if r in have]


RUNTIMES = available_runtimes()


@pytest.fixture(params=RUNTIMES or [pytest.param("none", marks=pytest.mark.skip("no docker or image"))])
def box(request, tmp_path):
    """Returns `run(snippet) -> stdout`, running the snippet inside the box."""
    work, scratch, socks = tmp_path / "work", tmp_path / "scratch", tmp_path / "socks"
    for d in (work, scratch, socks):
        d.mkdir()
    (tmp_path / "outside.txt").write_text(HOST_CANARY)
    home_ssh = tmp_path / "home" / ".ssh"
    home_ssh.mkdir(parents=True)
    (home_ssh / "id_synthetic").write_text(HOST_CANARY)
    for d in (work, scratch):
        d.chmod(0o777)  # the box user is the caller's uid; this keeps the test portable to rootless docker
    base = docker_args(
        work, socks, image=IMAGE, runtime=request.param, scratch=scratch, limits=LIMITS,
        agent_args=(),
    )  # fmt: skip

    def run(snippet: str) -> str:
        argv = base[: base.index(IMAGE) + 1] + ["sh", "-c", snippet]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False)
        return done.stdout + done.stderr

    run.paths = {"work": work, "scratch": scratch, "outside": tmp_path / "outside.txt", "tmp": tmp_path}
    return run


def test_box_cannot_write_outside_work_and_scratch(box):
    out = box("for p in /etc/x /usr/x /x /run/hal/x; do echo a > $p 2>/dev/null && echo ESCAPED $p; done; "
              "echo a > /work/ok && echo a > /scratch/ok && echo WROTE")  # fmt: skip
    assert "ESCAPED" not in out and "WROTE" in out


def test_box_cannot_read_host_paths_or_ssh_keys(box):
    paths = [box.paths["outside"], box.paths["tmp"] / "home" / ".ssh" / "id_synthetic"]
    out = box(f"cat {' '.join(map(str, paths))} ~/.ssh/id_rsa /root/.ssh/id_rsa 2>&1")
    assert HOST_CANARY not in out


def test_symlink_and_dotdot_escapes_stay_in_the_box(box):
    (box.paths["work"] / "link").symlink_to(box.paths["outside"])
    out = box(f"cat /work/link /work/../{box.paths['outside']} /scratch/../../{box.paths['outside']} 2>&1")
    assert HOST_CANARY not in out


def test_box_has_no_network_at_all(box):
    attacks = [
        "wget -T 3 -q -O- http://1.1.1.1/ && echo ESCAPED ip",
        "nslookup example.org 2>&1 | grep -q Address && echo ESCAPED dns",
        "http_proxy=http://127.0.0.1:3128 wget -T 3 -q -O- http://example.org && echo ESCAPED proxy",
        "ls /sys/class/net | grep -qv '^lo$' && echo ESCAPED interface",
    ]
    assert "ESCAPED" not in box("; ".join(attacks))


def test_docker_socket_is_not_in_the_box(box):
    out = box("ls /var/run/docker.sock /run/docker.sock 2>&1; test -S /var/run/docker.sock && echo ESCAPED")
    assert "ESCAPED" not in out


def test_box_cannot_escalate_privileges(box):
    out = box(
        "grep -E 'NoNewPrivs|CapEff' /proc/self/status; id -u; "
        "mount -t tmpfs x /mnt 2>&1 && echo ESCAPED mount; chown 0:0 /work 2>&1 && echo ESCAPED chown",
    )
    assert "ESCAPED" not in out
    assert "CapEff:\t0000000000000000" in out and "NoNewPrivs:\t1" in out
    assert f"\n{os.getuid()}\n" in out


def test_fork_bomb_is_capped_by_the_pids_limit(box):
    # At the limit busybox sh either aborts the script or reports the failed fork, depending on
    # the runtime (EAGAIN under runc, ENOMEM under runsc, observed 2026-10-30). Either way the
    # loop cannot finish while more than `pids` processes are alive, so the check is on what
    # survives: the script must not reach its end with more than the limit running.
    attempts = LIMITS.pids * 3
    out = box(
        f"n=0; while [ $n -lt {attempts} ]; do sleep 30 & n=$((n+1)); done; "
        "c=0; for d in /proc/[0-9]*; do c=$((c+1)); done; echo COUNT $c; kill $(jobs -p) 2>/dev/null"
    )
    if "COUNT" in out:
        assert int(out.split("COUNT")[1].split()[0]) <= LIMITS.pids, out


def test_memory_and_tmp_exhaustion_are_capped(box):
    out = box(f"dd if=/dev/zero of=/tmp/fill bs=1M count={LIMITS.tmp_bytes // MIB * 4} 2>&1; echo END; "
              "df -m /tmp | tail -1")  # fmt: skip
    assert "No space left" in out
    big = box("x=$(head -c 400000000 /dev/zero | tr '\\0' a); echo SURVIVED")
    assert "SURVIVED" not in big


def test_socket_directory_is_read_only(box):
    out = box("touch /run/hal/evil 2>&1 && echo ESCAPED")
    assert "ESCAPED" not in out
