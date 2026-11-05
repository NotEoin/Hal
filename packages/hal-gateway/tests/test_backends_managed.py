"""Tests for the `managed-openai` backend (REQ-GW-90..96) against a fake OpenAI-compatible server script."""

import http.client
import json
import os
import signal
import socket
import sys
import time

import pytest

from halgateway import cli, launchers
from halgateway.backends.managed import ManagedServer
from halgateway.config import load_config
from halgateway.errors import Unavailable
from halgateway.server import Gateway

TOKEN = "synthetic-owner-token"
# The fake reads its script from `ctl` once at start: `exit` quits at once, `never_ready` keeps
# `/health` at 503, `ready_after` answers 503 that many times first, `child` forks a sleeper in
# its process group (the stand-in for vLLM's EngineCore), `ignore_term` ignores SIGTERM in both.
FAKE_SERVER = """\
#!{python}
import http.server, json, os, signal, subprocess, sys
ctl_path = "{ctl}"
ctl = json.load(open(ctl_path))
run = {{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd()}}
json.dump(run, open(ctl_path + ".run", "w"))
if ctl.get("exit"):
    sys.exit(3)
if ctl.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if ctl.get("child"):
    code = "import signal, time\\n"
    if ctl.get("ignore_term"):
        code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
    child = subprocess.Popen([sys.executable, "-c", code + "time.sleep(120)"])
    open(ctl_path + ".child", "w").write(str(child.pid))
key = os.environ.get("FAKE_KEY", "")
probes = [0]

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, doc, status=200):
        raw = json.dumps(doc).encode()
        self.send_response(status); self.send_header("Content-Length", str(len(raw))); self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        if self.path == "/health":
            probes[0] += 1
            ready = not ctl.get("never_ready") and probes[0] > ctl.get("ready_after", 0)
            return self._send({{}}, 200 if ready else 503)
        if self.headers.get("Authorization") != "Bearer " + key:
            return self._send({{}}, 401)
        if self.path == "/v1/models":
            return self._send({{"data": [{{"id": m}} for m in ctl.get("models", ["m1"])]}})
        self._send({{}}, 404)
    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + key:
            return self._send({{}}, 401)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._send({{"object": "chat.completion", "echo": body, "choices": []}})

http.server.ThreadingHTTPServer((os.environ["HOST"], int(os.environ["PORT"])), H).serve_forever()
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeOllama:
    name = "ollama"
    type = "ollama"
    gpu = True
    remote = False
    expose_models = False

    def __init__(self):
        self.held = ["gemma:1"]

    def in_vram(self):
        return list(self.held)

    def unload(self, model):
        self.held.remove(model)


class FakeGpuBackend:
    """Stands in for a running llama router: it records a yield instead of stopping a process."""

    name = "local"
    type = "llama-router"
    gpu = True

    def __init__(self, busy=0):
        self.busy, self.yielded = busy, False

    def running(self):
        return not self.yielded

    def in_flight(self):
        return self.busy

    def yield_gpu(self):
        self.yielded = True


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A managed server over the fake script; `env.ctl(**doc)` scripts the next start."""
    monkeypatch.setenv("HF_TOKEN", "synthetic-shell-secret")
    monkeypatch.setenv("LLAMA_ARG_HOST", "0.0.0.0")
    ctl = tmp_path / "ctl.json"
    script = tmp_path / "bin" / "serve"
    script.parent.mkdir()
    script.write_text(FAKE_SERVER.format(python=sys.executable, ctl=ctl))
    script.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "token").write_text(TOKEN + "\n")
    (tmp_path / "token").chmod(0o600)
    state = tmp_path / "state" / "gateway"

    def write_config(extra_env='FAKE_MODE = "long"'):
        toml = tmp_path / "gw.toml"
        toml.write_text(
            f'[gateway]\nunix_path = "{tmp_path}/run/gw.sock"\ntoken_file = "{tmp_path}/token"\n'
            '[gateway.gpu]\ncoordinator = "none"\n'
            f'[gateway.backends.ollama]\ntype = "ollama"\nurl = "http://127.0.0.1:{free_port()}"\n'
            f'[gateway.backends.qwen]\ntype = "managed-openai"\ncommand = ["{script}", "--flag"]\n'
            f'cwd = "{work}"\nenv = {{ {extra_env} }}\nport = {port}\nkey_env = "FAKE_KEY"\n'
            'served_models = ["m1"]\nstartup_timeout_s = 2.0\nstop_timeout_s = 1.0\nmax_restarts = 2\n'
        )
        return load_config(toml), toml

    port = free_port()
    cfg, toml = write_config()

    class Env:
        pass

    e = Env()
    e.tmp, e.state, e.cfg, e.toml, e.write_config = tmp_path, state, cfg, toml, write_config
    e.servers = []

    def server(entry_cfg=None):
        made = ManagedServer("qwen", (entry_cfg or cfg).backends["qwen"], ollama_url="")
        made.poll_s = 0.05
        made.configure(state)
        e.servers.append(made)
        return made

    e.server = server
    e.ctl = lambda **doc: ctl.write_text(json.dumps(doc))
    e.ctl()
    e.run = lambda: json.loads((tmp_path / "ctl.json.run").read_text())
    e.child = lambda: int((tmp_path / "ctl.json.child").read_text())
    yield e
    for made in e.servers:
        made.stop()


