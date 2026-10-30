"""Tests for the `hal.conf` import."""

from __future__ import annotations

from pathlib import Path

import pytest

from halgateway.legacy import LegacyImportError, import_hal_conf, parse_hal_conf


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "hal.conf"
    path.write_text(text, encoding="utf-8")
    return path


def test_all_five_keys_map_to_config_tables(tmp_path):
    path = _write(
        tmp_path,
        "HAL_LLAMA_SERVER=/opt/llama/llama-server\nHAL_MODELS_DIR='/data/models'\n"
        'HAL_LLAMA_URL="http://127.0.0.1:9090"\nHAL_MODE=hybrid\nHAL_OLLAMA_URL=http://localhost:11434\n',
    )
    assert import_hal_conf(path) == {
        "gateway": {
            "backends": {
                "local": {
                    "server": "/opt/llama/llama-server",
                    "models_dir": "/data/models",
                    "mode": "hybrid",
                    "port": 9090,
                }
            }
        },
        "hal": {"ollama_url": "http://localhost:11434"},
    }


def test_shell_code_in_values_is_never_run(tmp_path):
    marker = tmp_path / "ran"
    path = _write(tmp_path, f"HAL_MODE=$(touch {marker})\nexport HAL_MODELS_DIR=`touch {marker}`\n")
    result = import_hal_conf(path)
    assert not marker.exists()
    assert result["gateway"]["backends"]["local"]["mode"] == f"$(touch {marker})"


def test_home_prefix_is_expanded_and_other_keys_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _write(tmp_path, "HAL_MODELS_DIR=~/models\nHAL_MODEL=qwen\n# HAL_MODE=old\n")
    assert import_hal_conf(path) == {"gateway": {"backends": {"local": {"models_dir": f"{tmp_path}/models"}}}}


def test_later_assignment_wins_and_trailing_comment_is_dropped():
    assert parse_hal_conf("HAL_MODE=gpu\nHAL_MODE=cpu # slow\n") == {"HAL_MODE": "cpu"}


def test_empty_file_gives_empty_result(tmp_path):
    assert import_hal_conf(_write(tmp_path, "HAL_MODE=\n")) == {}


@pytest.mark.parametrize(
    "url", ["http://example.com:8080", "https://127.0.0.1:8080", "http://127.0.0.1", "http://127.0.0.1:99999"]
)
def test_non_loopback_or_portless_llama_url_fails_loudly(tmp_path, url):
    with pytest.raises(LegacyImportError, match="HAL_LLAMA_URL"):
        import_hal_conf(_write(tmp_path, f"HAL_LLAMA_URL={url}\n"))


def test_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        import_hal_conf(tmp_path / "absent.conf")
