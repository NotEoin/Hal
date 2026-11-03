"""Unit tests for `hal box`, image resolution and the `hal-sandbox` command line (no real docker)."""

import json

import pytest

from halsandbox import box, boximages, cli, images
from halsandbox.errors import SandboxError


def fake_docker(tmp_path, body: str) -> str:
    path = tmp_path / "docker"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


def test_box_requires_docker(tmp_path):
    with pytest.raises(SandboxError, match="docker only"):
        box.run("claude", [], project=tmp_path, backend="bwrap", start_model=lambda *_: None)


def test_unknown_cli_is_refused_before_any_session(tmp_path):
    with pytest.raises(SandboxError, match="unsupported agent CLI"):
        box.run("emacs", [], project=tmp_path, start_model=lambda *_: None)


def test_missing_image_error_names_build_command(tmp_path):
    with pytest.raises(SandboxError, match="hal box image build"):
        images.resolve("hal-sandbox:none", docker=fake_docker(tmp_path, "exit 1\n"))


def test_image_without_the_context_label_is_refused(tmp_path):
    with pytest.raises(SandboxError, match="not built FROM a hal-sandbox image"):
        images.resolve("custom:1", docker=fake_docker(tmp_path, "echo sha256:abc\n"))


def test_image_is_resolved_to_its_id(tmp_path):
    assert images.resolve("x", docker=fake_docker(tmp_path, "echo sha256:abc ctx\n")) == "sha256:abc"


def test_box_without_a_built_image_names_the_build_command(tmp_path, monkeypatch):
    monkeypatch.setattr(boximages, "newest", lambda cli, **_: None)
    with pytest.raises(SandboxError, match="hal box image build crush"):
        box.run("crush", [], project=tmp_path, start_model=lambda *_: None)


def test_harnesses_json_is_ascii_and_lists_every_cli(monkeypatch, capsys):
    monkeypatch.setattr(boximages, "newest", lambda cli, **_: None)
    assert cli.sandbox_main(["harnesses", "--json"]) == 0
    out = capsys.readouterr().out
    assert out.isascii() and [r["name"] for r in json.loads(out)] == list(boximages.CLIS)


def test_box_strings_are_stripped_before_tty(capsys):
    assert "\x1b" not in cli._out("a\x1b]52;c;ZXZpbA==\x07b")


def test_box_main_without_a_cli_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.box_main(["--dir", "."])
    assert exc.value.code == 2


@pytest.mark.parametrize("command", ["image", "doctor", "ls", "gc", "forget", "deps", "harnesses"])
def test_every_documented_sandbox_command_works_through_hal_box(monkeypatch, command):
    """Regression: the docs said `hal-sandbox deps`, which is not on PATH, and `hal box deps` ran a CLI."""
    seen = []
    monkeypatch.setattr(cli, "sandbox_main", lambda argv: seen.append(argv) or 0)
    assert cli.box_main([command]) == 0
    assert seen and seen[0][0] == command
