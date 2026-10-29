"""Secrets through the runner: declared, resolved, sent in the call envelope, scrubbed.

REQ-HUB-69 and 01-security REQ-SEC-50 to REQ-SEC-52. Integration tests on real workers of the
synthetic packs; hermetic (temporary Hal home, no network, bubblewrap off).
"""

from __future__ import annotations

import dataclasses
import json
import os

import pytest

from halhub.manifest import ModuleSpec, load_manifest
from halhub.runner import Runner, RunnerConfig, SecretError, resolve_secrets, scrub

SECRET = "s3cret-canary-value"  # synthetic; never a real credential
CONFIG = RunnerConfig(kill_grace=0.3, bwrap="off")


@pytest.fixture
def probe(fixtures) -> ModuleSpec:
    return load_manifest(fixtures / "probe" / "hal-module.toml", source="path")


@pytest.fixture
def demo(fixtures) -> ModuleSpec:
    return load_manifest(fixtures / "demo" / "hal-module.toml", source="path")


@pytest.fixture
def runner():
    r = Runner(CONFIG)
    yield r
    r.close()


@pytest.fixture
def key_file(hal_home, tmp_path):
    path = tmp_path / "probe-key"
    path.write_text(SECRET + "\n")
    path.chmod(0o600)
    hal_home.config.write_text(f'[probe]\nkey_file = "{path}"\n')
    return path


def tool(module: ModuleSpec, name: str):
    return next(t for t in module.tools if t.name == name)


def test_secret_reaches_only_declaring_module(runner, probe, demo, key_file):
    secrets = resolve_secrets(probe)
    assert secrets == {"probe_key": SECRET}
    outcome = runner.run(
        probe, tool(probe, "probe_secret"), {"name": "probe_key"}, timeout=5, secrets=secrets
    )
    assert outcome.ok and outcome.result == "the key is [redacted]"
    with pytest.raises(ValueError, match="does not declare"):
        runner.run(demo, tool(demo, "demo_echo"), {"text": "x"}, timeout=5, secrets=secrets)


def test_secret_is_not_in_worker_environment_or_argv(runner, probe, key_file):
    secrets = resolve_secrets(probe)
    outcome = runner.run(probe, tool(probe, "probe_env"), {}, timeout=5, secrets=secrets)
    text = json.dumps(outcome.result)
    assert SECRET not in text and "[redacted]" not in text


def test_secret_value_is_scrubbed_from_result(runner, probe, key_file):
    outcome = runner.run(
        probe, tool(probe, "probe_secret"), {"name": "probe_key"}, timeout=5, secrets=resolve_secrets(probe)
    )
    assert SECRET not in json.dumps(outcome.result)


def test_secret_is_cleared_after_its_call(runner, probe, key_file):
    secrets = resolve_secrets(probe)
    runner.run(probe, tool(probe, "probe_secret"), {"name": "probe_key"}, timeout=5, secrets=secrets)
    outcome = runner.run(probe, tool(probe, "probe_secret"), {"name": "probe_key"}, timeout=5)
    assert outcome.error_type == "tool_error" and "KeyError" in outcome.message


def test_scrub_replaces_values_at_any_depth_and_longest_first():
    value = {"a": ["x-abc-y", {"abc": "abcd"}], "n": 3}
    assert scrub(value, {"k": "abc", "j": "abcd"}) == {
        "a": ["x-[redacted]-y", {"[redacted]": "[redacted]"}],
        "n": 3,
    }
    assert scrub("text", {}) == "text"


def test_default_path_is_used_when_config_names_none(probe, hal_home, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config" / "hal").mkdir(parents=True)
    path = tmp_path / ".config" / "hal" / "probe-key"
    path.write_text(SECRET)
    path.chmod(0o600)
    assert resolve_secrets(probe) == {"probe_key": SECRET}


def test_missing_secret_names_the_secret_and_the_fix(probe, hal_home, tmp_path):
    hal_home.config.write_text(f'[probe]\nkey_file = "{tmp_path / "absent"}"\n')
    with pytest.raises(SecretError) as info:
        resolve_secrets(probe)
    assert (
        "probe_key" in str(info.value) and "0600" in str(info.value) and "probe.key_file" in str(info.value)
    )


def test_loosely_permissioned_secret_is_refused(probe, key_file):
    os.chmod(key_file, 0o644)
    with pytest.raises(SecretError, match="chmod 600"):
        resolve_secrets(probe)


def test_module_without_secrets_resolves_none(demo):
    assert resolve_secrets(dataclasses.replace(demo, secrets=())) == {}
