"""Docker-marked `hal box` runs: a real box image, the config seed and the relay, no model call.

Needs `hal box image build` for the CLI under test; skipped when docker or the image is missing.
"""

from __future__ import annotations

import pytest

from halsandbox import boximages
from halsandbox.box import run
from halsandbox.code import docker_runtimes
from halsandbox.errors import SandboxError

pytestmark = pytest.mark.docker


def usable(cli: str) -> list[str]:
    """Runtimes to test `cli` under; empty when docker or its image is missing."""
    try:
        return [r for r in ("runc", "runsc") if r in docker_runtimes()] if boximages.newest(cli) else []
    except SandboxError:
        return []


class Forwarder:
    """A start_model stand-in: records the call and closes cleanly."""

    closed = False

    def close(self) -> None:
        """Marks the forwarder closed."""
        self.closed = True


@pytest.mark.parametrize("cli", ["claude", "qwen", "opencode", "codex", "crush", "goose"])
@pytest.mark.parametrize("runtime", ["runc", "runsc"])
def test_box_starts_the_cli_offline_in_an_isolated_copy(cli, runtime, tmp_path, monkeypatch, capfd):
    if runtime not in usable(cli):
        pytest.skip(f"no {cli} image or {runtime} runtime")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.txt").write_text("a\n")
    fwd = Forwarder()
    status = run(cli, ["--version"], project=project, runtime=runtime, start_model=lambda _s, _sid: fwd)
    assert status == 0 and fwd.closed
    assert (project / "a.txt").read_text() == "a\n"
    assert capfd.readouterr().out.strip()


def test_box_refuses_a_non_docker_backend(tmp_path):
    with pytest.raises(SandboxError, match="docker only"):
        run("crush", [], project=tmp_path, backend="bwrap", start_model=lambda _s, _i: Forwarder())
