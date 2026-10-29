"""Tests for `halhub.scaffold` and `hal-hub new`: the generated pack loads, runs and passes its own test."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from halhub import cli, scaffold
from halhub.config import HubConfig
from halhub.hub import Hub
from halhub.manifest import load_manifest
from halhub.registry import Registry
from halhub.runner import Runner, RunnerConfig
from halkit import proc

PYTEST_TIMEOUT_S = 120  # one tiny test file; a cold interpreter start is the slow part


def test_generated_pack_passes_its_own_test(tmp_path):
    scaffold.create("weather", tmp_path, holder="Ada Example")
    root = tmp_path / "hal-weather"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=PYTEST_TIMEOUT_S,
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("exec_worker", [False, True])
def test_generated_manifest_is_a_valid_v2_manifest(tmp_path, exec_worker):
    scaffold.create("weather", tmp_path, exec_worker=exec_worker, holder="Ada Example")
    spec = load_manifest(tmp_path / "hal-weather/src/hal_weather/hal-module.toml", source="path")
    assert (spec.schema, spec.name, spec.tools[0].name, spec.tools[0].tier) == (
        2,
        "weather",
        "weather_hello",
        "read",
    )
    assert (spec.exec is not None) == exec_worker


def test_generated_files_are_ascii_and_have_no_todo(tmp_path):
    written = scaffold.create("weather", tmp_path, exec_worker=True, holder="Ada Example")
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert text.isascii() and "TODO" not in text, path


def test_exec_pack_runs_through_the_hub_without_importing_hal(tmp_path):
    scaffold.create("weather", tmp_path, exec_worker=True, holder="Ada Example")
    package = tmp_path / "hal-weather/src/hal_weather"
    worker = (package / "worker.py").read_text()
    assert "halhub" not in worker and "halkit" not in worker
    config = HubConfig(module_paths=(package,), use_entry_points=False)
    hub = Hub(Registry.load(config, version="0.1.0"), runner=Runner(RunnerConfig(bwrap="off")), config=config)
    try:
        session = hub.open_session(kind="hub", origin="cli")
        result = hub.call("weather_hello", {"name": "Ada"}, session, front_end="python")
    finally:
        hub.close()
    assert (result.status, result.result) == ("ok", "Hello, Ada.")


def test_python_pack_entry_point_calls_the_pack_main(tmp_path):
    scaffold.create("weather", tmp_path, holder="Ada Example")
    init = (tmp_path / "hal-weather/src/hal_weather/__init__.py").read_text()
    assert "pack.main(PACK, MODULES)" in init and 'MODULES = ("weather",)' in init
    pyproject = (tmp_path / "hal-weather/pyproject.toml").read_text()
    assert 'hal-weather = "hal_weather:main"' in pyproject and '"hal-hub>=0.1,<0.2"' in pyproject


def test_refuses_to_overwrite_existing_files(tmp_path):
    scaffold.create("weather", tmp_path, holder="Ada Example")
    tools = tmp_path / "hal-weather/src/hal_weather/tools.py"
    tools.write_text("# mine\n")
    with pytest.raises(FileExistsError, match="tools.py"):
        scaffold.create("weather", tmp_path, holder="Ada Example")
    assert tools.read_text() == "# mine\n"


@pytest.mark.parametrize("name", ["Weather", "my-pack", "my_pack", "9lives", ""])
def test_bad_name_is_refused(tmp_path, name):
    with pytest.raises(ValueError, match="pack name"):
        scaffold.create(name, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_licence_holder_comes_from_git_or_a_fallback(monkeypatch):
    monkeypatch.setattr(proc, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"Ada Example\n", b""))
    assert scaffold.licence_holder() == "Ada Example"
    monkeypatch.setattr(proc, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b""))
    assert scaffold.licence_holder() == "the authors"

    def missing(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(proc, "run", missing)
    assert scaffold.licence_holder() == "the authors"


def test_new_command_writes_the_pack_and_refuses_a_second_time(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(scaffold, "licence_holder", lambda: "Ada Example")
    assert cli.main(["new", "weather", "--dir", str(tmp_path)]) == 0
    assert "hal-weather/pyproject.toml" in capsys.readouterr().out
    assert cli.main(["new", "weather", "--dir", str(tmp_path)]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err
