"""Tests for `halhub.discovery`: entry points without imports, scanning rules (REQ-HUB-07, 08, 65)."""

from __future__ import annotations

import json
import sys
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from halhub.discovery import ENTRY_POINT_GROUP, Candidate, discover, module_search_paths
from halhub.manifest import load_manifest

MANIFEST = '[module]\nname = "x"\nsummary = "s"\n'


def module_at(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "hal-module.toml"
    path.write_text(MANIFEST)
    return path


def entry(name: str, value: str) -> EntryPoint:
    return EntryPoint(name=name, value=value, group=ENTRY_POINT_GROUP)


@pytest.fixture
def bomb_package(tmp_path, monkeypatch) -> Path:
    """A package whose `__init__.py` raises, with a manifest in a subpackage directory."""
    root = tmp_path / "site"
    package = root / "halbomb"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("raise RuntimeError('pack code ran during discovery')\n")
    module_at(package / "calendar")
    monkeypatch.syspath_prepend(str(root))
    sys.modules.pop("halbomb", None)
    return package


def test_recursive_scan_skips_venv_and_hidden_dirs(tmp_path):
    wanted = module_at(tmp_path / "extensions" / "hal-cal")
    for skipped in (".git", ".venv", "venv", "node_modules", "build", "dist", "site-packages", "__pycache__"):
        module_at(tmp_path / skipped / "inner")
    assert [c.manifest for c in discover(paths=[tmp_path], use_entry_points=False)] == [wanted]


def test_scan_reaches_depth_four_and_no_further(tmp_path):
    deep = module_at(tmp_path / "a" / "b" / "c" / "d")
    module_at(tmp_path / "a" / "b" / "c" / "d2" / "e")
    assert [c.manifest for c in discover(paths=[tmp_path], use_entry_points=False)] == [deep]


def test_module_directory_is_not_searched_further(tmp_path):
    outer = module_at(tmp_path / "pack")
    module_at(tmp_path / "pack" / "vendored")
    assert [c.manifest for c in discover(paths=[tmp_path], use_entry_points=False)] == [outer]


def test_symlinked_directory_is_not_followed(tmp_path):
    real = module_at(tmp_path / "real" / "mod")
    (tmp_path / "scan").mkdir()
    (tmp_path / "scan" / "link").symlink_to(tmp_path / "real")
    assert discover(paths=[tmp_path / "scan"], use_entry_points=False) == []
    assert [c.manifest for c in discover(paths=[tmp_path / "real"], use_entry_points=False)] == [real]


def test_ignore_paths_are_skipped(tmp_path):
    keep = module_at(tmp_path / "keep")
    module_at(tmp_path / "drop")
    found = discover(paths=[tmp_path], use_entry_points=False, ignore=[tmp_path / "drop"])
    assert [c.manifest for c in found] == [keep]


def test_missing_module_path_is_skipped_quietly(tmp_path):
    problems = []
    assert discover(paths=[tmp_path / "nope"], use_entry_points=False, problems=problems) == []
    assert problems == []


def test_hal_module_path_adds_to_config_paths(tmp_path):
    environ = {"HAL_MODULE_PATH": f"{tmp_path / 'b'}:{tmp_path / 'c'}"}
    assert module_search_paths(["~/a"], environ) == [Path.home() / "a", tmp_path / "b", tmp_path / "c"]


def test_manifest_reached_twice_counts_once(tmp_path):
    module_at(tmp_path / "mod")
    found = discover(paths=[tmp_path, tmp_path / "mod"], use_entry_points=False)
    assert len(found) == 1 and found[0].source == "path"


def test_entry_point_package_is_located_without_import(bomb_package):
    found = discover(paths=[], entry_points=lambda: [entry("calendar", "halbomb.calendar")])
    assert [c.manifest for c in found] == [bomb_package / "calendar" / "hal-module.toml"]
    assert found[0].source == "entry_point" and found[0].entry_point == "calendar"
    assert "halbomb" not in sys.modules


def test_entry_point_top_level_value_searches_two_levels_down(bomb_package):
    found = discover(paths=[], entry_points=lambda: [entry("bomb", "halbomb")])
    assert [c.manifest for c in found] == [bomb_package / "calendar" / "hal-module.toml"]


def test_entry_point_with_attribute_is_a_problem(bomb_package):
    problems = []
    found = discover(paths=[], entry_points=lambda: [entry("cal", "halbomb:main")], problems=problems)
    assert found == [] and "module:attribute" in problems[0].message


def test_missing_entry_point_package_is_a_problem_for_that_entry_only(bomb_package):
    problems = []
    eps = [entry("gone", "halnotinstalled"), entry("calendar", "halbomb.calendar")]
    found = discover(paths=[], entry_points=lambda: eps, problems=problems)
    assert len(found) == 1 and len(problems) == 1 and "not installed" in problems[0].message


def test_entry_point_wins_over_the_same_manifest_on_a_path(bomb_package):
    eps = [entry("calendar", "halbomb.calendar")]
    found = discover(paths=[bomb_package], entry_points=lambda: eps)
    assert [c.source for c in found] == ["entry_point"]


def test_entry_points_are_skipped_when_disabled(bomb_package):
    def explode():
        raise AssertionError("entry points read")

    assert discover(paths=[], use_entry_points=False, entry_points=explode) == []


# ---- installed-pack record (packs.json) ---------------------------------------------------------


def write_record(path: Path, packs: dict, version: int = 2) -> Path:
    path.write_text(json.dumps({"version": version, "packs": packs}))
    return path


def record_entry(manifest: Path, interpreter: str = sys.executable) -> dict:
    return {"manifest": str(manifest), "interpreter": [interpreter]}


def test_installed_pack_record_sets_interpreter(tmp_path, hal_home):
    manifest = module_at(tmp_path / "env" / "halcal")
    record = write_record(
        hal_home.state / "packs.json",
        {"organiser": {"distribution": "hal-organiser", "modules": {"calendar": record_entry(manifest)}}},
    )
    found = discover(paths=[], use_entry_points=False, installed=record)
    assert found == [
        Candidate(
            manifest=manifest,
            source="installed",
            distribution="hal-organiser",
            interpreter=(sys.executable,),
            pack="organiser",
        )
    ]
    spec = load_manifest(found[0].manifest, source="installed", interpreter=found[0].interpreter)
    assert spec.interpreter == (sys.executable,)


def test_missing_record_file_means_no_installed_packs(tmp_path):
    assert discover(paths=[], use_entry_points=False, installed=tmp_path / "packs.json") == []


def test_listed_manifest_that_is_missing_is_a_problem_not_a_crash(tmp_path):
    good = module_at(tmp_path / "good")
    record = write_record(
        tmp_path / "packs.json",
        {
            "life": {
                "modules": {
                    "gone": record_entry(tmp_path / "gone" / "hal-module.toml"),
                    "good": record_entry(good),
                }
            }
        },
    )
    problems = []
    found = discover(paths=[], use_entry_points=False, installed=record, problems=problems)
    assert [c.manifest for c in found] == [good]
    assert len(problems) == 1 and "missing" in problems[0].message


def test_listed_interpreter_that_is_missing_is_a_problem(tmp_path):
    manifest = module_at(tmp_path / "mod")
    record = write_record(
        tmp_path / "packs.json", {"life": {"modules": {"m": record_entry(manifest, "/nope/python")}}}
    )
    problems = []
    assert discover(paths=[], use_entry_points=False, installed=record, problems=problems) == []
    assert "interpreter" in problems[0].message


@pytest.mark.parametrize(
    "text",
    ["{not json", json.dumps({"version": 1, "packs": {}}), json.dumps({"version": 2}), json.dumps([1])],
)
def test_broken_record_is_one_problem(tmp_path, text):
    record = tmp_path / "packs.json"
    record.write_text(text)
    problems = []
    assert discover(paths=[], use_entry_points=False, installed=record, problems=problems) == []
    assert len(problems) == 1 and problems[0].path == record


def test_record_without_modules_table_is_a_problem_for_that_pack(tmp_path):
    record = write_record(tmp_path / "packs.json", {"life": {"env": "shared"}})
    problems = []
    discover(paths=[], use_entry_points=False, installed=record, problems=problems)
    assert "packs.life.modules" in problems[0].message


def test_record_keeps_its_pack_name_for_a_manifest_also_found_by_entry_point(bomb_package, tmp_path):
    manifest = bomb_package / "calendar" / "hal-module.toml"
    record = write_record(
        tmp_path / "packs.json", {"organiser": {"modules": {"calendar": record_entry(manifest)}}}
    )
    found = discover(paths=[], entry_points=lambda: [entry("calendar", "halbomb.calendar")], installed=record)
    assert len(found) == 1
    assert (found[0].source, found[0].pack, found[0].interpreter) == ("entry_point", "organiser", None)


def test_record_comes_before_scanned_paths(tmp_path):
    manifest = module_at(tmp_path / "mod")
    record = write_record(tmp_path / "packs.json", {"life": {"modules": {"m": record_entry(manifest)}}})
    found = discover(paths=[tmp_path], use_entry_points=False, installed=record)
    assert [c.source for c in found] == ["installed"]
