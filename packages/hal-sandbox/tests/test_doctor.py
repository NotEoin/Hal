"""Tests for `hal-sandbox doctor`, `ls`, `gc` and `forget` (step 32), with docker faked."""

from __future__ import annotations

import json
import subprocess

import pytest

from halsandbox import cli, detect, doctor, images, labels
from halsandbox.errors import SandboxError


def _probe(runtime: str | None, ok: bool = True) -> detect.Probe:
    return detect.Probe("docker", ok, runtime, None, "", "2026-10-30T00:00:00Z")


@pytest.fixture
def fake_images(monkeypatch):
    rows = [{"tag": "hal-sandbox:full-abc", "id": "sha256:" + "0" * 12, "size": 900 * 10**6, "built": "x"}]
    monkeypatch.setattr(images, "status", lambda: rows)
    monkeypatch.setattr(doctor.boximages, "harnesses", lambda: [{"name": "crush", "built": True}])


def test_doctor_warns_when_runc_is_used_under_standard(monkeypatch, fake_images):
    monkeypatch.setattr(detect, "detect", lambda *a, **k: _probe("runc"))
    by_name = {c.name: c for c in doctor.run_checks()}
    assert by_name["runtime"].status == doctor.WARN
    assert "host-uds=open" in by_name["runtime"].detail


def test_doctor_fails_when_no_backend_fits(monkeypatch, fake_images):
    def boom(*a, **k):
        raise SandboxError("docker is not running")

    monkeypatch.setattr(detect, "detect", boom)
    by_name = {c.name: c for c in doctor.run_checks()}
    assert by_name["backend"].status == doctor.FAIL


def test_doctor_flags_an_image_over_its_size_budget(monkeypatch, fake_images):
    monkeypatch.setattr(detect, "detect", lambda *a, **k: _probe("runsc"))
    by_name = {c.name: c for c in doctor.run_checks()}
    assert by_name["image hal-sandbox:full-abc"].status == doctor.WARN


def test_doctor_cli_exit_code_is_nonzero_on_a_failed_check(monkeypatch, fake_images, capsys):
    monkeypatch.setattr(detect, "detect", lambda *a, **k: _probe(None, ok=False))
    assert cli.sandbox_main(["doctor", "--json"]) == 1
    assert any(c["status"] == "fail" for c in json.loads(capsys.readouterr().out))


def test_ls_parses_labelled_containers(monkeypatch):
    out = b"hal-box-1\tbox\tabc123\tUp 2 minutes\nbroken line\n"
    monkeypatch.setattr(
        doctor.proc, "run", lambda argv, timeout: subprocess.CompletedProcess(argv, 0, out, b"")
    )
    assert doctor.list_boxes() == [
        {"name": "hal-box-1", "kind": "box", "session": "abc123", "status": "Up 2 minutes"}
    ]


def test_gc_dry_run_reports_what_it_would_remove(monkeypatch, capsys):
    monkeypatch.setattr(labels, "sweep", lambda **k: labels.SweepResult(removed=("a", "b")))
    assert cli.sandbox_main(["gc", "--dry-run"]) == 0
    assert "would remove 2 container" in capsys.readouterr().out


def test_forget_reports_nothing_for_an_unknown_project(tmp_path, capsys):
    assert cli.sandbox_main(["forget", str(tmp_path)]) == 0
    assert "nothing to forget" in capsys.readouterr().out
