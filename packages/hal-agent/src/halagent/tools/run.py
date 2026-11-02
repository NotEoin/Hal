"""The `run` tool: a bash command in the project directory with bounded output.

Output is read with `selectors` into a head buffer and a tail ring buffer so memory stays bounded
whatever the command prints. A non-zero exit is a normal result: a failing test is information,
and calling it an error would invite retries (design 03-agent, 3.7.7).
"""

from __future__ import annotations

import collections
import os
import re
import selectors
import signal
import subprocess
import time

from halagent.tools.paths import ToolError, Workspace

DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
KILL_GRACE_S = 5
HEAD_CHARS = 2_000
TAIL_CHARS = 6_000
READ_CHUNK = 65_536
TITLE_CHARS = 80
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
CLEAN_ENV = {
    "LANG": "C.UTF-8", "TERM": "dumb", "PYTHONDONTWRITEBYTECODE": "1", "PAGER": "cat",
    "GIT_PAGER": "cat", "NO_COLOR": "1", "CI": "1",
}  # fmt: skip


def box_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for commands: the inherited one minus `HAL_*` (socket paths stay out)."""
    base = os.environ if environ is None else environ
    return {**{k: v for k, v in base.items() if not k.startswith("HAL_")}, **CLEAN_ENV}


def _collapse(text: str) -> str:
    """Keeps the last version of a line overwritten with carriage returns."""
    return "\n".join(line.rsplit("\r", 1)[-1] for line in text.replace("\r\n", "\n").split("\n"))


def _kill(proc: subprocess.Popen[bytes]) -> None:
    for sig, wait in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, KILL_GRACE_S)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            # still running after this slice of the wait; the loop checks the deadline
            continue


def run(ws: Workspace, args: dict, *, environ: dict[str, str] | None = None) -> str:
    """Runs `bash -c`; returns the exit line and the head and tail of the output."""
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ToolError("invalid_args", "command must be non-empty text")
    try:
        timeout = min(max(int(args.get("timeout") or DEFAULT_TIMEOUT_S), 1), MAX_TIMEOUT_S)
    except (TypeError, ValueError) as exc:
        raise ToolError("invalid_args", "timeout must be an integer") from exc
    start = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - the model's command is the point of this tool, boxed by the sandbox
        ["bash", "-c", command], cwd=ws.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True, close_fds=True, env=box_env(environ),
    )  # fmt: skip
    assert proc.stdout is not None  # noqa: S101 - PIPE was requested above
    head = bytearray()
    tail: collections.deque[bytes] = collections.deque()
    tail_size = total = 0
    timed_out = False
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    try:
        while sel.get_map():
            left = timeout - (time.monotonic() - start)
            if left <= 0:
                timed_out = True
                break
            if not sel.select(timeout=min(left, 1.0)):
                continue
            chunk = os.read(proc.stdout.fileno(), READ_CHUNK)
            if not chunk:
                sel.unregister(proc.stdout)
                break
            total += len(chunk)
            room = HEAD_CHARS * 4 - len(head)
            if room > 0:
                head += chunk[:room]
                chunk = chunk[room:]
            if chunk:
                tail.append(chunk)
                tail_size += len(chunk)
                while tail_size - len(tail[0]) >= TAIL_CHARS * 4:
                    tail_size -= len(tail.popleft())
    finally:
        if timed_out or proc.poll() is None:
            _kill(proc)
        proc.stdout.close()
        sel.close()
    clean = lambda b: _collapse(ANSI.sub("", bytes(b).decode("utf-8", "replace")))  # noqa: E731 - local helper
    head_t, tail_t = clean(head)[:HEAD_CHARS], clean(b"".join(tail))[-TAIL_CHARS:]
    secs = time.monotonic() - start
    if timed_out:
        first = f"stopped after {timeout} s (timeout); partial output below"
    else:
        first = f"exit code {proc.returncode} after {secs:.1f} s"
    shown = len(head_t) + len(tail_t)
    if total > shown:
        cut = total - shown
        note = f"[output: {total} characters; the first {HEAD_CHARS} and the last {TAIL_CHARS} are shown]"
        out = f"{note}\n{head_t}\n[... {cut} characters cut ...]\n{tail_t}"
    else:
        out = head_t + tail_t
    return f"{first}\n{out}".rstrip()
