"""`hal guard status|install|bench`: set up, inspect and time the local Prompt Guard 2 worker.

01-security "Taint, guard and reviewer" owns the guard (REQ-SEC-35, 36, 38); this module only
prepares the host side. Design decisions:

- `install` never downloads weights. Prompt Guard 2 is licence-gated on Hugging Face, so the
  person fetches `model.safetensors` and `tokenizer.json` themselves into
  `data_dir()/guard/<model>/` and `install` verifies, hashes and mounts them read-only. No token
  passes through Hal, and nothing runs on the host that a download could have changed.
- A model directory holding a pickle format is refused: loading one can run code (REQ-SEC-36).
- `install` records the worker command in `state_dir()/guard/worker.json` with the REQ-SEC-60
  flags; the relay (`halhub.guard.serve`) reads it. It does not enable `hal-guard.service`:
  starting units is the person's call, so the command prints it.
- Latency is measured through the relay, so the numbers include what a real scan costs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
from collections.abc import Sequence
from pathlib import Path

import halhub
from halhub.guard import GuardClient, GuardScanner, socket_path
from halhub.policy import load_policy
from halkit import files, paths, proc
from halkit.termsafe import escape
from halkit.timeutil import iso_z

IMAGE_TAG = "hal-guard:local"
WORKER_FILE = Path("guard") / "worker.json"
BENCH_FILE = Path("guard") / "bench.json"
BUILD_TIMEOUT_S = 1800  # cold build: base image plus about 1 GB of wheels
REQUIRED_FILES = ("model.safetensors", "tokenizer.json")
PICKLE_SUFFIXES = frozenset({".bin", ".pt", ".pth", ".pkl", ".ckpt"})
HASH_CHUNK = 1 << 20
BENCH_ROUNDS = 5
WINDOW_TEXT = "Please summarise the meeting notes and list the open actions. " * 8  # about 512 tokens
RESULT_TEXT = WINDOW_TEXT * 12  # a long tool result, several windows
YES_ANSWERS = frozenset({"y", "yes"})
# REQ-SEC-60 flags for the worker box; cpus and memory follow the guard image test
HARDENING = (
    "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
    "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "128",
    "--memory", "2g", "--memory-swap", "2g", "--cpus", "2",
)  # fmt: skip


def weights_problem(model_dir: Path) -> str | None:
    """Why `model_dir` cannot be mounted as the guard's weights, or None when it can."""
    if not model_dir.is_dir():
        return f"{model_dir} is not a directory; put the Hugging Face files there first"
    for name in REQUIRED_FILES:
        if not (model_dir / name).is_file():
            return f"{model_dir / name} is missing"
    pickled = sorted(p.name for p in model_dir.iterdir() if p.suffix in PICKLE_SUFFIXES)
    if pickled:
        return f"{model_dir} holds pickle files ({', '.join(pickled)}); loading one can run code"
    return None


