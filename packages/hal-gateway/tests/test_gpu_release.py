"""End-to-end tests of the `gpu/release` admin route, the off flag and the `release-gpu` CLI."""

import http.client
import json
import socket

import pytest

from halgateway import cli
from halgateway.config import load_config
from halgateway.server import Gateway


@pytest.fixture
def gateway(tmp_path):
    token = tmp_path / "token"
    token.write_text("t\n")
    token.chmod(0o600)
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[gateway]\nunix_path = "{tmp_path}/run/g.sock"\ntoken_file = "{token}"\n'
        '[gateway.gpu]\ncoordinator = "none"\n'
        '[gateway.backends.local]\ntype = "llama-router"\nmanaged = false\nurl = "http://127.0.0.1:9"\n'
        '[gateway.aliases.m]\nbackend = "local"\nmodel = "m"\n'
    )
    gw = Gateway(load_config(cfg))
    gw.start()
    gw.cfg_path = cfg
    yield gw
    gw.stop()


def call(gw, method, path, doc=None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(gw.socket_path()))
    conn = http.client.HTTPConnection("localhost")
    conn.sock = sock
    conn.request(method, path, body=json.dumps(doc or {}), headers={"Authorization": "Bearer t"})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read() or b"{}")


def test_release_route_answers_a_release_result(gateway):
    status, doc = call(gateway, "POST", "/hal/v1/gpu/release", {"force": True})
    assert status == 200 and doc["released"] is True and doc["unloaded"] == [] and doc["busy"] == 0


def test_release_route_rejects_non_boolean_flags(gateway):
    status, _ = call(gateway, "POST", "/hal/v1/gpu/release", {"force": "yes"})
    assert status == 400


def test_off_flag_gives_503_for_a_gpu_backend(gateway):
    gateway.gpu.off_flag.parent.mkdir(parents=True, exist_ok=True)
    gateway.gpu.off_flag.write_text("")
    status, doc = call(gateway, "POST", "/v1/chat/completions", {"model": "m", "messages": []})
    assert status == 503 and "hal on" in doc["error"]["message"]


def test_status_document_has_a_gpu_section(gateway):
    status, doc = call(gateway, "GET", "/hal/v1/status")
    assert status == 200 and doc["gpu"]["coordinator"] == {"kind": "none"}


def test_release_gpu_command_prints_the_result(gateway, capsys):
    assert cli.main(["release-gpu", "--config", str(gateway.cfg_path)]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["released"] is True


def test_release_gpu_command_reports_an_unreachable_gateway(tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[gateway]\nunix_path = "{tmp_path}/none/g.sock"\ntoken_file = "{tmp_path}/tok"\n')
    (tmp_path / "tok").write_text("t\n")
    (tmp_path / "tok").chmod(0o600)
    assert cli.main(["release-gpu", "--config", str(cfg)]) == cli.EXIT_GATEWAY
