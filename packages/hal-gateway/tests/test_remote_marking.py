"""A remote backend is visibly marked at start-up, in status and in the model list (REQ-GW-22)."""

import logging

import pytest

from halgateway.client import GatewayClient
from halgateway.config import load_config
from halgateway.server import Gateway

TOKEN = "hal-gw-remote-test"
HOST = "far.example.invalid"


@pytest.fixture
def gateway(tmp_path):
    token = tmp_path / "token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[gateway]\nunix_path = "{tmp_path}/run/gateway.sock"\ntoken_file = "{token}"\n'
        '[gateway.gpu]\ncoordinator = "none"\n'
        f'[gateway.backends.far]\ntype = "openai"\nurl = "https://{HOST}"\nallow_remote = true\n'
        '[gateway.aliases.away]\nbackend = "far"\nmodel = "m"\n'
    )
    return Gateway(load_config(cfg))


def test_remote_backend_logs_a_warning_at_start(gateway, caplog):
    with caplog.at_level(logging.WARNING, logger="halgateway.server"):
        gateway.start()
    gateway.stop()
    assert any(HOST in r.getMessage() and "remote" in r.getMessage() for r in caplog.records)


def test_remote_backend_is_visibly_marked_in_status(gateway):
    gateway.start()
    try:
        status = GatewayClient(gateway.socket_path(), TOKEN, timeout_s=5).status()
    finally:
        gateway.stop()
    assert status["backends"]["far"]["remote"] is True
    assert status["backends"]["far"]["host"] == HOST
    assert any(HOST in w for w in status["warnings"])


def test_gateway_without_remote_backends_has_no_warnings(tmp_path):
    token = tmp_path / "token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[gateway]\nunix_path = "{tmp_path}/g.sock"\ntoken_file = "{token}"\n')
    assert Gateway(load_config(cfg)).remote_warnings() == []
