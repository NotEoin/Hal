"""Tests for the managed llama-server router (REQ-GW-32..39) against a fake llama-server script."""

import hashlib
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from halgateway import launchers, park, relay
from halgateway.backends.llama_router import LlamaRouter, startup_presets
from halgateway.config import load_config
from halgateway.errors import ModelUnverified, RouterBusy
from halgateway.models.manifest import ModelEntry, Verifier

WEIGHTS = b"synthetic weights"
FAKE_SERVER = """\
#!{python}
import http.server, json, os, signal, sys, time
args = sys.argv[1:]
ctl_path = "{ctl}"
port = int(args[args.index("--port") + 1])
key = open(args[args.index("--api-key-file") + 1]).read().strip()
json.dump({{"argv": args, "env": dict(os.environ)}}, open(ctl_path + ".run", "w"))
if json.load(open(ctl_path)).get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(json.load(open(ctl_path)).get("listen_delay_s", 0))

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, doc, status=200):
        raw = json.dumps(doc).encode()
        self.send_response(status); self.send_header("Content-Length", str(len(raw))); self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + key:
            return self._send({{}}, 401)
        if self.path == "/health":
            return self._send({{"status": "ok"}})
        ctl = json.load(open(ctl_path))
        data = []
        for name, seq in ctl["statuses"].items():
            value = seq.pop(0) if len(seq) > 1 else seq[0]
            data.append({{"id": name, "status": {{"value": value}}}})
        json.dump(ctl, open(ctl_path, "w"))
        self._send({{"data": data}})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        open(ctl_path + ".calls", "a").write(self.path + " " + body["model"] + "\\n")
        self._send({{"success": True}})

http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
"""
MODE = """\
version = 1
[hal]
main = m
[*]
ctx-size = 8192
[m]
model = {model}
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeOllama:
    name = "ollama"
    type = "ollama"

    def __init__(self, order):
        self.order, self.held = order, ["gemma:1"]

    def in_vram(self):
        return list(self.held)

    def wait_unloaded(self, models):
        return not set(models) & set(self.held)

    def unload(self, model):
        self.order.append(f"unloaded {model}")
        self.held.remove(model)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A router over a fake llama-server; `env.ctl(**doc)` scripts the fake's `/models` answers."""
    monkeypatch.setenv("HF_TOKEN", "synthetic-token")
    models = tmp_path / "models"
    models.mkdir()
    weights = models / "m.gguf"
    weights.write_bytes(WEIGHTS)
    modes_dir = tmp_path / "modes"
    modes_dir.mkdir()
    (modes_dir / "one.ini").write_text(MODE.format(model=weights))
    (modes_dir / "two.ini").write_text(MODE.format(model=weights) + "[n]\nmodel = " + str(weights) + "\n")
    ctl = tmp_path / "ctl.json"
    server = tmp_path / "bin" / "llama-server"
    server.parent.mkdir()
    server.write_text(FAKE_SERVER.format(python=sys.executable, ctl=ctl))
    server.chmod(0o755)
    digest = hashlib.sha256(WEIGHTS).hexdigest()
    entry = ModelEntry("m.gguf", "local", weights, "gguf", digest, len(WEIGHTS))
    state = tmp_path / "state" / "gateway"
    toml = tmp_path / "gw.toml"
    toml.write_text(
        f'[gateway.backends.local]\ntype = "llama-router"\nserver = "{server}"\nmodels_dir = "{models}"\n'
        f'mode = "one"\nmodes_dirs = ["{modes_dir}"]\nport = {free_port()}\nstop_timeout_s = 1.0\n'
        "load_timeout_s = 3.0\nmax_restarts = 2\n"
        f'[gateway]\nunix_path = "{tmp_path}/run/gw.sock"\ntoken_file = "{tmp_path}/token"\n'
    )
    (tmp_path / "token").write_text("synthetic-owner-token\n")
    (tmp_path / "token").chmod(0o600)
    cfg = load_config(toml)
    order = []
    ollama = FakeOllama(order)
    router = LlamaRouter("local", cfg.backends["local"], ollama_url="")
    router.poll_s = 0.05
    verifier = Verifier({"m.gguf": entry}, tmp_path / "verified.json")
    router.configure(state, models, verifier, lambda: [ollama])

    class Env:
        pass

    e = Env()
    e.router, e.state, e.order, e.weights = router, state, order, weights
    e.tmp, e.cfg, e.ollama = tmp_path, cfg, ollama
    e.verifier, e.models = verifier, models
    e.ctl = lambda **doc: ctl.write_text(json.dumps({"statuses": {"m": ["loaded"], "n": ["loaded"]}, **doc}))
    e.ctl()
    e.run = lambda: json.loads((ctl.parent / "ctl.json.run").read_text())
    yield e
    router.stop()


