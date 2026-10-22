"""Tests for the GPU light hook: Hal claims gpu-light before it uses the GPU and releases after.

A fake `gpu-light` logs its arguments, and HAL_GPU_LIGHT points at it, so a real gpu-light
installed on the machine is never called. systemctl, Ollama and desktop notifications are
replaced in-process; nothing here starts a service, loads a model or touches a GPU.
Run with: python3 -m unittest discover -s tests
"""
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="halcore-gpulight-test-")
os.environ.update(HAL_CONFIG=f"{TMP}/config.toml", HAL_STATE=f"{TMP}/state", HAL_DATA=f"{TMP}/data")

from halcore import gpulight, power, units  # noqa: E402

FAKE_GPU_LIGHT = """#!/usr/bin/env python3
import os, sys, time
with open(os.environ["FAKE_GPU_LIGHT_LOG"], "a") as log:
    log.write("gpu-light " + " ".join(sys.argv[1:]) + "\\n")
cmd = sys.argv[1]
if cmd == "status":
    path = os.environ.get("FAKE_GPU_LIGHT_STATUS", "")
    print(open(path).read() if path else "{}")
elif cmd == "claim":
    time.sleep(float(os.environ.get("FAKE_GPU_LIGHT_SLEEP", "0")))
    sys.exit(int(os.environ.get("FAKE_GPU_LIGHT_CLAIM_EXIT", "0")))
"""

# Stands in for llama-server: logs that it started, then idles until hal-serve stops it
FAKE_ROUTER = """#!/bin/sh
echo "router start" >> "$FAKE_GPU_LIGHT_LOG"
exec sleep 60
"""
# Every preset name in modes/, so the fake server reports each mode's models as loaded
PRESETS = sorted({name for ini in (ROOT / "modes").glob("*.ini")
                  for name in re.findall(r"^\[([^]*][^]]*)\]$", ini.read_text(), re.M)})


