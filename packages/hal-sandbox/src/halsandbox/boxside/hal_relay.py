#!/usr/bin/env python3
"""`hal-relay`: a loopback TCP listener that splices each connection to a unix socket (REQ-BOX-37).

Stdlib only. It does no HTTP parsing, so keep-alive, chunked bodies and SSE pass through as they
arrive. It binds `127.0.0.1` only: it takes no token, so it must not be reachable from outside
the box (the host forwarder behind it injects the session token).
"""

from __future__ import annotations

import argparse
import contextlib
import socket
import sys
import threading

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8741
DEFAULT_SOCKET = "/run/hal/model.sock"
DEFAULT_MAX_CONNS = 32
CHUNK = 65536
CONNECT_TIMEOUT_S = 5.0


def _pump(src: socket.socket, dst: socket.socket) -> None:
    """Copies `src` to `dst` until EOF, then half-closes `dst`."""
    try:
        while chunk := src.recv(CHUNK):
            dst.sendall(chunk)
    except OSError:
        # the peer closed; stop copying
        pass
    with contextlib.suppress(OSError):
        dst.shutdown(socket.SHUT_WR)


class Relay:
    """Accepts loopback connections and splices each one to `socket_path`."""

    def __init__(
        self, port: int = DEFAULT_PORT, socket_path: str = DEFAULT_SOCKET, max_conns: int = DEFAULT_MAX_CONNS
    ):
        self.port = port
        self.socket_path = socket_path
        self.max_conns = max_conns
        self._server: socket.socket | None = None
        self._active = 0
        self._lock = threading.Lock()

    def start(self) -> int:
        """Binds, starts the accept thread and returns the bound port (useful with port 0)."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((LOOPBACK, self.port))
        server.listen(self.max_conns)
        self._server = server
        threading.Thread(target=self._accept_loop, name="relay", daemon=True).start()
        return int(server.getsockname()[1])

    def stop(self) -> None:
        """Closes the listener; open connections finish on their own."""
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()

    def _accept_loop(self) -> None:
        assert self._server is not None
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with self._lock:
                if self._active >= self.max_conns:
                    conn.close()
                    continue
                self._active += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.settimeout(CONNECT_TIMEOUT_S)
            upstream.connect(self.socket_path)
            upstream.settimeout(None)
            back = threading.Thread(target=_pump, args=(upstream, conn), daemon=True)
            back.start()
            _pump(conn, upstream)
            back.join()
        except OSError:
            # the connection broke; the relay for it ends
            pass
        finally:
            upstream.close()
            conn.close()
            with self._lock:
                self._active -= 1


def main(argv: list[str] | None = None) -> int:
    """Runs the relay in the foreground."""
    parser = argparse.ArgumentParser(prog="hal-relay")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--max-conns", type=int, default=DEFAULT_MAX_CONNS)
    args = parser.parse_args(argv)
    Relay(args.port, args.socket, args.max_conns).start()
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
