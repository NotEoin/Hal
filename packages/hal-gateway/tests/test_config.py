"""Tests for `halgateway.config`: every default and every rejected value."""

import pytest

from halgateway.config import load_config
from halkit.config import ConfigError


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_empty_file_gives_the_documented_defaults(tmp_path):
    cfg = load_config(write(tmp_path, ""))
    assert cfg.gateway["tcp"] is False
    assert (cfg.gateway["tcp_host"], cfg.gateway["tcp_port"]) == ("127.0.0.1", 8741)
    assert cfg.gateway["max_request_bytes"] == 32 * 1024 * 1024
    assert cfg.gateway["upstream_read_timeout_s"] == 3600.0
    assert (cfg.log["requests"], cfg.log["content"], cfg.log["keep"]) == (True, False, 3)
    assert (cfg.sessions["max_concurrent"], cfg.sessions["max_requests_per_min"]) == (4, 30)
    assert (cfg.gpu["coordinator"], cfg.gpu["on_busy"], cfg.gpu["idle_release_s"]) == (
        "auto",
        "proceed",
        120.0,
    )
    assert (cfg.gpu["owner_unix_class"], cfg.gpu["owner_tcp_class"]) == ("service", "interactive")


def test_without_backends_only_ollama_is_configured(tmp_path):
    cfg = load_config(write(tmp_path, ""))
    assert list(cfg.backends) == ["ollama"]
    assert cfg.backends["ollama"]["unload_offloaded_on_claim"] is True


def test_unknown_gateway_key_is_an_error_naming_it(tmp_path):
    with pytest.raises(ConfigError, match="tpc"):
        load_config(write(tmp_path, "[gateway]\ntpc = true\n"))


def test_wrong_type_is_an_error_naming_the_key(tmp_path):
    with pytest.raises(ConfigError, match="tcp_port"):
        load_config(write(tmp_path, '[gateway]\ntcp_port = "8741"\n'))


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "localhost"])
def test_tcp_host_must_be_loopback(tmp_path, host):
    with pytest.raises(ConfigError, match="tcp_host"):
        load_config(write(tmp_path, f'[gateway]\ntcp_host = "{host}"\n'))


def test_ipv6_loopback_is_allowed(tmp_path):
    assert load_config(write(tmp_path, '[gateway]\ntcp_host = "::1"\n')).gateway["tcp_host"] == "::1"


@pytest.mark.parametrize("port", [0, 70000])
def test_port_out_of_range_is_rejected(tmp_path, port):
    with pytest.raises(ConfigError, match="tcp_port"):
        load_config(write(tmp_path, f"[gateway]\ntcp_port = {port}\n"))


def test_unknown_coordinator_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="coordinator"):
        load_config(write(tmp_path, '[gateway.gpu]\ncoordinator = "magic"\n'))


def test_session_limits_must_be_positive(tmp_path):
    with pytest.raises(ConfigError, match="max_concurrent"):
        load_config(write(tmp_path, "[gateway.sessions]\nmax_concurrent = 0\n"))


def test_managed_router_needs_a_models_dir(tmp_path):
    with pytest.raises(ConfigError, match="models_dir"):
        load_config(write(tmp_path, '[gateway.backends.local]\ntype = "llama-router"\n'))


def test_managed_router_with_models_dir_gets_its_defaults(tmp_path):
    text = '[gateway.backends.local]\ntype = "llama-router"\nmodels_dir = "/m"\n'
    local = load_config(write(tmp_path, text)).backends["local"]
    assert (local["port"], local["mode"], local["listen"], local["launcher"]) == (
        8080,
        "gpu",
        "tcp",
        "process",
    )


MANAGED = '[gateway.backends.q]\ntype = "managed-openai"\n'
MANAGED_OK = MANAGED + 'command = ["/opt/serve.sh"]\nserved_models = ["m"]\n'


def test_managed_server_gets_its_defaults(tmp_path):
    q = load_config(write(tmp_path, MANAGED_OK)).backends["q"]
    assert (q["port"], q["health_path"], q["autostart"], q["idle_stop"], q["exclusive_gpu"]) == (
        8000,
        "/health",
        "on-demand",
        True,
        True,
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (MANAGED + 'served_models = ["m"]\n', "command is required"),
        (MANAGED + 'command = ["/opt/serve.sh"]\n', "served_models"),
        (MANAGED + 'command = ["serve.sh"]\nserved_models = ["m"]\n', "absolute path"),
        (MANAGED_OK + 'url = "http://127.0.0.1:9"\n', "derived from port"),
        (MANAGED_OK + 'cwd = "work"\n', "absolute path"),
        (MANAGED_OK + 'key_env = "not a name"\n', "environment variable name"),
    ],
)
def test_managed_server_needs_a_command_and_its_models(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


@pytest.mark.parametrize("name", ["HF_TOKEN", "OPENAI_API_KEY", "DB_PASSWORD", "AWS_SECRET_ACCESS_KEY"])
def test_managed_server_env_refuses_secret_looking_names(tmp_path, name):
    with pytest.raises(ConfigError, match="looks like a secret"):
        load_config(write(tmp_path, MANAGED_OK + f'env = {{ {name} = "x" }}\n'))


@pytest.mark.parametrize("name", ["PORT", "HOST", "SERVER_KEY"])
def test_managed_server_env_refuses_names_the_gateway_sets(tmp_path, name):
    text = MANAGED_OK + f'key_env = "SERVER_KEY"\nenv = {{ {name} = "x" }}\n'
    with pytest.raises(ConfigError, match="set by the gateway"):
        load_config(write(tmp_path, text))


def test_backend_without_type_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="type"):
        load_config(write(tmp_path, '[gateway.backends.x]\nurl = "http://127.0.0.1:1"\n'))


def test_backend_named_hal_is_reserved(tmp_path):
    with pytest.raises(ConfigError, match="reserved"):
        load_config(write(tmp_path, '[gateway.backends.hal]\ntype = "ollama"\n'))


def test_remote_backend_needs_allow_remote(tmp_path):
    text = '[gateway.backends.r]\ntype = "openai"\nurl = "https://example.invalid/api"\n'
    with pytest.raises(ConfigError, match="allow_remote"):
        load_config(write(tmp_path, text))


def test_remote_backend_must_use_https(tmp_path):
    text = '[gateway.backends.r]\ntype = "openai"\nurl = "http://example.invalid"\nallow_remote = true\n'
    with pytest.raises(ConfigError, match="https"):
        load_config(write(tmp_path, text))


def test_remote_https_backend_is_accepted_when_allowed(tmp_path):
    text = '[gateway.backends.r]\ntype = "openai"\nurl = "https://example.invalid"\nallow_remote = true\n'
    assert load_config(write(tmp_path, text)).backends["r"]["allow_remote"] is True


def test_alias_must_name_a_configured_backend(tmp_path):
    text = '[gateway.aliases.coder]\nbackend = "nope"\nmodel = "m"\n'
    with pytest.raises(ConfigError, match="nope"):
        load_config(write(tmp_path, text))


def test_alias_defaults(tmp_path):
    text = '[gateway.aliases.coder]\nbackend = "ollama"\nmodel = "m"\n'
    alias = load_config(write(tmp_path, text)).aliases["coder"]
    assert alias["shell_tools"] == ["Bash", "run"] and alias["effort"] is False


def test_malformed_toml_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "[gateway\n"))
