"""Tests for `halkit.hardware`: canned tool output, never a real GPU."""

import json
import os
import stat
import subprocess

import pytest

from halkit import hardware, proc

ONE_24GB = "0, NVIDIA GeForce RTX 4090, 24564, 23800, 8.9\n"
TWO_CARDS = "0, NVIDIA GeForce RTX 3090, 24576, 24000, 8.6\n1, NVIDIA GeForce RTX 3060, 12288, 11900, 8.6\n"
LAPTOP_6GB = "0, NVIDIA GeForce RTX 3060 Laptop GPU, 6144, 5500, 8.6\n"
MEMINFO = "MemTotal:       32768000 kB\nMemFree:         1000000 kB\nMemAvailable:   16384000 kB\n"


def fake_runner(outputs: dict[str, object]):
    """A runner that answers by program name; an exception instance is raised instead."""

    def run(argv: list[str], timeout: float) -> str:
        assert timeout > 0
        answer = outputs.get(argv[0], FileNotFoundError(argv[0]))
        if isinstance(answer, BaseException):
            raise answer
        return str(answer)

    return run


@pytest.fixture
def host(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(MEMINFO)
    release = tmp_path / "osrelease"
    release.write_text("6.8.0-generic\n")
    return {"meminfo": meminfo, "osrelease": release}


def test_one_24gb_card_is_reported_with_free_memory_and_compute_capability(host):
    hw = hardware.detect(fake_runner({"nvidia-smi": ONE_24GB}), **host)
    assert [(g.name, g.total_mib, g.free_mib, g.compute_cap) for g in hw.gpus] == [
        ("NVIDIA GeForce RTX 4090", 24564, 23800, "8.9")
    ]
    assert (hw.ram_total_mib, hw.ram_free_mib) == (32000, 16000)


def test_two_cards_are_both_listed_in_index_order(host):
    hw = hardware.detect(fake_runner({"nvidia-smi": TWO_CARDS}), **host)
    assert [(g.index, g.total_mib) for g in hw.gpus] == [(0, 24576), (1, 12288)]


def test_a_6gb_laptop_card_is_reported(host):
    hw = hardware.detect(fake_runner({"nvidia-smi": LAPTOP_6GB}), **host)
    assert hw.gpus[0].total_mib == 6144


def test_no_gpu_tool_gives_cpu_only_with_a_note(host):
    hw = hardware.detect(fake_runner({}), which=lambda _: None, **host)
    assert hw.gpus == ()
    assert "no GPU tool answered" in hw.notes


def test_old_driver_without_compute_cap_is_retried_without_it(host):
    calls = []

    def run(argv, timeout):
        calls.append(argv[1])
        if "compute_cap" in argv[1]:
            raise subprocess.CalledProcessError(2, argv)
        return "0, Old Card, 8192, 8000\n"

    hw = hardware.detect(run, **host)
    assert (hw.gpus[0].compute_cap, len(calls)) == ("", 2)


def test_nvidia_smi_timeout_is_reported_not_raised(host):
    hw = hardware.detect(fake_runner({"nvidia-smi": proc.ProcTimeout(["nvidia-smi"], 10.0)}), **host)
    assert hw.gpus == ()
    assert any("nvidia-smi unreadable" in n for n in hw.notes)


def test_garbage_output_is_reported_not_raised(host):
    hw = hardware.detect(fake_runner({"nvidia-smi": "oops\n"}), **host)
    assert hw.gpus == () and any("unreadable" in n for n in hw.notes)


def test_amd_smi_json_gives_name_total_and_free(host):
    static = json.dumps(
        [
            {
                "gpu": 0,
                "asic": {"market_name": "AMD Radeon RX 7900 XTX"},
                "vram": {"size": {"value": 24560, "unit": "MB"}},
            }
        ]
    )
    metric = json.dumps([{"gpu": 0, "mem_usage": {"free_vram": {"value": 24000, "unit": "MB"}}}])
    hw = hardware.detect(fake_runner({"amd-smi": static}), **host)
    assert hw.gpus[0].free_mib == 24560  # the canned runner answers both calls alike: no free key
    both = hardware.parse_amd_smi(static, metric)
    assert (both[0].vendor, both[0].total_mib, both[0].free_mib) == ("amd", 24560, 24000)


def test_rocm_smi_is_used_when_amd_smi_is_missing(host):
    doc = {
        "card0": {
            "Card Series": "Radeon RX 6800",
            "VRAM Total Memory (B)": str(16 * 1024**3),
            "VRAM Total Used Memory (B)": str(1024**3),
        }
    }
    hw = hardware.detect(fake_runner({"rocm-smi": json.dumps(doc)}), **host)
    assert (hw.gpus[0].name, hw.gpus[0].total_mib, hw.gpus[0].free_mib) == ("Radeon RX 6800", 16384, 15360)


def test_wsl2_is_detected_from_the_kernel_release(host):
    host["osrelease"].write_text("5.15.153.1-microsoft-standard-WSL2\n")
    assert hardware.detect(fake_runner({}), **host).wsl2 is True


def test_missing_meminfo_is_reported_not_raised(host, tmp_path):
    host["meminfo"] = tmp_path / "absent"
    hw = hardware.detect(fake_runner({}), **host)
    assert hw.ram_total_mib == 0 and any("RAM" in n for n in hw.notes)


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX shell script")
def test_a_fake_nvidia_smi_on_disk_runs_through_the_default_runner(tmp_path):
    script = tmp_path / "nvidia-smi"
    script.write_text(f"#!/bin/sh\nprintf '%s' '{ONE_24GB}'\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    out = hardware.default_runner([str(script)], 5.0)
    assert hardware.parse_nvidia(out)[0].total_mib == 24564
