"""Tests for the gateway systemd unit template."""

import pytest

from halgateway.config import load_config
from halgateway.server import Gateway
from halgateway.units import BIN_PLACEHOLDER, render_unit, unit_template


def test_template_keeps_the_placeholder_and_runs_serve():
    text = unit_template()
    assert f"ExecStart={BIN_PLACEHOLDER}/hal-gateway serve" in text


def test_render_replaces_every_placeholder():
    text = render_unit("/opt/hal/bin/")
    assert "ExecStart=/opt/hal/bin/hal-gateway serve" in text
    assert BIN_PLACEHOLDER not in text


def test_unit_is_not_stopped_by_hal_off_and_kills_the_router_with_it():
    text = unit_template()
    assert "X-HalGPU" not in text
    assert "KillMode=control-group" in text


def test_unit_stop_timeout_covers_the_default_router_stop_timeout(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[gateway.backends.local]\ntype = "llama-router"\nmanaged = false\nurl = "http://127.0.0.1:9"\n'
    )
    stop = float(Gateway(load_config(cfg)).router_entry()["stop_timeout_s"])
    timeout = next(
        int(line.split("=")[1]) for line in unit_template().splitlines() if line.startswith("TimeoutStopSec")
    )
    assert timeout > stop


@pytest.mark.parametrize("bad", ["", "/opt/my bin"])
def test_render_refuses_an_empty_or_spaced_bin_dir(bad):
    with pytest.raises(ValueError, match="bin_dir"):
        render_unit(bad)
