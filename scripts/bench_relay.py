"""Measures the gateway relay overhead against a fake local upstream.

Usage: python3 scripts/bench_relay.py [N]

Starts a throwaway gateway (private temp state, no real models) in front of a fake
OpenAI-compatible server, sends N chat requests over the unix socket and prints the median
and 95th percentile latency, direct to the upstream and through the gateway, so the
difference is the relay cost (design 05: 0.2 to 0.4 ms).
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import socket
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

DEFAULT_REQUESTS = 500
REPLY = json.dumps({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
BODY = json.dumps({"model": "bench", "messages": [{"role": "user", "content": "hi"}]})


class Upstream(http.server.BaseHTTPRequestHandler):
    """Answers every request with a fixed small reply."""

    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True  # else delayed ACK adds ~40 ms to every direct reply

    def log_message(self, *args: object) -> None:
        """Silences the access log."""

    def do_GET(self) -> None:  # noqa: N802 - http.server dispatch name
        """Serves the model list."""
        self._reply(json.dumps({"data": [{"id": "bench"}]}).encode())

    def do_POST(self) -> None:  # noqa: N802 - http.server dispatch name
        """Serves the fixed reply."""
        self.rfile.read(int(self.headers["Content-Length"]))
        self._reply(REPLY)

    def _reply(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def timed(conn: http.client.HTTPConnection, count: int) -> list[float]:
    """Returns the latency in ms of `count` POSTs on one connection."""
    out = []
    for _ in range(count):
        start = time.perf_counter()
        conn.request("POST", "/v1/chat/completions", body=BODY)
        conn.getresponse().read()
        out.append((time.perf_counter() - start) * 1000)
    return out


def summary(label: str, values: list[float]) -> float:
    """Prints and returns the median of `values`."""
    ordered = sorted(values)
    median = statistics.median(ordered)
    sys.stdout.write(f"{label:10s} median {median:.3f} ms  p95 {ordered[int(len(ordered) * 0.95)]:.3f} ms\n")
    return median


def main(argv: list[str]) -> int:
    """Runs the benchmark; returns the exit status."""
    count = int(argv[1]) if len(argv) > 1 else DEFAULT_REQUESTS
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HAL_STATE_DIR"] = str(Path(tmp) / "state")
        from halgateway.config import load_config
        from halgateway.server import Gateway

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        token = Path(tmp) / "token"
        token.write_text("hal-gw-bench\n")
        token.chmod(0o600)
        cfg = Path(tmp) / "c.toml"
        cfg.write_text(
            f'[gateway]\nunix_path = "{tmp}/g.sock"\ntoken_file = "{token}"\n'
            f'[gateway.backends.up]\ntype = "openai"\nurl = "http://127.0.0.1:{port}"\n'
            'apis = ["chat", "models"]\n'
        )
        gateway = Gateway(load_config(cfg))
        gateway.start()
        try:
            direct = http.client.HTTPConnection("127.0.0.1", port)
            direct.connect()
            direct.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # else Nagle adds ~40 ms
            via = http.client.HTTPConnection("localhost")
            via.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            via.sock.connect(str(gateway.socket_path()))
            timed(direct, 20)
            timed(via, 20)
            base = summary("direct", timed(direct, count))
            relayed = summary("gateway", timed(via, count))
            sys.stdout.write(f"overhead   {relayed - base:.3f} ms (median)\n")
        finally:
            gateway.stop()
            server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
