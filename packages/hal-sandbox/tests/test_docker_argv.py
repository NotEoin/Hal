"""Golden and property tests for `halsandbox.backends.docker.docker_argv`."""

import re
from pathlib import Path

import pytest

from halsandbox.backends.docker import LaunchSpec, docker_argv
from halsandbox.errors import SandboxError
from halsandbox.gitlayout import GitMount
from halsandbox.limits import Limits

GOLDEN = Path(__file__).parent / "golden"
LIMITS = Limits(memory_bytes=4 * 2**30, cpus=2.0, pids=512, tmp_bytes=2**30, home_bytes=2**29)


def make(**over) -> LaunchSpec:
    base = dict(
        kind="code",
        sid="0123456789ab",
        image="hal-agent:test",
        runtime="runsc",
        work=Path("/w/proj"),
        session_root=Path("/r/hal/s/0123456789ab"),
        uid=1000,
        gid=1000,
        limits=LIMITS,
        env={"HOME": "/home/hal", "HAL_SANDBOX": "docker"},
        child=["hal-agent", "--acp"],
        pid=4242,
        pidstart=99,
        started="2026-10-30T10:00:00Z",
        git_mounts=(GitMount(Path("/w/proj/.git"), "/work/.git"),),
        masked=(".env",),
        scratch=Path("/st/scratch/k"),
    )
    base.update(over)
    return LaunchSpec(**base)


def test_code_argv_matches_the_golden_file():
    got = "\n".join(docker_argv(make())) + "\n"
    assert got == (GOLDEN / "docker_code.txt").read_text()


def test_tty_and_relay_and_userns_flags_appear_only_when_asked():
    plain = docker_argv(make())
    assert "-t" not in plain and "--userns" not in plain and "--relay-port" not in plain
    rich = docker_argv(make(tty=True, userns_host=True, relay_port=8741))
    assert "-t" in rich and "--userns" in rich
    assert rich[rich.index("--relay-port") : rich.index("--relay-port") + 3] == ["--relay-port", "8741", "--"]


def test_mount_list_is_exact():
    mounts = [a for a in docker_argv(make()) if a.startswith("type=bind")]
    dsts = [re.search(r'"dst=([^"]*)"', m).group(1) for m in mounts]
    assert dsts == ["/work", "/work/.git", "/run/hal", "/etc/passwd", "/etc/group", "/work/.env", "/scratch"]
    assert all(
        m.endswith(",readonly") for m in mounts if not m.endswith('"dst=/work"') and "/scratch" not in m
    )


def test_path_with_comma_colon_and_quote_is_one_csv_field():
    mount = [a for a in docker_argv(make(work=Path('/w/a,b:c"d'))) if '"dst=/work"' in a][0]
    assert mount == 'type=bind,"src=/w/a,b:c""d","dst=/work"'


def test_argv_never_asks_for_privilege_or_the_host():
    joined = " ".join(docker_argv(make(tty=True, userns_host=True)))
    for bad in (
        "--privileged",
        "--device",
        "--cap-add",
        "--network host",
        "--pid host",
        "--ipc host",
        " -v ",
    ):
        assert bad not in joined


def test_session_env_is_passed_as_env_flags_in_order():
    argv = docker_argv(make())
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert pairs == ["HOME=/home/hal", "HAL_SANDBOX=docker"]


@pytest.mark.parametrize(
    "over",
    [
        {"runtime": "kata"},
        {"kind": "weird"},
        {"image": "--privileged"},
        {"masked": ("../x",)},
        {"masked": ("/etc/passwd",)},
        {"env": {"A=B": "c"}},
    ],
)
def test_unsafe_spec_is_refused(over):
    with pytest.raises(SandboxError):
        docker_argv(make(**over))
