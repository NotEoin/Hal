"""Tests for `hal-sandbox doctor`, `ls`, `gc` and `forget` (step 32), with docker faked."""

from __future__ import annotations

import json
import os
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


def test_doctor_warns_when_the_agent_code_is_newer_than_the_image(monkeypatch, tmp_path):
    """Regression: agent fixes never reached the box; the image held the old wheels and doctor said ok."""
    source = tmp_path / "packages" / "hal-agent" / "src" / "halagent" / "loop.py"
    source.parent.mkdir(parents=True)
    source.write_text("x = 1\n")
    os.utime(source, (2_000_000_000, 2_000_000_000))  # 2033-05-18
    monkeypatch.setattr(images, "find_repo_root", lambda: tmp_path)
    newer = images.newer_sources("2026-11-03T01:00:00Z")
    assert newer == [source]
    check = doctor.freshness_check([{"tag": "hal-sandbox:full-abc", "built": "2026-11-03T01:00:00Z"}])
    assert check.status == doctor.WARN and "hal box image build" in check.detail and "loop.py" in check.detail


def test_doctor_says_nothing_about_freshness_for_an_installed_hal(monkeypatch):
    monkeypatch.setattr(images, "find_repo_root", lambda: (_ for _ in ()).throw(SandboxError("no checkout")))
    assert images.newer_sources("2026-11-03T01:00:00Z") == []
    assert doctor.freshness_check([{"tag": "t", "built": "2026-11-03T01:00:00Z"}]) is None
