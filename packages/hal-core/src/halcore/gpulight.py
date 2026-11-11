"""Claims the GPU light for Hal, so a background job leaves the GPU before Hal loads a model.

gpu-light is a separate tool: a traffic light for one shared GPU. Background jobs use the card
while the light is green and free it within 30 s of red. Any process on the GPU turns the light
red by itself, but only once it has allocated VRAM, and an Ollama model loaded while a
background job still fills the card is placed partly in system RAM, without an error, and
stays slow until it is reloaded. So Hal claims the light and waits for the background jobs to
leave before it starts a GPU service or loads a model, and releases the claim when it stops.

Every call is optional and none blocks Hal for good. Without gpu-light each function does
nothing, a wait that times out lets Hal start anyway with a warning, and a failing call is
reported as a warning, never raised. HAL_GPU_LIGHT names the gpu-light executable (default:
`gpu-light` on PATH); `HAL_GPU_LIGHT=off` turns the hook off.

Hal holds no claim while idle; the gateway claims per request. A claim named `hal` is the old boot
claim, which `hal off` and `hal units install` still clean up.

    python3 -m halcore gpulight claim NAME [--reason R] [--timeout S] [--pid PID] [--no-wait]
                                           [--unload-offloaded]
    python3 -m halcore gpulight release NAME
    python3 -m halcore gpulight status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

# Default wait of `claim` for the background jobs to leave. gpu-light gives a background job
# 30 s to free the GPU after red, so this is three release deadlines.
CLAIM_WAIT_S = 90
# Extra time a waiting claim gets on top of its own --timeout before Hal stops waiting for it
SLACK_S = 10
# Release and status never wait, so a call that takes longer than this is stuck
CALL_TIMEOUT_S = 10
# gpu-light's exit code when a wait timed out (its docs/design.md, "Command-line interface")
EXIT_TIMEOUT = 4
# The claim name rule of gpu-light's protocol version 1
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass
class Outcome:
    """What a gpu-light call did: `ok` when it ran and succeeded, `warning` for the user otherwise."""

    ok: bool = False
    warning: str = ""


def binary() -> str | None:
    """Returns the gpu-light executable, or None when it is not installed or turned off."""
    configured = os.environ.get("HAL_GPU_LIGHT", "")
    if configured.lower() in ("off", "0", "no", "false"):
        return None
    return shutil.which(configured or "gpu-light")


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess | str:
    """Runs gpu-light with `args`; returns the finished process, or a warning if it could not run."""
    exe = binary()
    if exe is None:
        return ""
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"gpu-light {args[0]} did not finish within {timeout:g} s"
    except OSError as e:
        return f"gpu-light could not run: {e}"


def _failure(args: list[str], proc: subprocess.CompletedProcess) -> str:
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    return f"gpu-light {args[0]} failed (exit {proc.returncode})" + (f": {detail[-1]}" if detail else "")


def status() -> dict | None:
    """Returns `gpu-light status --json`, or None when gpu-light is missing or does not answer."""
    proc = _run(["status", "--json"], CALL_TIMEOUT_S)
    if isinstance(proc, str) or proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def status_line() -> str:
    """One line for `hal status`: the light's colour and why, or "" without gpu-light."""
    doc = status()
    if doc is None:
        return ""
    state = doc.get("effective") or doc.get("state") or "unknown"
    reason = doc.get("reason")
    return f"GPU light: {state}" + (f" ({reason})" if reason and state != "unknown" else "")


def _background_clients(doc: dict | None) -> list[str]:
    """Names of the background jobs on the GPU, when the light is fresh enough to say."""
    if not doc or doc.get("effective") not in ("green", "amber", "red"):
        return []
    entries = doc.get("background") or []
    return sorted({str(e.get("client") or "a background job") for e in entries if isinstance(e, dict)})


def claim(name: str, reason: str, *, timeout: int = CLAIM_WAIT_S, pid: int | None = None,
          wait: bool = True, on_wait: Callable[[str], None] | None = None) -> Outcome:
    """Claims the light as `name` and, with `wait`, blocks until no background job is on the GPU.

    A claim is a `boot` claim, or a `pid` claim bound to `pid`. A wait that times out leaves the
    claim in place, so the light stays red and the background job keeps leaving; Hal starts
    anyway with the warning. `on_wait` is called with a message before a wait that will block.
    """
    if not NAME_RE.match(name):
        raise ValueError(f"not a gpu-light claim name: {name!r}")
    if binary() is None:
        return Outcome()
    args = ["claim", "--name", name, "--reason", reason]
    if pid is not None:
        args += ["--pid", str(pid)]
    limit: float = CALL_TIMEOUT_S
    if wait:
        args += ["--wait", "--timeout", str(timeout)]
        limit = timeout + SLACK_S
        busy = _background_clients(status())
        if busy and on_wait:
            on_wait(f"waiting up to {timeout} s for {', '.join(busy)} to free the GPU")
    proc = _run(args, limit)
    if isinstance(proc, str):
        return Outcome(warning=proc)
    if proc.returncode == 0:
        return Outcome(ok=True)
    if proc.returncode == EXIT_TIMEOUT and wait:
        return Outcome(warning=f"the GPU is still busy after {timeout} s, so models may load slowly")
    return Outcome(warning=_failure(args, proc))


def release(name: str) -> Outcome:
    """Removes the claim `name`; succeeds when there is none."""
    if binary() is None:
        return Outcome()
    args = ["release", "--name", name]
    proc = _run(args, CALL_TIMEOUT_S)
    if isinstance(proc, str):
        return Outcome(warning=proc)
    return Outcome(ok=True) if proc.returncode == 0 else Outcome(warning=_failure(args, proc))


def _say(text: str) -> None:
    print(f"hal: {text}", file=sys.stderr, flush=True)


def main(argv: list[str]) -> int:
    """The shell scripts' way in. Exits 0 unless the usage is wrong: the light never stops Hal."""
    ap = argparse.ArgumentParser(prog="halcore gpulight")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim")
    c.add_argument("name")
    c.add_argument("--reason", default="hal")
    c.add_argument("--timeout", type=int, default=CLAIM_WAIT_S)
    c.add_argument("--pid", type=int)
    c.add_argument("--no-wait", action="store_true")
    c.add_argument("--unload-offloaded", action="store_true",
                   help="after a claim, unload Ollama models that sit partly in system RAM")
    r = sub.add_parser("release")
    r.add_argument("name")
    sub.add_parser("status")
    args = ap.parse_args(argv)
    if args.cmd == "claim":
        out = claim(args.name, args.reason, timeout=args.timeout, pid=args.pid,
                    wait=not args.no_wait, on_wait=_say)
        if out.ok and args.unload_offloaded:
            from . import power
            for model in power.unload_offloaded():
                _say(f"unloaded {model}, which sat partly in system RAM; it reloads on the GPU")
    elif args.cmd == "release":
        out = release(args.name)
    else:
        line = status_line()
        if line:
            print(line)
        return 0
    if out.warning:
        _say(out.warning)
    return 0
