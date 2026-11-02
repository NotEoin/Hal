#!/usr/bin/env python3
"""`hal-mcp-bridge`: splices stdin and stdout to the broker socket (02-sandbox REQ-BOX-38).

Stdlib only, so it also runs as a lone file in a box image. It never writes to stdout itself: the
stream is MCP framing and a stray byte would corrupt it.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading

DEFAULT_SOCKET = "/run/hal/broker.sock"
CONNECT_TIMEOUT_S = 5.0
CHUNK = 65536
EXIT_UNAVAILABLE = 69  # EX_UNAVAILABLE


def _stdin_to_socket(sock: socket.socket) -> None:
    """Copies stdin to the socket, then half-closes the write side on EOF."""
    try:
        while chunk := os.read(0, CHUNK):
            sock.sendall(chunk)
    except OSError:
        # stdin closed or the socket broke; stop copying
        pass
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        # the peer already closed
        pass


def main(argv: list[str] | None = None) -> int:
    """Runs the bridge; returns the exit status (0, or 69 when the socket cannot be reached)."""
    parser = argparse.ArgumentParser(prog="hal-mcp-bridge")
    parser.add_argument("--socket", default=os.environ.get("HAL_BROKER_SOCKET", DEFAULT_SOCKET))
    args = parser.parse_args(argv)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT_S)
    try:
        sock.connect(args.socket)
    except OSError as exc:
        sys.stderr.write(f"hal-mcp-bridge: cannot connect to {args.socket}: {exc}\n")
        return EXIT_UNAVAILABLE
    sock.settimeout(None)
    threading.Thread(target=_stdin_to_socket, args=(sock,), daemon=True).start()
    try:
        while chunk := sock.recv(CHUNK):
            os.write(1, chunk)
    except OSError:
        # the socket or stdout closed; stop copying
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