@pytest.fixture
def gateway(env):
    """A started gateway with the managed backend, its Ollama backend replaced by a fake."""
    gw = Gateway(env.cfg, state_dir=env.state)
    gw.backends["qwen"].poll_s = 0.05
    gw.backends["ollama"] = FakeOllama()
    gw.start()
    yield gw
    for server in gw.managed().values():
        server.stop()
    gw.stop()


def _unix_connect(conn, path):
    conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.sock.settimeout(10)
    conn.sock.connect(str(path))


def call(gw, method, path, doc=None, headers=None):
    conn = http.client.HTTPConnection("localhost")
    conn.connect = lambda: _unix_connect(conn, gw.socket_path())
    raw = json.dumps(doc or {}).encode()
    conn.request(
        method,
        path,
        body=raw if method == "POST" else None,
        headers={"Content-Length": str(len(raw)), **(headers or {})},
    )
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read())


def test_start_waits_for_health_and_writes_the_record(env):
    env.ctl(ready_after=3)
    server = env.server()
    server.start()
    record = json.loads((env.state / "qwen.json").read_text())
    assert (
        launchers.is_alive(record["pid"], record["pid_start"])
        and record["config_sha256"] == server.fingerprint()
    )
    assert oct((env.state / "qwen.json").stat().st_mode & 0o777) == "0o600"
    assert oct((env.state / "qwen.key").stat().st_mode & 0o777) == "0o600"
    assert server.status()["state"] == "running" and server.ready()


def test_child_gets_only_allowlisted_env_plus_port_host_and_key(env):
    server = env.server()
    server.start()
    run = env.run()
    assert run["argv"] == ["--flag"] and run["cwd"] == str(env.tmp / "work")
    child_env = run["env"]
    assert child_env["PORT"] == str(server.entry["port"]) and child_env["HOST"] == "127.0.0.1"
    assert child_env["FAKE_KEY"] == (env.state / "qwen.key").read_text().strip()
    assert child_env["FAKE_MODE"] == "long"
    allowed = {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "CUDA_VISIBLE_DEVICES",
        "PORT",
        "HOST",
        "FAKE_KEY",
        "FAKE_MODE",
    }
    # the interpreter may add its own variables at start (LC_CTYPE on some systems)
    assert set(child_env) - allowed <= {"LC_CTYPE"}


def test_shell_secrets_never_reach_the_child(env):
    env.server().start()
    child_env = env.run()["env"]
    assert "HF_TOKEN" not in child_env and "LLAMA_ARG_HOST" not in child_env
    assert "synthetic-shell-secret" not in json.dumps(child_env)


