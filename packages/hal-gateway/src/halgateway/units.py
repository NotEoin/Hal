"""The systemd unit template for the gateway; this package never installs or starts it.

The template carries one placeholder, `@BIN@`, the `bin/` directory of the environment Hal
runs in. The launcher (design 06) renders and installs it. The unit has no `X-HalGPU` mark on
purpose: `hal off` frees the GPU through the admin route and the off flag, so every client of
the endpoint keeps working while the GPU is free.
"""

from __future__ import annotations

from importlib import resources

UNIT_NAME = "hal-gateway.service"
BIN_PLACEHOLDER = "@BIN@"


def unit_template() -> str:
    """The unit file text with `@BIN@` still in place."""
    return resources.files("halgateway").joinpath("data", UNIT_NAME).read_text(encoding="ascii")


def render_unit(bin_dir: str) -> str:
    """The unit text for `bin_dir`; raises `ValueError` for an empty or whitespace-bearing path."""
    if not bin_dir or any(ch.isspace() for ch in bin_dir):
        raise ValueError(f"bin_dir must be a non-empty path without whitespace: {bin_dir!r}")
    return unit_template().replace(BIN_PLACEHOLDER, bin_dir.rstrip("/"))
