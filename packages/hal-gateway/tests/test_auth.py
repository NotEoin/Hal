"""Tests for `halgateway.auth`."""

import stat

import pytest

from halgateway import auth
from halgateway.errors import Forbidden, Unauthorized

TOKEN = "hal-gw-test-secret"


def test_token_file_is_created_0600_at_first_start(tmp_path):
    path = tmp_path / "sub" / "token"
    token = auth.load_or_create_owner_token(path)
    assert token.startswith("hal-gw-") and path.read_text().strip() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_token_file_is_read_not_replaced(tmp_path):
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n")
    assert auth.load_or_create_owner_token(path) == TOKEN


def test_world_readable_token_file_is_not_private(tmp_path):
    path = tmp_path / "token"
    path.write_text(TOKEN)
    path.chmod(0o644)
    assert not auth.token_file_is_private(path)


def test_unix_peer_with_our_uid_is_the_owner():
    assert auth.principal_for("unix", 1000, {}, TOKEN, own_uid=1000).kind == "owner"


def test_unix_header_token_is_ignored_for_the_owner():
    headers = {"Authorization": "Bearer stale"}
    assert auth.principal_for("unix", 1000, headers, TOKEN, own_uid=1000).kind == "owner"


def test_tcp_request_without_token_is_anonymous():
    assert auth.principal_for("tcp", None, {}, TOKEN).kind == "anonymous"


def test_tcp_owner_token_is_accepted_as_bearer_and_as_x_api_key():
    assert auth.principal_for("tcp", None, {"Authorization": f"Bearer {TOKEN}"}, TOKEN).kind == "owner"
    assert auth.principal_for("tcp", None, {"X-Api-Key": TOKEN}, TOKEN).kind == "owner"


def test_tcp_wrong_token_is_unauthorized():
    with pytest.raises(Unauthorized):
        auth.principal_for("tcp", None, {"Authorization": "Bearer nope"}, TOKEN)


@pytest.mark.parametrize("token", ["hal-sess-ab12cd34.x", "hal-gwc-laptop.x"])
def test_session_and_client_tokens_are_refused_until_their_registries_exist(token):
    with pytest.raises(Unauthorized):
        auth.principal_for("tcp", None, {"X-Api-Key": token}, TOKEN)


def test_foreign_host_is_forbidden():
    with pytest.raises(Forbidden, match="Host"):
        auth.check_host_and_origin({"Host": "evil.example:8741"}, 8741, [], [])


def test_loopback_host_and_extra_allowed_host_pass():
    auth.check_host_and_origin({"Host": "localhost:8741"}, 8741, [], [])
    auth.check_host_and_origin({"Host": "box.tailnet:8741"}, 8741, ["box.tailnet:8741"], [])


def test_browser_origin_is_forbidden_unless_listed():
    headers = {"Host": "127.0.0.1:8741", "Origin": "http://page.example"}
    with pytest.raises(Forbidden, match="Origin"):
        auth.check_host_and_origin(headers, 8741, [], [])
    auth.check_host_and_origin(headers, 8741, [], ["http://page.example"])
