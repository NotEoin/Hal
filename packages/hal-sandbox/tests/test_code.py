"""Tests for the `docker run` argv of `hal code`; the docker-marked ones run the real flags."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from halsandbox.code import docker_args
from halsandbox.errors import SandboxError


def test_argv_carries_every_hardening_flag(tmp_path):
    (tmp_path / ".git").mkdir()
    a = docker_args(tmp_path, Path("/run/x"), uid=1000, gid=1000)
    joined = " ".join(a)
    for flag in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--user 1000:1000",
        "--runtime runc",
    ):
        assert flag in joined
    assert f"{tmp_path}:/work:rw" in a and f"{tmp_path}/.git:/work/.git:ro" in a
    assert "/run/x:/run/hal:ro" in a


def test_git_mount_is_left_out_when_there_is_no_git_dir(tmp_path):
    assert not any(":/work/.git" in x for x in docker_args(tmp_path, Path("/run/x")))


def test_agent_args_follow_the_socket_flags(tmp_path):
    a = docker_args(tmp_path, Path("/run/x"), agent_args=["-p", "hi"])
    assert a[-2:] == ["-p", "hi"] and "/run/hal/model.sock" in a and "/run/hal/hub.sock" in a


def _docker_ok() -> bool:
    return shutil.which("docker") is not None


@pytest.fixture(params=["runc", "runsc"])
def runtime(request):
    out = subprocess.run(
        ["docker", "info", "--format", "{{json .Runtimes}}"], capture_output=True, text=True, timeout=30
    ).stdout
    if request.param not in json.loads(out):
        pytest.skip(f"{request.param} not registered")
    return request.param


@pytest.mark.docker
def test_container_has_no_network_and_a_read_only_root(tmp_path, runtime):
    work = tmp_path / "w"
    work.mkdir()
    (work / ".git").mkdir()
    socks = tmp_path / "s"
    socks.mkdir()
    a = docker_args(work, socks, image="python:3.12-slim", runtime=runtime)
    cut = a.index("python:3.12-slim")
    script = (
        "import socket,os,sys\n"
        "try:\n socket.create_connection(('1.1.1.1',53),timeout=3); print('NET')\n"
        "except OSError: print('nonet')\n"
        "for p in ('/etc/x','/work/.git/x'):\n"
        " try: open(p,'w'); print('WROTE',p)\n"
        " except OSError: print('ro',p)\n"
        "open('/work/ok','w').close(); open('/tmp/ok','w').close(); print('rw ok')\n"
        "print(sorted(l for l in open('/proc/self/status') if l.startswith(('CapEff','NoNewPrivs'))))\n"
        "print(os.getuid())\n"
    )
    out = subprocess.run(
        [*a[:cut], "--entrypoint", "python", "python:3.12-slim", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "nonet" in out.stdout and "NET\n" not in out.stdout.replace("nonet", "")
    assert "WROTE" not in out.stdout and "rw ok" in out.stdout
    assert "CapEff:\\t0000000000000000" in out.stdout and "NoNewPrivs:\\t1" in out.stdout
    assert (work / "ok").exists()


@pytest.mark.docker
def test_container_reaches_the_model_forwarder_socket(tmp_path, runtime, sockdir, upstream):
    from halsandbox.forwarder import ModelForwarder

    work = tmp_path / "w"
    work.mkdir()
    fw = ModelForwarder(sockdir / "model.sock", upstream.url)
    fw.start()
    try:
        a = docker_args(work, sockdir, image="python:3.12-slim", runtime=runtime)
        cut = a.index("python:3.12-slim")
        script = (
            "import socket\ns=socket.socket(socket.AF_UNIX);s.connect('/run/hal/model.sock')\n"
            "s.sendall(b'GET /v1/models HTTP/1.0\\r\\n\\r\\n');d=b''\n"
            "while True:\n c=s.recv(4096)\n if not c: break\n d+=c\nprint(d.decode())\n"
        )
        out = subprocess.run(
            [*a[:cut], "--entrypoint", "python", "python:3.12-slim", "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        fw.stop()
    assert out.returncode == 0, out.stderr
    assert '"ok": true' in out.stdout


def test_argv_limits_memory_cpus_and_pids(tmp_path):
    from halsandbox.limits import Limits

    a = docker_args(
        tmp_path, Path("/run/x"), limits=Limits(memory_bytes=2**31, cpus=2.0, pids=100, tmp_bytes=2**28)
    )
    joined = " ".join(a)
    for flag in ("--memory 2147483648", "--memory-swap 2147483648", "--cpus 2", "--pids-limit 100"):
        assert flag in joined
    assert "size=268435456" in joined


def test_scratch_is_mounted_rw_at_scratch(tmp_path):
    a = docker_args(tmp_path, Path("/run/x"), scratch=Path("/st/scratch/p"))
    assert "/st/scratch/p:/scratch:rw" in a
    assert not any(":/scratch" in x for x in docker_args(tmp_path, Path("/run/x")))


def test_direct_mode_is_refused_under_the_default_lockdown(tmp_path, monkeypatch):
    from halsandbox.code import run_code
    from halsandbox.errors import LockdownError

    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "none.toml"))
    with pytest.raises(LockdownError, match="relaxed"):
        run_code(tmp_path, direct=True)


def test_masked_files_are_bound_read_only_from_an_empty_file(tmp_path):
    a = docker_args(tmp_path, Path("/run/x"), masked=[".env", "d/k.pem"], empty_file=Path("/run/x/empty"))
    assert "/run/x/empty:/work/.env:ro" in a and "/run/x/empty:/work/d/k.pem:ro" in a


def test_masked_paths_without_an_empty_file_are_refused(tmp_path):
    with pytest.raises(SandboxError):
        docker_args(tmp_path, Path("/run/x"), masked=[".env"])
