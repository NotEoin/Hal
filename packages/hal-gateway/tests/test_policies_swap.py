"""Tests for the swap scheduler (REQ-GW-46..48) against a fake router, and its handler wiring."""

import http.client
import http.server
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from halgateway.config import load_config
from halgateway.modes import HalSettings
from halgateway.policies.swap import SwapError, SwapScheduler, SwapTimeout, last_stop_reason
from halgateway.server import Gateway

TOOLS = [{"name": "Bash"}]
WITH_TOOLS = {"tools": TOOLS, "messages": [{"role": "user", "content": "x"}]}
SIDE = {"messages": [{"role": "user", "content": "title?"}]}


class FakeRouter:
    """Records calls and moves the status of a preset on unload and load."""

    def __init__(self, loaded="big", fail_load=False):
        self.status = {"big": "unloaded", "small": "unloaded"}
        if loaded:
            self.status[loaded] = "loaded"
        self.calls, self.fail_load = [], fail_load

    def statuses(self):
        return dict(self.status)

    def swap_call(self, path, body, check=True):
        self.calls.append((path, body.get("model"), body.get("filename")))
        if path == "/models/unload":
            self.status[body["model"]] = "unloaded"
        elif path == "/models/load":
            if self.fail_load:
                raise SwapError("/models/load: 500")
            self.status[body["model"]] = "loaded"
        elif "action=save" in path:
            return {"n_saved": 10}
        elif "action=restore" in path:
            return {"n_restored": 10}
        return {}


def scheduler(ops, **kw):
    kw = {"quiet_s": 0.0, "wait_poll_s": 0.02, "status_poll_s": 0.01, **kw}
    return SwapScheduler(ops, "big", "small", **kw)


def test_swap_saves_and_restores_strong_slots():
    ops = FakeRouter("big")
    s = scheduler(ops)
    ticket = s.admit("small", WITH_TOOLS, "k")
    assert ticket.model == "small" and "big -> small" in ticket.swap
    assert [c for c in ops.calls if "slots" in c[0]] == [
        ("/slots/0?action=save", "big", "big-0.bin"),
        ("/slots/1?action=save", "big", "big-1.bin"),
    ]
    s.finish(ticket, "end_turn")
    back = s.admit("big", WITH_TOOLS, "k")
    assert back.model == "big" and back.swap.endswith("restored 20 tokens")
    assert ("/slots/0?action=restore", "big", "big-0.bin") in ops.calls


def test_swap_waits_for_quiet_and_subagents():
    ops = FakeRouter("small")
    s = scheduler(ops, quiet_s=0.3)
    held = s.admit("small", WITH_TOOLS, "sub")  # a subagent conversation is running
    s.finish(held, "tool_use")  # it asked for a tool, so it is still active
    done = []
    thread = threading.Thread(target=lambda: done.append(s.admit("big", WITH_TOOLS, None)))
    thread.start()
    time.sleep(0.5)
    assert not done and ops.status["small"] == "loaded"  # quiet has passed but the subagent holds the model
    again = s.admit("small", WITH_TOOLS, "sub")
    s.finish(again, "end_turn")  # the subagent is done
    thread.join(5)
    assert done and done[0].model == "big" and ops.status["big"] == "loaded"


def test_stale_subagent_no_longer_holds_the_model():
    ops = FakeRouter("small")
    s = scheduler(ops, agent_stale_s=0.1)
    s.finish(s.admit("small", WITH_TOOLS, "sub"), "tool_use")
    time.sleep(0.2)
    assert s.admit("big", WITH_TOOLS, None).model == "big"


def test_swap_wait_times_out():
    s = scheduler(FakeRouter("small"), swap_wait_s=0.2)
    s.admit("small", WITH_TOOLS, "k")  # in flight, never finished
    with pytest.raises(SwapTimeout):
        s.admit("big", WITH_TOOLS, None)


def test_failed_swap_recovers_state():
    ops = FakeRouter("big", fail_load=True)
    s = scheduler(ops)
    with pytest.raises(SwapError, match="could not load small"):
        s.admit("small", WITH_TOOLS, "k")
    assert s.active is None and not s.switching  # nothing is loaded now; the router says so
    ops.fail_load = False
    assert s.admit("small", WITH_TOOLS, "k").model == "small"


def test_side_request_uses_loaded_model():
    ops = FakeRouter("small")
    s = scheduler(ops)
    ticket = s.admit("big", SIDE, None)
    assert ticket.model == "small" and ops.calls == []


def test_side_request_loads_main_when_idle():
    ops = FakeRouter(loaded=None)
    s = scheduler(ops)
    ticket = s.admit("small", SIDE, None)
    assert ticket.model == "big" and ("/models/load", "big", None) in ops.calls


def test_large_request_without_tools_is_not_a_side_request():
    s = scheduler(FakeRouter("big"))
    big = {"messages": [{"role": "user", "content": "x" * 60_000}]}
    assert s.owner("small", big) == "small" and s.owner("small", SIDE) is None


def test_last_stop_reason_reads_both_formats():
    assert last_stop_reason(b'..."stop_reason": "end_turn"...') == "end_turn"
    assert last_stop_reason(b'"finish_reason":"tool_calls"} ... "finish_reason":"stop"') == "stop"
    assert last_stop_reason(b"nothing") is None


# --- through the handler -----------------------------------------------------------------------

SEEN = []


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        SEEN.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


@pytest.fixture
def gateway(tmp_path):
    SEEN.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token = tmp_path / "token"
    token.write_text("t\n")
    token.chmod(0o600)
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'[gateway]\nunix_path = "{tmp_path}/run/g.sock"\ntoken_file = "{token}"\nmain = ""\nagent = ""\n'
        f'[gateway.backends.local]\ntype = "llama-router"\nmanaged = false\n'
        f'url = "http://127.0.0.1:{server.server_address[1]}"\n'
    )
    gw = Gateway(load_config(cfg))
    router = gw.backends["local"]
    router.hal = HalSettings(
        "", "big", "small", None, 1, False, False, False, Path("n"), None, (), True, 2, 1000, None
    )
    router.fake = FakeRouter("big")
    router._swap = scheduler(router.fake, swap_wait_s=0.3)
    gw.resolver.mode_aliases.update({"hal/main": ("local", "big"), "hal/agent": ("local", "small")})
    gw.start()
    yield gw
    gw.stop()
    server.shutdown()
    server.server_close()


def post(gw, doc):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(gw.socket_path()))
    conn = http.client.HTTPConnection("localhost")
    conn.sock = sock
    conn.request("POST", "/v1/messages", body=json.dumps(doc))
    resp = conn.getresponse()
    return resp.status, resp.getheader("Retry-After")


def test_handler_swaps_to_the_agent_model_and_releases_the_request(gateway):
    status, _ = post(gateway, {"model": "hal/agent", **WITH_TOOLS})
    assert status == 200 and SEEN[0]["model"] == "small"
    sched = gateway.backends["local"].scheduler()
    deadline = time.monotonic() + 5
    while sched.inflight and time.monotonic() < deadline:  # finish runs after the reply is sent
        time.sleep(0.02)
    assert sched.inflight == 0


def test_handler_answers_503_with_retry_after_when_the_swap_wait_expires(gateway):
    scheduler_ = gateway.backends["local"].scheduler()
    scheduler_.inflight = 1  # a request that never ends
    status, retry = post(gateway, {"model": "hal/agent", **WITH_TOOLS})
    assert status == 503 and retry == "30" and SEEN == []