def test_key_is_never_in_the_argv(env):
    env.server().start()
    key = (env.state / "qwen.key").read_text().strip()
    assert key not in " ".join(env.run()["argv"])


def test_exit_during_startup_fails_with_the_log_path(env):
    env.ctl(exit=True)
    server = env.server()
    with pytest.raises(Unavailable, match="exited during startup") as err:
        server.start()
    assert "qwen.log" in str(err.value) and not server.running()
    assert not (env.state / "qwen.json").exists()


def test_startup_timeout_stops_the_process_group(env):
    env.ctl(never_ready=True, child=True)
    server = env.server()
    begun = time.monotonic()
    with pytest.raises(Unavailable, match="not ready after 2 s") as err:
        server.start()
    assert time.monotonic() - begun >= 2.0 and "qwen.log" in str(err.value)
    assert not launchers.is_alive(env.child()) and not server.running()


def test_stop_ends_the_whole_process_group(env):
    env.ctl(child=True)
    server = env.server()
    server.start()
    pid, child = server._record["pid"], env.child()
    assert launchers.is_alive(child)
    server.stop()
    assert not launchers.is_alive(pid) and not launchers.is_alive(child)
    assert launchers.group_members(pid) == []


def test_stop_escalates_to_sigkill(env):
    env.ctl(child=True, ignore_term=True)
    server = env.server()
    server.start()
    pid, child = server._record["pid"], env.child()
    begun = time.monotonic()
    server.stop()
    assert time.monotonic() - begun >= 1.0
    assert not launchers.is_alive(pid) and not launchers.is_alive(child)


def test_stop_ends_children_left_after_the_leader_exited(env):
    env.ctl(child=True)
    server = env.server()
    server.start()
    os.kill(server._record["pid"], signal.SIGKILL)
    time.sleep(0.2)
    assert launchers.is_alive(env.child())
    server.stop()
    assert not launchers.is_alive(env.child())


def test_server_is_adopted_across_gateway_restart_when_config_is_unchanged(env):
    server = env.server()
    server.start()
    pid = server._record["pid"]
    other = env.server()
    assert other.adopt() == "adopted" and other.running() and other._record["pid"] == pid


def test_changed_config_stops_the_old_server_at_start(env):
    server = env.server()
    server.start()
    pid = server._record["pid"]
    changed, _ = env.write_config(extra_env='FAKE_MODE = "short"')
    other = env.server(changed)
    assert other.fingerprint() != server.fingerprint()
    assert other.adopt() == "stopped" and not launchers.is_alive(pid)


def test_crash_restart_budget(env):
    server = env.server()
    server.ensure()
    for _ in range(2):
        os.kill(server._record["pid"], signal.SIGKILL)
        time.sleep(0.2)
        server.ensure()
        assert server.running()
    os.kill(server._record["pid"], signal.SIGKILL)
    time.sleep(0.2)
    with pytest.raises(Unavailable, match="crashed 2 times") as err:
        server.ensure()
    assert err.value.status == 503 and "qwen.log" in str(err.value)


def test_stopped_server_is_not_restarted_by_a_request(env):
    server = env.server()
    server.start()
    server.stop()
    with pytest.raises(Unavailable, match="was stopped"):
        server.ensure()
    assert server.status()["state"] == "stopped"


def test_free_vram_stops_the_server_and_returns_its_models(env):
    server = env.server()
    server.start()
    assert server.free_vram() == ["m1"] and not server.running()
    server.ensure()  # not a deliberate stop: the next request starts it again
    assert server.running()


