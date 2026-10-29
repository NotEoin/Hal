"""Tests for `halhub.registry` and `halhub.config` (REQ-HUB-06, 07, 09 to 12, 63, 65)."""

from __future__ import annotations

import json
import sys
import textwrap
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from halhub.config import HubConfig
from halhub.discovery import ENTRY_POINT_GROUP
from halhub.registry import DuplicateModuleError, Registry, hub_version
from halkit.config import ConfigError


def module(
    directory: Path,
    name: str,
    *,
    pack: str | None = None,
    extra: str = "",
    tool: str = "ping",
    v1: bool = False,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    header = "" if v1 else "schema = 2\n"
    pack_line = f'pack = "{pack}"\n' if pack else ""
    tier = "" if v1 else 'tier = "read"\n'
    text = (
        f'{header}[module]\nname = "{name}"\nsummary = "s"\npython = "m.tools"\n{pack_line}{extra}'
        f'[[tools]]\nname = "{name}_{tool}"\ndescription = "d"\n{tier}'
    )
    path = directory / "hal-module.toml"
    path.write_text(text)
    return path


def config(*paths: Path, **kwargs) -> HubConfig:
    kwargs.setdefault("use_entry_points", False)
    return HubConfig(module_paths=tuple(paths), **kwargs)


def load(*paths: Path, **kwargs) -> Registry:
    collect = kwargs.pop("collect_errors", False)
    version = kwargs.pop("version", "0.1.0")
    entry_points = kwargs.pop("entry_points", None)
    return Registry.load(
        config(*paths, **kwargs), collect_errors=collect, version=version, entry_points=entry_points
    )


def record(path: Path, packs: dict) -> Path:
    path.write_text(json.dumps({"version": 2, "packs": packs}))
    return path


def listed(manifest: Path) -> dict:
    return {"manifest": str(manifest), "interpreter": [sys.executable]}


# ---- packs (REQ-HUB-63) ------------------------------------------------------------------------


def test_pack_name_not_matching_packs_json_is_excluded(tmp_path):
    manifest = module(tmp_path / "cal", "calendar", pack="life")
    packs_json = record(tmp_path / "packs.json", {"organiser": {"modules": {"calendar": listed(manifest)}}})
    registry = load(installed=packs_json)
    assert "calendar" not in registry.modules
    assert any("organiser" in p.message and "life" in p.message for p in registry.problems)


def test_pack_name_matching_packs_json_is_kept(tmp_path):
    manifest = module(tmp_path / "cal", "calendar", pack="organiser")
    packs_json = record(tmp_path / "packs.json", {"organiser": {"modules": {"calendar": listed(manifest)}}})
    registry = load(installed=packs_json)
    spec = registry.modules["calendar"].spec
    assert spec.pack == "organiser" and spec.tools[0].pack == "organiser"
    assert spec.interpreter == (sys.executable,)


def test_path_scanned_module_uses_module_name_as_pack(tmp_path):
    module(tmp_path / "cal", "calendar", pack="organiser")
    registry = load(tmp_path)
    spec = registry.modules["calendar"].spec
    assert spec.pack == "calendar" and registry.tool("calendar_ping").pack == "calendar"
    assert any("organiser" in p.message for p in registry.problems)


# ---- discovery through the registry ------------------------------------------------------------


def test_discovery_imports_no_pack_module(tmp_path, monkeypatch):
    site = tmp_path / "site"
    package = site / "halbomb"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("raise RuntimeError('pack code ran during discovery')\n")
    module(package, "bomb")
    monkeypatch.syspath_prepend(str(site))
    sys.modules.pop("halbomb", None)
    eps = [EntryPoint(name="bomb", value="halbomb", group=ENTRY_POINT_GROUP)]
    registry = load(use_entry_points=True, entry_points=lambda: eps)
    assert registry.modules["bomb"].spec.source == "entry_point"
    assert "halbomb" not in sys.modules


def test_installed_module_with_missing_manifest_is_a_problem(tmp_path):
    packs_json = record(
        tmp_path / "packs.json", {"life": {"modules": {"x": listed(tmp_path / "no" / "m.toml")}}}
    )
    registry = load(installed=packs_json)
    assert registry.modules == {} and len(registry.problems) == 1


# ---- duplicates, broken manifests, disabled (REQ-HUB-09, 10, 12) --------------------------------


def test_duplicate_module_names_both_paths(tmp_path):
    first = module(tmp_path / "a", "notes")
    second = module(tmp_path / "b", "notes")
    with pytest.raises(DuplicateModuleError) as info:
        load(tmp_path)
    assert {d[1] for d in info.value.duplicates} == {first, second}
    assert str(first) in str(info.value) and str(second) in str(info.value)


def test_duplicate_module_names_are_problems_when_collecting(tmp_path):
    module(tmp_path / "a", "notes")
    module(tmp_path / "b", "notes")
    module(tmp_path / "c", "other")
    registry = load(tmp_path, collect_errors=True)
    assert list(registry.modules) == ["other"]
    assert sum("duplicate" in p.message for p in registry.problems) == 2


def test_broken_manifest_excludes_only_its_module_and_prints_nothing(tmp_path, capsys, caplog):
    module(tmp_path / "good", "good")
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "hal-module.toml").write_text("schema = 2\n[module\n")
    registry = load(tmp_path)
    assert list(registry.modules) == ["good"]
    assert registry.problems[0].path == tmp_path / "bad" / "hal-module.toml"
    assert capsys.readouterr().out == ""
    assert any("malformed TOML" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


def test_disabled_module_is_excluded_but_listed(tmp_path):
    module(tmp_path / "a", "phone")
    registry = load(tmp_path, disabled=frozenset({"phone"}))
    assert registry.modules["phone"].status == "disabled"
    assert registry.tool("phone_ping") is None and registry.tools() == []


def test_legacy_hub_manifest_on_disk_is_skipped(tmp_path, caplog):
    hub = tmp_path / "core" / "hal-hub"
    hub.mkdir(parents=True)
    (hub / "hal-module.toml").write_text(
        '[module]\nname = "hub"\nsummary = "s"\npython = "halhub.tools"\n'
        '[[tools]]\nname = "hal_find_tools"\ndescription = "d"\n'
    )
    module(tmp_path / "ext" / "a", "notes")
    registry = load(tmp_path)
    assert list(registry.modules) == ["notes"] and registry.problems == ()
    assert any("v1 hub module" in r.getMessage() for r in caplog.records)


# ---- requires and versions (REQ-HUB-06, 11) ----------------------------------------------------


def test_requires_hal_outside_the_hub_version_is_rejected_naming_both(tmp_path):
    module(tmp_path / "a", "future", extra='requires_hal = ">=0.2,<0.3"\n')
    registry = load(tmp_path, version="0.1.0")
    assert "future" not in registry.modules
    message = registry.problems[0].message
    assert ">=0.2,<0.3" in message and "0.1.0" in message


def test_requires_hal_inside_the_hub_version_loads(tmp_path):
    module(tmp_path / "a", "now", extra='requires_hal = ">=0.1,<0.2"\n')
    assert load(tmp_path, version="0.1.0.dev0").modules["now"].status == "ok"


def test_unmet_requirement_hides_tools_with_reason(tmp_path):
    module(tmp_path / "a", "brief", extra='requires = ["weather"]\n')
    registry = load(tmp_path)
    state = registry.modules["brief"]
    assert state.status == "unavailable" and "weather" in state.reason
    assert registry.tool("brief_ping") is None and registry.tools() == []
    tool, found = registry.lookup("brief_ping")
    assert tool.name == "brief_ping" and found.reason == state.reason


def test_requirement_version_is_checked(tmp_path):
    module(tmp_path / "a", "brief", extra='requires = ["notify>=0.2"]\n')
    module(tmp_path / "b", "notify", extra='version = "0.1.0"\n')
    state = load(tmp_path).modules["brief"]
    assert state.status == "unavailable" and "found notify 0.1.0" in state.reason


def test_requirement_on_disabled_module_is_unmet(tmp_path):
    module(tmp_path / "a", "brief", extra='requires = ["notify"]\n')
    module(tmp_path / "b", "notify")
    state = load(tmp_path, disabled=frozenset({"notify"})).modules["brief"]
    assert state.status == "unavailable" and "disabled" in state.reason


def test_met_requirements_chain_loads(tmp_path):
    module(tmp_path / "a", "brief", extra='requires = ["notify>=0.1"]\n')
    module(tmp_path / "b", "notify", extra='version = "0.1.0"\nrequires = ["base"]\n')
    module(tmp_path / "c", "base")
    assert {s.status for s in load(tmp_path).modules.values()} == {"ok"}


def test_unavailable_requirement_propagates(tmp_path):
    module(tmp_path / "a", "top", extra='requires = ["mid"]\n')
    module(tmp_path / "b", "mid", extra='requires = ["gone"]\n')
    states = load(tmp_path).modules
    assert states["mid"].status == "unavailable" and states["top"].status == "unavailable"
    assert "unavailable" in states["top"].reason


def test_requires_cycle_marks_both_unavailable(tmp_path):
    module(tmp_path / "a", "alpha", extra='requires = ["beta"]\n')
    module(tmp_path / "b", "beta", extra='requires = ["alpha"]\n')
    module(tmp_path / "c", "gamma", extra='requires = ["alpha"]\n')
    states = load(tmp_path).modules
    assert states["alpha"].status == states["beta"].status == "unavailable"
    assert "cycle" in states["alpha"].reason and "cycle" in states["beta"].reason
    assert states["gamma"].status == "unavailable"


# ---- packs() -----------------------------------------------------------------------------------


def test_packs_lists_roots_and_interpreter(tmp_path):
    cal = module(tmp_path / "env" / "cal", "calendar", pack="organiser")
    tasks = module(tmp_path / "env" / "tasks", "tasks", pack="organiser")
    module(tmp_path / "dev" / "rgb", "rgb")
    packs_json = record(
        tmp_path / "packs.json",
        {"organiser": {"modules": {"calendar": listed(cal), "tasks": listed(tasks)}}},
    )
    registry = load(tmp_path / "dev", installed=packs_json, disabled=frozenset({"rgb"}))
    organiser, rgb = registry.packs()
    assert organiser.name == "organiser" and organiser.modules == ("calendar", "tasks")
    assert organiser.roots == (cal.parent, tasks.parent)
    assert organiser.interpreter == (sys.executable,) and organiser.enabled
    assert (rgb.name, rgb.interpreter, rgb.enabled) == ("rgb", None, False)


def test_tools_are_sorted_by_name(tmp_path):
    module(tmp_path / "b", "zeta")
    module(tmp_path / "a", "alpha")
    registry = load(tmp_path)
    assert [t.name for t in registry.tools()] == ["alpha_ping", "zeta_ping"]
    assert registry.module_of(registry.tool("zeta_ping")).name == "zeta"


def test_hub_version_is_the_package_version():
    import halhub

    assert hub_version() == halhub.__version__


# ---- HubConfig ---------------------------------------------------------------------------------


def test_hub_config_reads_the_hub_table_and_module_path(hal_home, tmp_path):
    hal_home.config.write_text(
        textwrap.dedent(
            """
            [hub]
            module_paths = ["~/ext"]
            disabled = ["phone"]
            pinned = ["memory_remember"]
            [hub.runner]
            max_workers_per_module = 2
            """
        )
    )
    cfg = HubConfig.load(environ={"HAL_MODULE_PATH": str(tmp_path)})
    assert cfg.module_paths == (Path.home() / "ext", tmp_path)
    assert cfg.disabled == frozenset({"phone"}) and cfg.pinned == ("memory_remember",)
    assert cfg.installed == hal_home.state / "packs.json"


def test_unknown_hub_key_is_a_config_error_naming_it(hal_home):
    hal_home.config.write_text("[hub]\nmodule_path = []\n")
    with pytest.raises(ConfigError) as info:
        HubConfig.load(environ={})
    assert info.value.key == "module_path"


def test_malformed_config_is_a_config_error_with_line(hal_home):
    hal_home.config.write_text("[hub]\nmodule_paths = = []\n")
    with pytest.raises(ConfigError) as info:
        HubConfig.load(environ={})
    assert info.value.line is not None
