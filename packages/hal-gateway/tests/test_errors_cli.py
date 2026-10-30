"""Tests for `halgateway.errors` and the `check-config` command."""

import json

import pytest

from halgateway import errors
from halgateway.cli import EXIT_CONFIG, EXIT_OK, main


def test_openai_error_body_carries_message_and_code():
    body = json.loads(errors.error_body(errors.NotFound("no such model"), "openai"))
    assert body["error"]["message"] == "no such model" and body["error"]["code"] == "not_found"


def test_anthropic_error_body_uses_its_envelope():
    body = json.loads(errors.error_body(errors.Unauthorized("bad token"), "anthropic"))
    assert body == {"type": "error", "error": {"type": "authentication_error", "message": "bad token"}}


def test_ollama_error_body_is_a_flat_string():
    assert json.loads(errors.error_body(errors.Forbidden("no"), "ollama")) == {"error": "no"}


def test_unknown_error_format_is_rejected():
    with pytest.raises(ValueError, match="xml"):
        errors.error_body(errors.BadRequest("x"), "xml")


def test_each_error_class_has_its_own_status():
    assert [
        e.status
        for e in (
            errors.BadRequest,
            errors.Unauthorized,
            errors.Forbidden,
            errors.NotFound,
            errors.PayloadTooLarge,
            errors.TooManyRequests,
            errors.BadGatewayError,
            errors.Unavailable,
        )
    ] == [400, 401, 403, 404, 413, 429, 502, 503]


def test_check_config_accepts_a_valid_file(tmp_path, capsys):
    path = tmp_path / "c.toml"
    path.write_text("")
    assert main(["check-config", "--config", str(path)]) == EXIT_OK
    assert capsys.readouterr().out.startswith("ok:")


def test_check_config_reports_a_bad_key_and_exits_3(tmp_path, capsys):
    path = tmp_path / "c.toml"
    path.write_text("[gateway]\nbogus = 1\n")
    assert main(["check-config", "--config", str(path)]) == EXIT_CONFIG
    assert "bogus" in capsys.readouterr().err