def test_start_waits_for_the_startup_preset_and_writes_the_record(env):
    env.ctl(statuses={"m": ["loading", "loading", "loaded"]})
    env.router.start()
    record = json.loads((env.state / "router.json").read_text())
    assert record["mode"] == "one" and launchers.is_alive(record["pid"], record["pid_start"])
    assert oct((env.state / "router.json").stat().st_mode & 0o777) == "0o600"
    assert env.router.status()["state"] == "running"


def test_a_router_with_only_on_demand_presets_answers_once_started(env):
    # regression: with nothing to load at start, a request relayed before the server listened got a 502
    mode = env.tmp / "modes" / "one.ini"
    mode.write_text(mode.read_text().replace("[*]\n", "[*]\nload-on-startup = false\n"))
    env.ctl(listen_delay_s=0.5)
    env.router.start()
    assert relay.call_json(env.router, "GET", "/models", timeout_s=2.0)["data"]


def test_router_argv_and_env(env):
    env.router.start()
    run = env.run()
    argv = run["argv"]
    assert argv[argv.index("--host") + 1] == "127.0.0.1" and "--no-webui" in argv
    key_file = argv[argv.index("--api-key-file") + 1]
    assert oct(os.stat(key_file).st_mode & 0o777) == "0o600"
    assert "HF_TOKEN" not in run["env"] and run["env"]["GPU_LIGHT_SERVICE"] == "hal-gateway"


def test_ollama_models_are_unloaded_before_router_start(env):
    env.router.start()
    assert env.order == ["unloaded gemma:1"] and env.ollama.held == []


@pytest.mark.parametrize(
    ("statuses", "message"),
    [
        (["failed"], "failed to load"),
        (["loading", "unloaded"], "failed to load"),
        (["loading"], "still not loaded"),
    ],
)
def test_load_wait_outcomes(env, statuses, message):
    env.ctl(statuses={"m": statuses})
    with pytest.raises(Exception, match=message) as err:
        env.router.start()
    assert "router.log" in str(err.value) and not env.router.running()


def test_router_exit_during_load_fails_with_the_log_path(env):
    env.ctl(statuses={"m": ["loading"]})
    timer = [None]

    def kill_soon():
        time.sleep(0.5)
        os.kill(env.router._record["pid"], signal.SIGKILL)

    import threading

    timer[0] = threading.Thread(target=kill_soon)
    timer[0].start()
    with pytest.raises(Exception, match="exited while loading"):
        env.router.start()
    timer[0].join()


def test_stop_escalates_to_sigkill(env):
    env.ctl(ignore_term=True)
    env.router.start()
    pid = env.router._record["pid"]
    begun = time.monotonic()
    env.router.stop()
    assert not launchers.is_alive(pid) and time.monotonic() - begun >= 1.0


def test_reused_pid_is_never_signalled():
    child = subprocess.Popen(["sleep", "30"])
    try:
        launchers.ProcessLauncher().stop(child.pid, launchers.pid_start(child.pid) + 1, 0.2)
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_router_is_adopted_across_gateway_restart(env):
    env.router.start()
    pid = env.router._record["pid"]
    other = LlamaRouter("local", env.cfg.backends["local"], ollama_url="")
    other.configure(env.state, env.models, env.verifier, lambda: [])
    assert other.adopt() == "adopted" and other.running() and other._record["pid"] == pid


def test_router_of_another_mode_is_stopped_at_start(env):
    env.router.start("two")
    pid = env.router._record["pid"]
    other = LlamaRouter("local", env.cfg.backends["local"], ollama_url="")
    other.configure(env.state, env.models, env.verifier, lambda: [])
    assert other.adopt() == "stopped" and not launchers.is_alive(pid)


def test_crash_restart_budget(env):
    env.router.ensure()
    for _ in range(2):
        os.kill(env.router._record["pid"], signal.SIGKILL)
        time.sleep(0.2)
        env.router.ensure()
        assert env.router.running()
    os.kill(env.router._record["pid"], signal.SIGKILL)
    time.sleep(0.2)
    with pytest.raises(Exception, match="crashed 2 times") as err:
        env.router.ensure()
    assert err.value.status == 503 and "router.log" in str(err.value)


def test_stopped_router_is_not_restarted_by_a_request(env):
    env.router.start()
    env.router.stop()
    with pytest.raises(Exception, match="was stopped"):
        env.router.ensure()


