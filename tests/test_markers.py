"""The pytest markers are declared and deselected by default (REQ-SYS-21)."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MARKERS = ("gpu", "docker", "network", "slow", "bwrap", "systemd", "live")  # resolution E12
PYTEST = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]


def test_all_markers_are_declared_in_the_root_pyproject():
    declared = {line.split(":", 1)[0] for line in PYTEST["markers"]}
    assert declared == set(MARKERS)


def test_every_marker_is_deselected_by_default():
    expression = PYTEST["addopts"].split('-m "', 1)[1].rstrip('"')
    assert set(expression.split(" and ")) == {f"not {m}" for m in MARKERS}


def test_markers_are_strict_so_a_typo_fails_collection():
    assert "--strict-markers" in PYTEST["addopts"]
