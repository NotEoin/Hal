"""Tests for the `[sandbox]` table and lockdown levels."""

import pytest

from halkit import config
from halsandbox import config as sbconfig
from halsandbox.errors import LockdownError
from halsandbox.lockdown import Policy


def test_lockdown_defaults_to_standard(tmp_path):
    assert sbconfig.load(tmp_path / "none.toml", environ={})["lockdown"] == "standard"


def test_lockdown_cannot_be_set_from_the_environment(tmp_path):
    cfg = sbconfig.load(tmp_path / "none.toml", environ={"HAL_SANDBOX_LOCKDOWN": "off"})
    assert cfg["lockdown"] == "standard"


def test_unknown_lockdown_level_is_rejected_by_config(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[sandbox]\nlockdown = "loose"\n')
    with pytest.raises(config.ConfigError):
        sbconfig.load(f, environ={})


def test_strict_refuses_to_start_without_runsc():
    with pytest.raises(LockdownError, match="runsc"):
        Policy.for_level("strict").choose_runtime({"runc"})


def test_strict_refuses_an_explicit_runc():
    with pytest.raises(LockdownError, match="runc"):
        Policy.for_level("strict").choose_runtime({"runc", "runsc"}, "runc")


def test_standard_prefers_runsc_and_warns_on_runc():
    p = Policy.for_level("standard")
    assert p.choose_runtime({"runc", "runsc"}) == ("runsc", [])
    runtime, warnings = p.choose_runtime({"runc"})
    assert runtime == "runc" and "runsc" in warnings[0]


def test_unregistered_requested_runtime_is_an_error():
    with pytest.raises(LockdownError):
        Policy.for_level("standard").choose_runtime({"runc"}, "runsc")


@pytest.mark.parametrize(
    "level,allowed", [("strict", False), ("standard", False), ("relaxed", True), ("off", True)]
)
def test_direct_mode_needs_relaxed_or_lower(level, allowed):
    p = Policy.for_level(level)
    if allowed:
        p.check_direct()
    else:
        with pytest.raises(LockdownError, match="relaxed"):
            p.check_direct()


def test_only_off_allows_backend_none_and_only_strict_limits_upstreams():
    flags = {lv: Policy.for_level(lv) for lv in ("strict", "standard", "relaxed", "off")}
    assert [lv for lv, p in flags.items() if p.allow_backend_none] == ["off"]
    assert [lv for lv, p in flags.items() if p.hub_only_upstreams] == ["strict"]
    assert flags["strict"].paranoia_floor == "high"


def test_unknown_level_raises():
    with pytest.raises(LockdownError):
        Policy.for_level("loose")