def test_yield_gpu_stops_the_router_without_marking_it_stopped(env):
    env.router.start()
    pid = env.router._record["pid"]
    env.router.yield_gpu()
    assert not launchers.is_alive(pid) and not env.router.running()
    env.router.ensure()  # a yield is not `router/stop`: the next request starts it again
    assert env.router.running()


def test_a_parked_router_keeps_its_process_and_resumes_after_making_room(env):
    calls = []

    class Parker:
        def park(self, root):
            calls.append("park")
            return (root,)

        def resume(self, pids):
            calls.append("resume")

    env.router.parking = park.Parking(True, Parker())
    env.router.start()
    pid = env.router._record["pid"]
    env.router.yield_gpu()
    assert launchers.is_alive(pid) and env.router.parking.parked
    env.router.before_start = lambda router: calls.append("room")
    env.ollama.held = ["gemma:1"]
    env.router.ensure()  # the next hal-writer request
    assert calls == ["park", "room", "resume"] and env.ollama.held == [] and env.router._record["pid"] == pid


def test_router_start_calls_before_start_first(env):
    seen = []
    env.router.before_start = lambda router: seen.append((router.running(), list(env.order)))
    env.router.start()
    assert seen == [(False, [])] and env.order == ["unloaded gemma:1"]


def test_mode_switch_waits_for_inflight_requests(env):
    env.router.start()
    with env.router.request():
        begun = time.monotonic()
        with pytest.raises(RouterBusy):
            env.router.switch("two", drain_s=0.3)
        assert time.monotonic() - begun >= 0.3
        env.router.switch("two", drain_s=0.0, force=True)
    assert env.router._record["mode"] == "two"


def test_load_checks_the_weights_before_asking_the_router(env):
    env.router.start()
    env.router.load("m")
    assert "/models/load m" in (env.tmp / "ctl.json.calls").read_text()
    env.weights.write_bytes(b"tampered weights")
    with pytest.raises(ModelUnverified):
        env.router.load("m")


def test_startup_presets_skip_on_demand_presets(tmp_path):
    ini = tmp_path / "x.ini"
    ini.write_text("version = 1\n[*]\nctx-size = 1\n[a]\nmodel = a\n[b]\nload-on-startup = false\n")
    assert startup_presets(ini) == ["a"]


def test_a_preset_inherits_load_on_startup_from_the_default_section(tmp_path):
    # regression: the tuned mode sets it only in [*], and the first request waited for a load that never came
    ini = tmp_path / "x.ini"
    ini.write_text("version = 1\n[*]\nload-on-startup = false\n[a]\nmodel = a\n[b]\nload-on-startup = true\n")
    assert startup_presets(ini) == ["b"]


def _unix_connect(conn, path):
    conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.sock.settimeout(10)
    conn.sock.connect(str(path))


def admin(gw, method, path, doc=None):
    conn = http.client.HTTPConnection("localhost")
    conn.connect = lambda: _unix_connect(conn, gw.socket_path())
    raw = json.dumps(doc or {}).encode()
    headers = {"Content-Length": str(len(raw))}
    conn.request(method, path, body=raw if method == "POST" else None, headers=headers)
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read())


def test_admin_mode_switch_and_status_over_the_unix_socket(env):
    from halgateway.server import Gateway

    manifest = env.tmp / "userconfig" / "models.toml"
    manifest.parent.mkdir(exist_ok=True)
    manifest.write_text(
        f'schema = 1\n[[model]]\nname = "m"\nfile = "{env.weights}"\nformat = "gguf"\n'
        f'sha256 = "{hashlib.sha256(WEIGHTS).hexdigest()}"\nsize = {len(WEIGHTS)}\nsource = "local"\n'
    )
    gw = Gateway(env.cfg, state_dir=env.state)
    gw.start()
    try:
        status, doc = admin(gw, "POST", "/hal/v1/mode", {"mode": "two"})
        assert status == 200 and doc["backends"]["local"]["mode"] == "two"
        assert gw.resolver.mode_aliases["hal/main"] == ("local", "m")
        assert gw.resolver.mode_windows[("local", "m")] == 8192
        status, doc = admin(gw, "GET", "/hal/v1/status")
        assert status == 200 and doc["backends"]["local"]["state"] == "running"
        status, doc = admin(gw, "POST", "/hal/v1/router/stop")
        assert status == 200 and doc["backends"]["local"]["state"] == "stopped"
        status, doc = admin(gw, "POST", "/hal/v1/mode", {"mode": ""})
        assert status == 400
    finally:
        gw.router().stop()
        gw.stop()
