"""Tests for box image rendering, pins and lockfiles (no docker)."""

import json
from datetime import date

import pytest

from halsandbox import boximages, images
from halsandbox.errors import SandboxError

BASE = "sha256:" + "a" * 64


def test_every_cli_has_a_pin_at_least_14_days_old():
    for cli in boximages.CLIS:
        boximages.check_cli_pin(images.load_pins(), cli, today=date(2026, 10, 30))


def test_a_cli_pin_younger_than_14_days_is_refused():
    with pytest.raises(SandboxError, match="younger"):
        boximages.check_cli_pin(images.load_pins(), "claude", today=date(2026, 10, 10))


def test_an_empty_goose_checksum_is_refused():
    p = images.load_pins()
    p["cli"]["goose"]["sha256"] = ""
    with pytest.raises(SandboxError, match="sha256"):
        boximages.check_cli_pin(p, "goose", today=date(2026, 10, 30))


def test_unknown_cli_is_refused():
    with pytest.raises(SandboxError, match="unknown agent CLI"):
        boximages.check_cli_pin(images.load_pins(), "emacs")


def test_npm_cli_installs_from_its_lockfile_with_node():
    text = boximages.render_box_dockerfile(images.load_pins(), "claude", base_id=BASE)
    assert f"FROM {BASE}" in text and "AS node" in text and "npm ci --omit=dev" in text
    assert "${CLI}" not in text and "${INSTALL}" not in text


def test_goose_installs_a_checksummed_tarball_without_node():
    text = boximages.render_box_dockerfile(images.load_pins(), "goose", base_id=BASE)
    assert "--checksum=sha256:ab396c95" in text and "AS node" not in text and "npm" not in text


def test_every_npm_lockfile_pins_its_package_with_integrity_hashes():
    for cli in boximages.CLIS:
        if cli == "goose":
            continue
        pin = images.load_pins()["cli"][cli]
        lock = json.loads((images.IMAGES_DIR / "box" / cli / "package-lock.json").read_text())
        assert lock["packages"]["node_modules/" + pin["package"]]["version"] == pin["version"]
        assert all("integrity" in v for k, v in lock["packages"].items() if k and not v.get("link"))


def test_box_tag_changes_with_the_base_image(tmp_path):
    ctx = boximages.stage_box_context(tmp_path, "crush")
    assert boximages.box_tag("crush", BASE, ctx) != boximages.box_tag("crush", "sha256:" + "b" * 64, ctx)
    assert boximages.box_tag("crush", BASE, ctx).startswith("hal-box-crush:")


def test_box_tag_changes_with_the_dockerfile(tmp_path):
    ctx = boximages.stage_box_context(tmp_path, "goose")
    assert boximages.box_tag("goose", BASE, ctx, "a") != boximages.box_tag("goose", BASE, ctx, "b")