def test_model_listing_shows_served_models_without_starting_the_server(gateway):
    status, doc = call(gateway, "GET", "/v1/models", headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 200 and "qwen/m1" in [m["id"] for m in doc["data"]]
    assert gateway.resolver.resolve("m1").backend.name == "qwen"
    assert not gateway.backends["qwen"].running()


def test_request_starts_the_server_on_demand_and_carries_its_key(gateway):
    status, doc = call(gateway, "POST", "/v1/chat/completions", {"model": "qwen/m1", "messages": []})
    assert status == 200 and doc["echo"]["model"] == "m1"
    assert gateway.backends["qwen"].running()


def test_effort_off_disables_thinking_on_a_managed_server(env):
    gw = Gateway(env.cfg, state_dir=env.state)
    gw.backends["qwen"].entry = {**gw.backends["qwen"].entry, "served_models": ["qwen3.8-27b"]}
    env.ctl(models=["qwen3.8-27b"])
    gw.backends["qwen"].poll_s = 0.05
    gw.backends["ollama"] = FakeOllama()
    gw.start()
    try:
        status, doc = call(
            gw,
            "POST",
            "/v1/chat/completions",
            {"model": "qwen/qwen3.8-27b", "messages": []},
            {"X-Hal-Effort": "off"},
        )
        assert status == 200 and doc["echo"]["chat_template_kwargs"] == {"enable_thinking": False}
    finally:
        gw.backends["qwen"].stop()
        gw.stop()


def test_managed_start_yields_the_router_and_empties_ollama(gateway):
    router = FakeGpuBackend()
    gateway.backends["local"] = router
    gateway.backends["qwen"].start()
    assert router.yielded and gateway.backends["ollama"].held == []


def test_managed_start_refuses_to_stop_a_backend_with_requests_running(gateway):
    gateway.backends["local"] = FakeGpuBackend(busy=1)
    with pytest.raises(Unavailable, match="request"):
        gateway.backends["qwen"].start()
    assert not gateway.backends["qwen"].running()


def test_router_start_stops_the_managed_server_without_marking_it_stopped(gateway):
    server = gateway.backends["qwen"]
    server.start()
    gateway._make_room(FakeGpuBackend())  # what a router start calls through `before_start`
    assert not server.running() and server.status()["state"] == "stopped"
    server.ensure()
    assert server.running()


def test_idle_unload_keeps_a_server_with_idle_stop_false(env):
    cfg, _ = env.write_config()
    entry = {**cfg.backends["qwen"], "idle_stop": False}
    gw = Gateway(cfg, state_dir=env.state)
    keep = ManagedServer("qwen", entry, ollama_url="")
    keep.poll_s = 0.05
    keep.configure(env.state)
    env.servers.append(keep)
    gw.gpu.backends = {"qwen": keep}
    keep.start()
    gw.gpu._dirty = True
    gw.gpu.tick(time.monotonic() + 10_000)
    assert keep.running()
    assert gw.gpu.release(force=False, stop_router=False, reclaim=False).unloaded == ("m1",)
    assert not keep.running()


def test_cli_backend_start_stop_and_restart_reach_the_admin_routes(gateway, env, capsys):
    assert cli.main(["backend", "start", "qwen", "--config", str(env.toml)]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["backends"]["qwen"]["state"] == "running"
    pid = gateway.backends["qwen"]._record["pid"]
    assert cli.main(["backend", "restart", "qwen", "--config", str(env.toml)]) == cli.EXIT_OK
    assert gateway.backends["qwen"]._record["pid"] != pid
    assert cli.main(["backend", "stop", "qwen", "--config", str(env.toml)]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["backends"]["qwen"]["state"] == "stopped"
    assert cli.main(["backend", "start", "nope", "--config", str(env.toml)]) == cli.EXIT_GATEWAY
    assert "404" in capsys.readouterr().err


def test_release_gpu_stop_router_stops_managed_servers(gateway, env, capsys):
    gateway.backends["qwen"].start()
    assert cli.main(["release-gpu", "--stop-router", "--config", str(env.toml)]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["router_stopped"] is True
    assert gateway.backends["qwen"].status()["state"] == "stopped"
    status, _ = call(gateway, "POST", "/v1/chat/completions", {"model": "qwen/m1", "messages": []})
    assert status == 503


def test_status_lists_the_managed_server(gateway):
    status, doc = call(gateway, "GET", "/hal/v1/status")
    qwen = doc["backends"]["qwen"]
    assert status == 200 and qwen["type"] == "managed-openai" and qwen["state"] == "stopped"
    assert qwen["models"] == ["m1"]
