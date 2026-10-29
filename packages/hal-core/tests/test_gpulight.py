"""Tests for `halcore.gpulight`: the claim command, its timeout and its wait message.

Run with: uv run pytest packages/hal-core
"""
import os
import subprocess
import sys
import json
import unittest
from unittest import mock

from halcore_env import HUB_SRC, SRC, LightCase
from halcore import gpulight


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
        env = {**os.environ, "PYTHONPATH": f"{SRC}:{HUB_SRC}", "FAKE_GPU_LIGHT_CLAIM_EXIT": "1"}
        proc = subprocess.run([sys.executable, "-m", "halcore", "gpulight", "claim", "hal", "--no-wait"],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("hal: gpu-light claim failed (exit 1)", proc.stderr)


if __name__ == "__main__":
    unittest.main()
