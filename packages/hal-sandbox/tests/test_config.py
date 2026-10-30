"""Tests for the sandbox config tables: defaults, validation and limit resolution."""

import pytest

from halkit.config import ConfigError
from halsandbox import config as sbconfig
from halsandbox.limits import Limits


def _load(tmp_path, text):
    f = tmp_path / "c.toml"
    f.write_text(text)
    return sbconfig.load_all(f, environ={})


def test_defaults_match_the_documented_table(tmp_path):
    cfg = sbconfig.load_all(tmp_path / "none.toml", environ={})
    assert cfg["sandbox"]["backend"] == "auto" and cfg["sandbox"]["mask"] == "default"
    assert cfg["transport"]["max_message_bytes"] == 4 * 1024 * 1024
    assert cfg["transport"]["partial_timeout_s"] == 30 and cfg["limits"]["nofile"] == 8192
    assert cfg["box"]["relay_port"] == 8741 and cfg["isolated"]["max_copy_bytes"] == 2 * 1024**3


@pytest.mark.parametrize(
    "text",
    [
        '[sandbox]\nextra_apt_packages = ["Bad Name"]\n',
        '[sandbox]\nenv_passthrough = ["MY_API_TOKEN"]\n',
        '[sandbox]\nenv_passthrough = ["lower"]\n',
        '[sandbox]\nmask = "everything"\n',
        '[sandbox.limits]\ntmp_size = "auto"\n',
        '[sandbox]\nbackend = "podman"\n',
        "[box]\nrelay_port = 70000\n",
        "[sandbox.limits]\nnope = 1\n",
    ],
)
def test_invalid_values_are_refused(tmp_path, text):
    with pytest.raises(ConfigError):
        _load(tmp_path, text)


def test_a_mask_list_and_an_empty_list_are_accepted(tmp_path):
    assert _load(tmp_path, "[sandbox]\nmask = []\n")["sandbox"]["mask"] == []
    assert _load(tmp_path, '[sandbox]\nmask = ["*.pem"]\n')["sandbox"]["mask"] == ["*.pem"]


def test_limits_table_reaches_docker_flags(tmp_path):
    cfg = _load(tmp_path, '[sandbox.limits]\nmemory = "2g"\ntmp_size = "256m"\nnofile = 100\n')
    flags = Limits.from_config(cfg["limits"]).docker_flags()
    assert "nofile=100:100" in flags and str(2 * 1024**3) in flags
