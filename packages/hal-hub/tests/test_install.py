"""Tests for `halhub.install` and `hal-hub setup` (REQ-HUB-59).

Steps run through a fake `halkit.proc.run`, so no command runs; `confirm` is scripted. The
module is written into a temporary directory per test.
"""

from __future__ import annotations

import json
import subprocess
import textwrap

import pytest

from halhub import cli, install
from halhub.manifest import load_manifest
from halkit import proc


@pytest.fixture
def module(tmp_path):
    """A v2 module `kit` with two install steps: `fetch` (in its root) and `index` (in its state dir)."""
    root = tmp_path / "kit"
    root.mkdir()
    (root / "hal-module.toml").write_text(
        textwrap.dedent(
            """
            schema = 2
            [module]
            name = "kit"
            summary = "A module with install steps"
            python = "halkit_fixture.tools"

            [[install]]
            id = "fetch"
            summary = "download the model (about 1 MB)"
            run = ["{python}", "-c", "print('fetch')"]
            timeout = 30
            network = true

            [[install]]
            id = "index"
            summary = "build the index"
            run = ["{python}", "-c", "print('index')"]
            cwd = "state"
            """
        )
    )
    return load_manifest(root / "hal-module.toml", source="path")


class FakeRun:
    """Stands in for `halkit.proc.run`: records each call; `codes` gives each step's exit status."""

    def __init__(self, codes=None, *, timeout_on: str | None = None) -> None:
        self.codes = dict(codes or {})
        self.timeout_on = timeout_on
        self.calls: list[tuple[list[str], float, object]] = []

    def __call__(self, argv, *, timeout, cwd=None, check=True, **_):
        self.calls.append((list(argv), timeout, cwd))
        step = argv[-1].split("'")[1]
        if step == self.timeout_on:
            raise proc.ProcTimeout(argv, timeout)
        return subprocess.CompletedProcess(argv, self.codes.get(step, 0), b"", b"boom")


@pytest.fixture
def fake_run(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(proc, "run", fake)
    return fake


def test_each_step_runs_after_confirmation_with_its_timeout_and_cwd(module, fake_run):
    asked = []
    states = install.run_steps(module, confirm=lambda step: asked.append(step.id) or True)
    assert asked == ["fetch", "index"] and [s.status for s in states] == ["ok", "ok"]
    (_, t1, cwd1), (_, t2, cwd2) = fake_run.calls
    assert (t1, cwd1) == (30.0, module.root) and (t2, cwd2) == (600.0, module.state_dir())
    assert all(s.at.endswith("Z") for s in states)


def test_succeeded_step_with_same_digest_is_skipped(module, fake_run):
    install.run_steps(module, confirm=lambda step: True)
    asked = []
    assert install.run_steps(module, confirm=lambda step: asked.append(step.id) or True) == []
    assert asked == [] and len(fake_run.calls) == 2
    assert [prior.status for _, prior in install.plan(module)] == ["ok", "ok"]


def test_changed_step_runs_again(module, fake_run, tmp_path):
    install.run_steps(module, confirm=lambda step: True)
    manifest = module.manifest
    manifest.write_text(manifest.read_text().replace("timeout = 30", "timeout = 45"))
    changed = load_manifest(manifest, source="path")
    states = install.run_steps(changed, confirm=lambda step: True)
    assert [s.step for s in states] == ["fetch"]


def test_force_runs_succeeded_steps_again(module, fake_run):
    install.run_steps(module, confirm=lambda step: True)
    assert len(install.run_steps(module, confirm=lambda step: True, force=True)) == 2


def test_declined_step_stops_the_run(module, fake_run):
    assert install.run_steps(module, confirm=lambda step: False) == []
    assert fake_run.calls == []


def test_first_failure_stops_the_run(module, monkeypatch):
    fake = FakeRun({"fetch": 3})
    monkeypatch.setattr(proc, "run", fake)
    states = install.run_steps(module, confirm=lambda step: True)
    assert [(s.step, s.status, s.exit_code) for s in states] == [("fetch", "failed", 3)]
    assert len(fake.calls) == 1


def test_timeout_is_a_failure_without_exit_code(module, monkeypatch):
    monkeypatch.setattr(proc, "run", FakeRun(timeout_on="fetch"))
    (state,) = install.run_steps(module, confirm=lambda step: True)
    assert (state.status, state.exit_code) == ("failed", None)


def test_state_is_written_atomically_to_the_state_dir(module, fake_run, hal_home):
    install.run_steps(module, confirm=lambda step: True)
    doc = json.loads(install.state_file().read_text())
    assert install.state_file().parent == hal_home.state / "hub"
    assert set(doc["steps"]) == {"kit/fetch", "kit/index"}


# ---- hal-hub setup -----------------------------------------------------------------------------


@pytest.fixture
def home(hal_home, module):
    hal_home.config.write_text(f'[hub]\nmodule_paths = ["{module.root}"]\nuse_entry_points = false\n')
    return hal_home


def test_setup_refuses_without_terminal(home, fake_run, capsys):
    assert cli.main(["setup", "kit"]) == 2
    assert "terminal" in capsys.readouterr().err and fake_run.calls == []


def test_setup_dry_run_prints_the_plan_anywhere(home, fake_run, capsys):
    assert cli.main(["setup", "kit", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "kit/fetch [to run, downloads, up to 30 s]" in out and fake_run.calls == []


def test_setup_has_no_yes_flag(home, capsys):
    assert cli.main(["setup", "kit", "--yes"]) == 2


def test_setup_runs_each_step_after_a_typed_y(home, fake_run, capsys, monkeypatch):
    import io

    monkeypatch.setattr(cli, "person_at_terminal", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO("y\nn\n"))
    assert cli.main(["setup", "kit"]) == 3
    assert len(fake_run.calls) == 1
    assert "stopped before index" in capsys.readouterr().err


def test_setup_of_unknown_module_exits_2(home, capsys, monkeypatch):
    monkeypatch.setattr(cli, "person_at_terminal", lambda: True)
    assert cli.main(["setup", "nope"]) == 2


def test_doctor_notes_install_steps_not_yet_run(home, capsys):
    cli.main(["doctor", "--json"])
    kit = next(m for m in json.loads(capsys.readouterr().out)["modules"] if m["module"] == "kit")
    assert any("install step fetch has not run" in note for note in kit["notes"])
