"""Tests for the bash launchers `hal-serve` and `hal`: gpu-light claims around the router and sessions.

The real scripts run against a fake router, a fake Claude Code and a fake model server on
localhost.
Run with: uv run pytest packages/hal-core
"""
import os
import signal
import subprocess
import unittest
from unittest import mock

from halcore_env import FAKE_ROUTER, ROOT, LightCase, serve_models


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


class SessionTest(LightCase):
    """Runs the real `hal claude` launcher on a local profile, with a fake Claude Code and Ollama."""

    def setUp(self):
        super().setUp()
        server = serve_models()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"
        claude = self.dir / "claude"
        claude.write_text('#!/bin/sh\necho "claude pid $$" >> "$FAKE_GPU_LIGHT_LOG"\n')
        claude.chmod(0o755)
        (self.dir / "vault").mkdir()
        # Hal's own config names the fake Ollama too, for the unloading after a claim
        (self.dir / "config.toml").write_text(f'[hal]\nollama_url = "{self.url}"\n')

    def launch(self, model: str) -> subprocess.CompletedProcess:
        conf = self.dir / "hal.conf"
        conf.write_text(f'HAL_PROFILE="24gb"\nHAL_MODEL="{model}"\nHAL_OLLAMA_URL="{self.url}"\n')
        env = {**os.environ, "HAL_CONF": str(conf), "HAL_CONFIG": str(self.dir / "config.toml"),
               "HAL_VAULT": str(self.dir / "vault"), "PATH": f"{self.dir}:{os.environ['PATH']}",
               "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        return subprocess.run([str(ROOT / "hal"), "claude"], capture_output=True, text=True, env=env, timeout=60)

    def test_an_ollama_session_claims_the_light_for_its_own_pid(self):
        proc = self.launch("chat-model")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.calls()
        pid = int(calls[-1].removeprefix("claude pid "))
        self.assertIn(f"gpu-light claim --name hal-session-{pid} --reason hal session on chat-model "
                      f"--pid {pid} --wait --timeout 60", calls[:-1])

    def test_a_cloud_session_does_not_claim(self):
        self.assertEqual(self.launch("").returncode, 0)
        self.assertEqual([c for c in self.calls() if c.startswith("gpu-light")], [])

    def test_an_ollama_session_is_unchanged_without_gpu_light(self):
        with mock.patch.dict(os.environ, {"HAL_GPU_LIGHT": "off"}):
            self.assertEqual(self.launch("chat-model").returncode, 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertTrue(self.calls()[0].startswith("claude pid "))


class EntryTest(LightCase):
    """Runs the real `hal` launcher with a fake Claude Code and a fake Python for the Hal CLI."""

    def setUp(self):
        super().setUp()
        claude = self.dir / "claude"
        claude.write_text('#!/bin/sh\necho "claude $*" >> "$FAKE_GPU_LIGHT_LOG"\n')
        claude.chmod(0o755)
        python = self.dir / "fakepython"
        python.write_text('#!/bin/sh\necho "halcore $*" >> "$FAKE_GPU_LIGHT_LOG"\n')
        python.chmod(0o755)
        (self.dir / "vault").mkdir()
        # a cloud profile: no model server is needed to reach the `claude` path
        conf = self.dir / "hal.conf"
        conf.write_text('HAL_PROFILE="cloud"\nHAL_MODEL=""\n')
        self.conf = conf

    def hal(self, *args: str, conf: bool = True) -> subprocess.CompletedProcess:
        env = {**os.environ, "HAL_CONF": str(self.conf if conf else self.dir / "absent.conf"),
               "HAL_PYTHON": str(self.dir / "fakepython"), "HAL_VAULT": str(self.dir / "vault"),
               "PATH": f"{self.dir}:{os.environ['PATH']}"}
        return subprocess.run([str(ROOT / "hal"), *args], capture_output=True, text=True, env=env, timeout=60)

    def test_bare_hal_opens_hals_own_interface_even_without_hal_conf(self):
        proc = self.hal(conf=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls(), ["halcore -m halcore"])

    def test_hal_here_is_no_longer_a_way_into_claude_code(self):
        self.hal("here")
        self.assertEqual(self.calls(), ["halcore -m halcore here"])

    def test_hal_claude_opens_claude_code_with_a_one_line_notice(self):
        proc = self.hal("claude", "--resume")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls(), ["claude --resume"])
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1)
        self.assertIn("not Hal's private local path", proc.stderr)


if __name__ == "__main__":
    unittest.main()