def sha256_of(path: Path) -> str:
    """The hex sha256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def worker_argv(model_dir: Path, runtime: str, *, uid: int, gid: int) -> list[str]:
    """The docker command the relay runs: REQ-SEC-60 flags, host uid, weights mounted read-only."""
    mount = f"type=bind,src={model_dir},dst=/model,readonly"
    return [
        "docker", "run", "--rm", "-i", f"--runtime={runtime}", *HARDENING,
        "--user", f"{uid}:{gid}", "--mount", mount, IMAGE_TAG, "--model-dir", "/model",
    ]  # fmt: skip


def percentiles(samples: Sequence[int]) -> dict[str, int]:
    """`p50` and `p95` of `samples` in the same unit (milliseconds)."""
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {"p50": int(statistics.median(ordered)), "p95": p95}


def bench(scanner: GuardScanner, *, rounds: int = BENCH_ROUNDS) -> dict[str, object]:
    """Times `rounds` scans of one window and of one long result; raises when any scan errors."""
    out: dict[str, object] = {"at": iso_z()}
    for name, text in (("window", WINDOW_TEXT), ("result", RESULT_TEXT)):
        times: list[int] = []
        for _ in range(rounds):
            result = scanner.scan(text, source_tool="hal_guard_bench")
            if result.label == "error":
                raise RuntimeError(f"guard scan failed: {result.error}")
            times.append(result.ms)
        out[name] = percentiles(times)
    return out


def main(argv: Sequence[str]) -> int:
    """Runs `status`, `install` or `bench`; returns the exit status."""
    parser = argparse.ArgumentParser(prog="hal guard")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    install = sub.add_parser("install")
    install.add_argument("--model-dir")
    install.add_argument("--runtime", default="runc", choices=("runc", "runsc"))
    install.add_argument("--image-dir")
    sub.add_parser("bench")
    opts = parser.parse_args(list(argv))
    try:
        if opts.cmd == "status":
            return _status()
        if opts.cmd == "bench":
            return _bench()
        return _install(opts)
    except (RuntimeError, OSError, proc.ProcTimeout, ValueError) as exc:
        print(f"hal guard: {escape(str(exc))}", file=sys.stderr)
        return 1


def _settings():
    return load_policy().guard


def _status() -> int:
    settings = _settings()
    print(f"enabled: {'yes' if settings.enabled else 'no'} (set [guard] enabled = true in policy.toml)")
    print(f"model: {settings.model}")
    worker = paths.state_dir() / WORKER_FILE
    if worker.exists():
        record = json.loads(worker.read_text(encoding="utf-8"))
        print(f"worker: {escape(str(record.get('runtime')))} image {escape(str(record.get('image')))}")
        print(f"weights sha256: {escape(str(record.get('sha256')))}")
    else:
        print("worker: not installed (run `hal guard install`)")
    try:
        GuardClient(settings).check()
        print(f"relay: reachable at {socket_path()}")
    except Exception as exc:  # noqa: BLE001 - status reports every cause as text, it never raises
        print(f"relay: NOT reachable ({escape(str(exc))})")
    bench_file = paths.state_dir() / BENCH_FILE
    if bench_file.exists():
        data = json.loads(bench_file.read_text(encoding="utf-8"))
        for name in ("window", "result"):
            print(f"latency per {name}: p50 {data[name]['p50']} ms, p95 {data[name]['p95']} ms ({data['at']})")
    else:
        print("latency: not measured (run `hal guard bench`)")
    return 0


def _bench() -> int:
    settings = _settings()
    client = GuardClient(settings)
    client.check()
    data = bench(client)
    files.write_atomic(paths.state_dir() / BENCH_FILE, json.dumps(data, indent=2) + "\n")
    for name in ("window", "result"):
        print(f"latency per {name}: p50 {data[name]['p50']} ms, p95 {data[name]['p95']} ms")  # type: ignore[index]
    return 0


def _install(opts: argparse.Namespace) -> int:
    settings = _settings()
    model_dir = Path(opts.model_dir) if opts.model_dir else paths.data_dir() / "guard" / settings.model
    problem = weights_problem(model_dir)
    if problem:
        raise ValueError(problem)
    image_dir = Path(opts.image_dir) if opts.image_dir else Path(halhub.__file__).resolve().parents[2] / "guard-image"
    if not (image_dir / "Dockerfile").is_file():
        raise ValueError(f"{image_dir / 'Dockerfile'} not found; pass --image-dir")
    if not sys.stdin.isatty():
        raise ValueError("hal guard install needs a terminal to confirm; nothing was changed")
    print(f"Build {IMAGE_TAG} from {image_dir} (downloads about 1 GB of pinned wheels)")
    print("Type y and Enter to build it: ", end="", flush=True)
    if sys.stdin.readline().strip().lower() not in YES_ANSWERS:
        print("nothing changed")
        return 1
    proc.run(["docker", "build", "-q", "-t", IMAGE_TAG, str(image_dir)], timeout=BUILD_TIMEOUT_S)
    record = {
        "argv": worker_argv(model_dir.resolve(), opts.runtime, uid=os.getuid(), gid=os.getgid()),
        "runtime": opts.runtime,
        "image": IMAGE_TAG,
        "model": settings.model,
        "sha256": sha256_of(model_dir / "model.safetensors"),
        "installed": iso_z(),
    }
    files.write_atomic(paths.state_dir() / WORKER_FILE, json.dumps(record, indent=2) + "\n")
    print(f"wrote {paths.state_dir() / WORKER_FILE}")
    print("Next: start hal-guard.service (systemctl --user start hal-guard), run `hal guard bench`,")
    print("then set [guard] enabled = true in policy.toml.")
    return 0
