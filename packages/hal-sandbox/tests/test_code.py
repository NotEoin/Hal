"""Tests for the `docker run` argv of `hal code`; the docker-marked ones run the real flags."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from halsandbox import detect, isolated
from halsandbox.backends.docker import LaunchSpec, docker_argv
from halsandbox.code import run_code
from halsandbox.env import box_env
from halsandbox.errors import SandboxError
from halsandbox.gitlayout import discover
from halsandbox.limits import Limits
from halsandbox.runtime import RuntimeDir

SID = "0123456789ab"
SOCK_M, SOCK_B = "/run/hal/model.sock", "/run/hal/broker.sock"


def docker_args(workdir, rd, *, image="hal-agent:test", runtime="runc", uid=1000, gid=1000, limits=None,
                masked=(), scratch=None, agent_args=()):  # fmt: skip
    """The argv `run_code` would build, with the image's own entrypoint so tests can override it."""
    spec = LaunchSpec(
        kind="code", sid=rd.sid, image=image, runtime=runtime, work=workdir, session_root=rd.root,
        uid=uid, gid=gid, limits=limits or Limits(2**31, 1.0, tmp_bytes=2**28), pid=1, pidstart=1,
        env=box_env("code", rd.sid, "docker", {}), started="2026-10-30T00:00:00Z", entrypoint=None,
        child=["-C", "/work", "--endpoint", SOCK_M, "--broker", SOCK_B, *agent_args],
        git_mounts=discover(workdir), masked=masked, scratch=scratch,
    )  # fmt: skip
    return docker_argv(spec)


def rdir(root: Path = Path("/run/x")) -> RuntimeDir:
    """A `RuntimeDir` that names paths only; nothing is created."""
    return RuntimeDir(SID, root)


def test_argv_carries_every_hardening_flag(tmp_path):
    (tmp_path / ".git").mkdir()
    a = docker_args(tmp_path, rdir())
    joined = " ".join(a)
    for flag in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges=true",
        "--user 1000:1000",
        "--runtime runc",
    ):
        assert flag in joined
    work = str(tmp_path.resolve())
    assert f'type=bind,"src={work}","dst=/work"' in a
    assert f'type=bind,"src={work}/.git","dst=/work/.git",readonly' in a
    assert 'type=bind,"src=/run/x/run","dst=/run/hal",readonly' in a


def test_git_mount_is_left_out_when_there_is_no_git_dir(tmp_path):
    assert not any("/work/.git" in x for x in docker_args(tmp_path, rdir()))


def test_agent_args_follow_the_socket_flags(tmp_path):
    a = docker_args(tmp_path, rdir(), agent_args=["-p", "hi"])
    assert a[-2:] == ["-p", "hi"] and "/run/hal/model.sock" in a and "/run/hal/broker.sock" in a


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


def laid_out(root: Path) -> RuntimeDir:
    """A `RuntimeDir` whose bind sources exist, as `RuntimeDir.create` leaves them (docker needs that)."""
    (root / "run").mkdir(parents=True, exist_ok=True)
    (root / "etc").mkdir(exist_ok=True)
    for name in ("etc/passwd", "etc/group", "empty"):
        (root / name).touch()
    return RuntimeDir(SID, root)


@pytest.mark.docker
def test_container_has_no_network_and_a_read_only_root(tmp_path, runtime):
    work = tmp_path / "w"
    work.mkdir()
    (work / ".git").mkdir()
    socks = tmp_path / "s"
    socks.mkdir()
    a = docker_args(work, laid_out(socks), image="python:3.12-slim", runtime=runtime)
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
    rd = laid_out(sockdir)  # the box sees run/ as /run/hal, so the socket goes there
    fw = ModelForwarder(rd.run / "model.sock", upstream.url)
    fw.start()
    try:
        a = docker_args(work, rd, image="python:3.12-slim", runtime=runtime)
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

    a = docker_args(tmp_path, rdir(), limits=Limits(memory_bytes=2**31, cpus=2.0, pids=100, tmp_bytes=2**28))
    joined = " ".join(a)
    for flag in ("--memory 2147483648", "--memory-swap 2147483648", "--cpus 2", "--pids-limit 100"):
        assert flag in joined
    assert "size=268435456" in joined


def test_scratch_is_mounted_rw_at_scratch(tmp_path):
    a = docker_args(tmp_path, rdir(), scratch=Path("/st/scratch/p"))
    assert 'type=bind,"src=/st/scratch/p","dst=/scratch"' in a
    assert not any("dst=/scratch" in x for x in docker_args(tmp_path, rdir()))


def test_direct_mode_is_refused_under_the_default_lockdown(tmp_path, monkeypatch):
    from halsandbox.code import run_code
    from halsandbox.errors import LockdownError

    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "none.toml"))
    with pytest.raises(LockdownError, match="relaxed"):
        run_code(tmp_path, direct=True)


def test_masked_files_are_bound_read_only_from_an_empty_file(tmp_path):
    a = docker_args(tmp_path, rdir(), masked=[".env", "d/k.pem"])
    assert 'type=bind,"src=/run/x/empty","dst=/work/.env",readonly' in a
    assert 'type=bind,"src=/run/x/empty","dst=/work/d/k.pem",readonly' in a


def test_run_code_boxes_an_isolated_copy_and_cleans_up(tmp_path, monkeypatch):
    import os

    os.chmod(sockdir := Path(__import__("tempfile").mkdtemp(prefix="rc-")), 0o700)
    log = tmp_path / "docker.log"
    fake = tmp_path / "docker"
    fake.write_text(
        f'#!/bin/sh\necho "$@" >> {log}\n[ "$1 $2" = "image inspect" ] && echo "sha256:abc ctx"\n'
        '[ "$1" = run ] && exit 3\nexit 0\n'
    )
    fake.chmod(0o755)
    for k, v in (("HAL_DOCKER", fake), ("HAL_RUNTIME_DIR", sockdir), ("HAL_STATE_DIR", tmp_path / "st"),
                 ("HAL_CONFIG", tmp_path / "none.toml")):  # fmt: skip
        monkeypatch.setenv(k, str(v))
    monkeypatch.setattr(
        detect,
        "detect",
        lambda *a, **k: detect.Probe("docker", True, "runc", "rootful", "", "2026-10-30T00:00:00Z"),
    )
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.py").write_text("x\n")
    try:
        assert run_code(project, upstream="http://127.0.0.1:1") == 3
        run_line = [ln for ln in log.read_text().splitlines() if ln.startswith("run ")][0]
        assert str(project) not in run_line.replace(str(tmp_path / "st"), "")
        assert len(isolated.sessions()) == 1
        assert not list((sockdir / "s").glob("*"))
    finally:
        import shutil

        shutil.rmtree(sockdir, ignore_errors=True)


def test_masked_path_that_leaves_the_work_tree_is_refused(tmp_path):
    with pytest.raises(SandboxError):
        docker_args(tmp_path, rdir(), masked=["../x"])
