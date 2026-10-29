"""Shared setup for the halcore tests: import path, throwaway Hal state, fake gpu-light.

Importing this module first points HAL_CONFIG, HAL_STATE and HAL_DATA at a temporary folder, so
no test touches the real Hal. A fake `gpu-light` logs its arguments and HAL_GPU_LIGHT points at
it, so a real gpu-light installed on the machine is never called.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]  # repository root
SRC = ROOT / "packages" / "hal-core" / "src"
sys.path.insert(0, str(SRC))
TMP = tempfile.mkdtemp(prefix="halcore-test-")
os.environ.update(HAL_CONFIG=f"{TMP}/config.toml", HAL_STATE=f"{TMP}/state", HAL_DATA=f"{TMP}/data")

from halcore import power, units  # noqa: E402

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
