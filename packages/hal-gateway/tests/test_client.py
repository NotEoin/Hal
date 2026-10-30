"""Tests for `GatewayClient` against a real gateway on a unix socket."""

import pytest

from halgateway.client import ClientError, GatewayClient
from halgateway.config import load_config
from halgateway.server import Gateway

TOKEN = "hal-gw-client-test"


@pytest.fixture
def gateway(tmp_path):
    token = tmp_path / "token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[gateway]\nunix_path = "{tmp_path}/run/gateway.sock"\ntoken_file = "{token}"\n')
    gw = Gateway(load_config(cfg))
    gw.config_file = cfg
    gw.start()
    yield gw
    gw.stop()


@pytest.fixture
def client(gateway):
    return GatewayClient(gateway.socket_path(), TOKEN, timeout_s=5)


def test_health_is_true_for_a_running_gateway_and_false_for_a_missing_socket(client, tmp_path):
    assert client.health() is True
    assert GatewayClient(tmp_path / "none.sock", TOKEN).health() is False


def test_status_and_aliases_answer_documents(client):
    assert "backends" in client.status()
    assert "aliases" in client.aliases()


def test_unreachable_gateway_raises_client_error_naming_the_socket(tmp_path):
    with pytest.raises(ClientError, match="none.sock"):
        GatewayClient(tmp_path / "none.sock", TOKEN).status()


def test_session_can_be_created_listed_and_revoked(client):
    grant = client.create_session("tests", aliases=["hal/*"], ttl_s=600)
    assert grant.token and grant.expires.endswith("Z")
    sessions = client.admin("GET", "/hal/v1/sessions")["sessions"]
    assert [s["label"] for s in sessions] == ["tests"]
    client.revoke_session(grant.id)
    assert client.admin("GET", "/hal/v1/sessions")["sessions"] == []


def test_revoking_an_unknown_session_is_a_404(client):
    with pytest.raises(ClientError) as info:
        client.revoke_session("deadbeef")
    assert info.value.status == 404


def test_session_with_a_bad_limit_is_refused_by_the_gateway(client):
    with pytest.raises(ClientError) as info:
        client.create_session("x", ttl_s=-5)
    assert info.value.status == 400


def test_leased_session_shows_in_leases(client):
    client.create_session("voice", lease=True)
    assert [lease.label for lease in client.leases()] == ["voice"]


def test_release_gpu_returns_a_typed_result(client):
    result = client.release_gpu()
    assert result.released in (True, False)
    assert isinstance(result.unloaded, tuple) and isinstance(result.kept, tuple)


def test_router_calls_without_a_managed_router_are_404(client):
    for call in (
        lambda: client.set_mode("gpu"),
        client.stop_router,
        lambda: client.load("m"),
        lambda: client.unload("m"),
    ):
        with pytest.raises(ClientError) as info:
            call()
        assert info.value.status == 404


def test_models_lists_entries_as_the_owner(client):
    assert isinstance(client.models(), list)


def test_from_config_reads_the_owner_token(gateway):
    assert GatewayClient.from_config(gateway.config_file).token == TOKEN