class FakeModelServer(BaseHTTPRequestHandler):
    """Answers the llama.cpp router's /models and Ollama's /api/version and /api/ps."""

    def do_GET(self):
        if self.path == "/models":
            body = {"data": [{"id": n, "status": {"value": "loaded"}} for n in PRESETS]}
        elif self.path == "/api/ps":
            body = {"models": []}
        else:
            body = {"version": "0.0.0"}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def serve_models() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModelServer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class LightCase(unittest.TestCase):
    """Puts a fake gpu-light in a fresh folder and records its calls next to systemctl's."""

    def setUp(self):
        # The hook touches Hal's off flag; it must never be the real one.
        self.assertTrue(os.environ["HAL_STATE"].startswith(tempfile.gettempdir()))
        self.dir = Path(tempfile.mkdtemp(dir=TMP))
        self.log = self.dir / "calls.log"
        self.log.touch()
        fake = self.dir / "gpu-light"
        fake.write_text(FAKE_GPU_LIGHT)
        fake.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {"HAL_GPU_LIGHT": str(fake),
                                                "FAKE_GPU_LIGHT_LOG": str(self.log)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def record(self, text: str) -> None:
        with self.log.open("a") as f:
            f.write(text + "\n")

    def fake_systemctl(self):
        def systemctl(*args, check=False):
            self.record("systemctl " + " ".join(args))
            return subprocess.CompletedProcess(["systemctl", *args], 0, "", "")
        return mock.patch.object(units, "systemctl", systemctl)

    def fake_ollama(self, models):
        """Serves `models` from /api/ps and records every unload."""
        def ollama(path, payload=None, timeout=10):
            if path == "/api/ps":
                return {"models": models}
            self.record(f"ollama unload {payload['model']}")
            return {}
        return mock.patch.object(power, "_ollama", ollama)


class PowerTest(LightCase):
    def setUp(self):
        super().setUp()
        for patch in (self.fake_systemctl(), self.fake_ollama([]),
                      mock.patch.object(power, "_notify", lambda *a: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_hal_on_claims_the_light_before_starting_hal_target(self):
        msg = power.on(quiet=True)
        calls = self.calls()
        claim = next(i for i, c in enumerate(calls) if c.startswith("gpu-light claim"))
        self.assertIn("--name hal --reason hal on --wait --timeout 90", calls[claim])
        self.assertLess(claim, calls.index("systemctl start hal.target"))
        self.assertNotIn("Warning", msg)

    def test_hal_on_starts_hal_anyway_when_the_wait_times_out(self):
        with mock.patch.dict(os.environ, {"FAKE_GPU_LIGHT_CLAIM_EXIT": "4"}):
            msg = power.on(quiet=True)
        self.assertIn("systemctl start hal.target", self.calls())
        self.assertIn("the GPU is still busy after 90 s", msg)

    def test_hal_on_unloads_models_offloaded_to_ram_after_a_claim(self):
        models = [{"name": "split", "size": 10, "size_vram": 6}, {"name": "whole", "size": 10, "size_vram": 10}]
        with self.fake_ollama(models):
            power.on(quiet=True)
        self.assertIn("ollama unload split", self.calls())
        self.assertNotIn("ollama unload whole", self.calls())

    def test_hal_on_keeps_offloaded_models_when_the_gpu_is_still_busy(self):
        with self.fake_ollama([{"name": "split", "size": 10, "size_vram": 6}]), \
                mock.patch.dict(os.environ, {"FAKE_GPU_LIGHT_CLAIM_EXIT": "4"}):
            power.on(quiet=True)
        self.assertNotIn("ollama unload split", self.calls())

    def test_hal_off_releases_the_light_after_unloading_models(self):
        with self.fake_ollama([{"name": "chat", "size": 10, "size_vram": 10}]):
            power.off(quiet=True)
        calls = self.calls()
        self.assertLess(calls.index("ollama unload chat"), calls.index("gpu-light release --name hal"))

    def test_hal_on_and_off_are_unchanged_without_gpu_light(self):
        with mock.patch.dict(os.environ, {"HAL_GPU_LIGHT": "off"}):
            on, off = power.on(quiet=True), power.off(quiet=True)
        self.assertEqual(self.calls(), ["systemctl start hal.target"])
        self.assertNotIn("Warning", on + off)

    def test_a_failing_gpu_light_is_a_warning_not_an_error(self):
        with mock.patch.dict(os.environ, {"FAKE_GPU_LIGHT_CLAIM_EXIT": "1"}):
            msg = power.on(quiet=True)
        self.assertIn("systemctl start hal.target", self.calls())
        self.assertIn("gpu-light claim failed (exit 1)", msg)

    def test_status_shows_the_light_and_its_reason(self):
        doc = self.dir / "status.json"
        doc.write_text(json.dumps({"effective": "red", "reason": "claim hal (boot)"}))
        with mock.patch.dict(os.environ, {"FAKE_GPU_LIGHT_STATUS": str(doc)}):
            self.assertIn("GPU light: red (claim hal (boot))", power.status())


class ClaimTest(LightCase):
    def test_a_claim_that_hangs_is_cut_off_by_its_timeout(self):
        with mock.patch.object(gpulight, "SLACK_S", 0.2), \
                mock.patch.dict(os.environ, {"FAKE_GPU_LIGHT_SLEEP": "5"}):
            out = gpulight.claim("hal", "test", timeout=0)
        self.assertFalse(out.ok)
        self.assertIn("did not finish within 0.2 s", out.warning)

    def test_the_wait_message_names_the_background_jobs(self):
        doc = self.dir / "status.json"
        doc.write_text(json.dumps({"effective": "red", "background": [{"client": "batch"}, {"client": "batch"}]}))
        said = []
        with mock.patch.dict(os.environ, {"FAKE_GPU_LIGHT_STATUS": str(doc)}):
            gpulight.claim("hal", "test", timeout=7, on_wait=said.append)
        self.assertEqual(said, ["waiting up to 7 s for batch to free the GPU"])

    def test_no_wait_message_when_the_light_is_stale(self):
        doc = self.dir / "status.json"
        doc.write_text(json.dumps({"effective": "unknown", "background": [{"client": "batch"}]}))
        said = []
        with mock.patch.dict(os.environ, {"FAKE_GPU_LIGHT_STATUS": str(doc)}):
            gpulight.claim("hal", "test", on_wait=said.append)
        self.assertEqual(said, [])

    def test_a_pid_claim_without_wait_passes_the_pid(self):
        self.assertTrue(gpulight.claim("hal-serve", "router", pid=4242, wait=False).ok)
        self.assertEqual(self.calls(), ["gpu-light claim --name hal-serve --reason router --pid 4242"])

    def test_a_bad_claim_name_fails_loudly(self):
        with self.assertRaises(ValueError):
            gpulight.claim("Hal session", "test")

    def test_the_command_line_exits_0_when_the_claim_fails(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT), "FAKE_GPU_LIGHT_CLAIM_EXIT": "1"}
        proc = subprocess.run([sys.executable, "-m", "halcore", "gpulight", "claim", "hal", "--no-wait"],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("hal: gpu-light claim failed (exit 1)", proc.stderr)


class ServeTest(LightCase):
    """Runs the real hal-serve against a fake router and a fake model server on localhost."""

    def setUp(self):
        super().setUp()
        server = serve_models()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        router = self.dir / "llama-server"
        router.write_text(FAKE_ROUTER)
        router.chmod(0o755)
        (self.dir / "models").mkdir()
        conf = self.dir / "hal.conf"
        conf.write_text(f'HAL_LLAMA_SERVER="{router}"\nHAL_MODELS_DIR="{self.dir}/models"\n'
                        f'HAL_LLAMA_URL="{url}"\nHAL_OLLAMA_URL="{url}"\n')
        self.state = self.dir / "state"
        self.env_vars = {**os.environ, "HAL_CONF": str(conf), "XDG_STATE_HOME": str(self.state),
                         "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        self.addCleanup(self.kill_router)

    def serve(self, *args, **env) -> subprocess.CompletedProcess:
        return subprocess.run([str(ROOT / "hal-serve"), *args], capture_output=True, text=True,
                              env={**self.env_vars, **env}, timeout=60)

    def router_pid(self) -> int:
        return int((self.state / "hal" / "router.pid").read_text())

    def kill_router(self):
        """Stops the fake router if a test left it running; it is this test's own process."""
        try:
            os.kill(self.router_pid(), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def test_hal_serve_claims_before_the_router_starts_and_binds_the_claim_to_it(self):
        proc = self.serve("start", "gpu")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls, pid = self.calls(), self.router_pid()
        wait = calls.index("gpu-light claim --name hal-serve --reason llama.cpp router gpu --wait --timeout 120")
        bind = calls.index(f"gpu-light claim --name hal-serve --reason llama.cpp router gpu --pid {pid}")
        self.assertLess(wait, calls.index("router start"))
        self.assertLess(calls.index("router start"), bind)

    def test_hal_serve_claims_in_cpu_mode_too(self):
        # cpu mode keeps the weights in RAM but still puts about 3GB of VRAM to work
        self.assertEqual(self.serve("start", "cpu").returncode, 0)
        self.assertIn("gpu-light claim --name hal-serve --reason llama.cpp router cpu --wait --timeout 120",
                      self.calls())

    def test_hal_stop_releases_the_light_after_the_router_exits(self):
        self.assertEqual(self.serve("start", "gpu").returncode, 0)
        pid = self.router_pid()
        proc = self.serve("stop")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls()[-1], "gpu-light release --name hal-serve")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_hal_serve_is_unchanged_without_gpu_light(self):
        self.assertEqual(self.serve("start", "gpu", HAL_GPU_LIGHT="off").returncode, 0)
        self.assertEqual(self.serve("stop", HAL_GPU_LIGHT="off").returncode, 0)
        self.assertEqual(self.calls(), ["router start"])


if __name__ == "__main__":
    unittest.main()
