"""End to end: `run_code` boxes hal-agent with a scripted OpenAI-style model server (no real model).

The fake server asks for one `write_file` tool call and then answers; the test checks that the
write landed in the isolated copy and never in the real project. Runs under `runc` and `runsc`
when docker has them and the `hal-agent:local` image exists (`hal code --build`).
"""

from __future__ import annotations

import http.server
import json
import subprocess
import threading

import pytest

from halsandbox.code import DEFAULT_IMAGE, docker_runtimes, run_code

pytestmark = pytest.mark.docker

RUN_TIMEOUT_S = 300  # runsc start-up plus two model round trips


def available_runtimes() -> list[str]:
    try:
        have = docker_runtimes()
        inspect = ["docker", "image", "inspect", DEFAULT_IMAGE]
        subprocess.run(inspect, check=True, capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001 - any failure means "no usable docker here", so skip
        return []
    return [r for r in ("runc", "runsc") if r in have]


RUNTIMES = available_runtimes()


class ScriptedModel(http.server.ThreadingHTTPServer):
    """Answers /v1/chat/completions with a `write` call, then a `finish` call."""

    def __init__(self) -> None:
        self.calls = 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self._send({"object": "list", "data": [{"id": "fake", "object": "model"}]})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.calls += 1
                name, args = (
                    ("write", {"path": "made.txt", "content": "hi\n"})
                    if outer.calls == 1
                    else ("finish", {"summary": "wrote made.txt"})
                )
                call = {
                    "id": f"c{outer.calls}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                msg = {"role": "assistant", "content": None, "tool_calls": [call]}
                reason = "tool_calls"
                self._send(
                    {
                        "id": "x",
                        "object": "chat.completion",
                        "model": "fake",
                        "choices": [{"index": 0, "message": msg, "finish_reason": reason}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                )

            def _send(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        super().__init__(("127.0.0.1", 0), H)


SKIPPED = pytest.param("none", marks=pytest.mark.skip("no docker or image"))


@pytest.mark.parametrize("runtime", RUNTIMES or [SKIPPED])
def test_hal_code_edits_the_isolated_copy_and_never_the_real_project(tmp_path, runtime, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.txt").write_text("a\n")
    srv = ScriptedModel()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status = run_code(
            project,
            upstream=f"http://127.0.0.1:{srv.server_address[1]}",
            runtime=runtime,
            agent_args=["-p", "write made.txt", "--no-session", "--timeout", "120"],
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert status == 0
    assert srv.calls >= 2
    assert not (project / "made.txt").exists()
    copies = list((tmp_path / "state").rglob("made.txt"))
    assert len(copies) == 1 and copies[0].read_text() == "hi\n"
