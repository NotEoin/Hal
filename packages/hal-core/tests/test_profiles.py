"""Tests for `halcore.profiles` and the profile choice in `hal configure` (REQ-CLI-83)."""

from __future__ import annotations

import tomllib

import pytest

from halcore import configure, profiles


@pytest.mark.parametrize(
    ("vram", "ram", "expected"),
    [
        (24564, 64, "max"),
        (24564, 32, "24gb"),
        (22000, 60, "max"),
        (21999, 128, "12gb"),
        (12288, 16, "12gb"),
        (10999, 16, "cloud"),
        (0, 64, "cloud"),
    ],
)
def test_profile_thresholds_match_setup_sh(vram, ram, expected):
    assert profiles.profile_for_hardware(vram, ram) == expected


def test_missing_nvidia_smi_means_no_gpu(monkeypatch):
    def boom(*_a, **_k):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(profiles.proc, "run", boom)
    assert profiles.detect_vram_mib() == 0


def test_meminfo_is_read_in_gib(tmp_path, monkeypatch):
    info = tmp_path / "meminfo"
    info.write_text("MemTotal:       33554432 kB\nMemFree: 1 kB\n", encoding="ascii")
    monkeypatch.setattr(profiles, "MEMINFO", info)
    assert profiles.detect_ram_gib() == 32.0


def test_configure_writes_the_detected_profile_and_its_chat_model(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    monkeypatch.setenv("HAL_CONFIG", str(target))
    monkeypatch.setattr(profiles, "detect_profile", lambda: "12gb")
    assert configure.main(["--yes", "--vault", str(tmp_path)]) == 0
    hal = tomllib.loads(target.read_text())["hal"]
    assert hal["profile"] == "12gb"
    assert hal["chat_model"] == "qwen3.5:4b"
    assert target.stat().st_mode & 0o777 == 0o600


def test_configure_never_overwrites_keys(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text('[hal]\nchat_model = "mine"\n\n[hub]\nmodule_paths = []\n')
    monkeypatch.setenv("HAL_CONFIG", str(target))
    configure.main(["--profile", "max", "--yes"])
    assert tomllib.loads(target.read_text())["hal"]["chat_model"] == "mine"
