"""Runs queued deep-work jobs in boxed `job` sessions (RESOLUTIONS I3, I8).

`hal jobs run` reconciles the queue, then starts the oldest queued job when none is running. The
job's worker is `python -m halcore jobs worker ID`. With gpu-light installed the worker is
submitted as class `batch`, so it yields the GPU to foreground use; otherwise it is a detached
host process. Either way the worker records its pid in the job file, so a restart of the machine
or of `hal` is survived: `reconcile` puts a job whose worker is gone back in the queue.

The worker runs the task as one ACP turn in a box on an isolated copy of the project, writes every
update as a JSON line to `jobs/<id>.log` (what `hal jobs follow` shows), and stores the copy's
session id on the job. Changes leave only through `hal jobs accept` or `hal jobs export`, which
use the same host-side review as `hal code`. The broker hears no human message for a job: the task
text may come from the model, so it is never trusted as the person's words.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from halkit import termsafe
from halkit.timeutil import iso_z
from halsandbox.errors import SandboxError

from . import deepwork, sessions
from .acp.client import AcpClient
from .acp.transport import AgentCrashed
from .chat import ChatHandler

log = logging.getLogger(__name__)

JOB_MAX_S = 6 * 3600  # one job never holds a box longer than this
TAIL_CHARS = 2000  # how much of the last reply the job record keeps
SUBMIT_TIMEOUT_S = 30  # gpu-light answers a submit at once
FOLLOW_POLL_S = 0.5
WORKER_ARGS = ("-m", "halcore", "jobs", "worker")
TERMINAL = ("done", "failed", "cancelled")


def log_path(job_id: str) -> Path:
    """`jobs/<id>.log`, the JSON-lines update log of a job."""
    return deepwork.jobs_dir() / f"{job_id}.log"


def worker_alive(pid: int | None) -> bool:
    """True when `pid` is a live `halcore` worker (a reused pid of another program counts as gone)."""
    if not pid:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"halcore" in cmdline and b"worker" in cmdline


def stop_worker(pid: int) -> None:
    """Asks a worker to stop; its handler closes the box."""
    if worker_alive(pid):
        os.kill(pid, signal.SIGTERM)


def reconcile() -> list[str]:
    """Puts running jobs whose worker is gone back in the queue; returns their ids."""
    back = []
    for job in deepwork.listing():
        if job.state == "running" and not worker_alive(job.pid):
            job.state, job.pid, job.note = "queued", None, "requeued: the worker was gone"
            deepwork.save(job)
            back.append(job.id)
    return back


def _worker_argv(job_id: str) -> list[str]:
    return [sys.executable, *WORKER_ARGS, job_id]


def launch(job: deepwork.Job) -> str:
    """Starts the worker for `job` under gpu-light class `batch`, or detached; returns how."""
    argv = _worker_argv(job.id)
    exe = shutil.which(os.environ.get("HAL_GPU_LIGHT") or "gpu-light")
    if exe and os.environ.get("HAL_GPU_LIGHT", "").lower() not in ("off", "0", "no", "false"):
        proc = subprocess.run(
            [exe, "submit", "--class", "batch", "--name", f"hal-job-{job.id}", "--label", "app=hal", "--", *argv],
            capture_output=True, text=True, timeout=SUBMIT_TIMEOUT_S, check=False,
        )  # fmt: skip
        if proc.returncode == 0:
            return "gpu-light"
        log.warning("gpu-light refused the job (%s); starting it directly", proc.stderr.strip()[:200])
    out = log_path(job.id).with_suffix(".out").open("ab")
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)  # noqa: S603 - our own argv
    return "direct"


def dispatch() -> str | None:
    """Reconciles, then launches the oldest queued job when none runs; returns its id."""
    reconcile()
    jobs = deepwork.listing()
    if any(j.state == "running" for j in jobs):
        return None
    for job in jobs:
        if job.state == "queued":
            how = launch(job)
            log.info("job %s launched (%s)", job.id, how)
            return job.id
    return None


class JobHandler(ChatHandler):
    """Writes every update to the job log as one JSON line and keeps the reply text."""

    def __init__(self, job_id: str) -> None:
        super().__init__(quiet=True)
        self.log = log_path(job_id).open("a", encoding="utf-8")

    async def on_update(self, session_id: str, update: dict[str, object]) -> None:
        """Logs the update (JSON escapes control characters) and collects message text."""
        self.log.write(json.dumps({"t": iso_z(), "update": update}) + "\n")
        self.log.flush()
        await super().on_update(session_id, update)


async def _run_job(job: deepwork.Job) -> str:
    project = Path(job.project) if job.project else None
    box = await sessions.BoxedAgent.start(kind="job", project=project, channel="job")
    handler = JobHandler(job.id)
    client = AcpClient(box.transport, handler)
    try:
        if box.session.iso is not None:
            job.isolated_session = box.session.iso.sid
            deepwork.save(job)
        await client.initialize()
        sid = await client.new_session(sessions.BOX_WORK if project else sessions.BOX_SCRATCH, [])
        reason = await asyncio.wait_for(client.prompt(sid, [{"type": "text", "text": job.task}]), JOB_MAX_S)
        job.result = "".join(handler.text)[-TAIL_CHARS:]
        return reason
    finally:
        handler.log.close()
        await client.close()
        await box.close()


def worker(job_id: str) -> int:
    """Runs one job to its end and records the outcome; the entry point of a worker process."""
    job = deepwork.load(job_id)
    if job.state != "queued":
        print(f"hal jobs worker: job {job_id} is {job.state}", file=sys.stderr)
        return 1
    job.state, job.pid, job.started, job.note = "running", os.getpid(), iso_z(), ""
    deepwork.save(job)
    loop = asyncio.new_event_loop()
    task = loop.create_task(_run_job(job))
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        reason = loop.run_until_complete(task)
        state, note = ("done", "") if reason == "end_turn" else ("failed", f"stop reason {reason}")
    except asyncio.CancelledError:
        state, note = "cancelled", "stopped by the person"
    except (SandboxError, AgentCrashed, TimeoutError, OSError) as exc:
        state, note = "failed", str(exc)[:500]
    finally:
        loop.close()
    latest = deepwork.load(job_id)  # `cancel` may have written the final state already
    latest.pid, latest.finished = None, iso_z()
    latest.isolated_session, latest.result = job.isolated_session, job.result
    if latest.state != "cancelled":
        latest.state, latest.note = state, note
    deepwork.save(latest)
    return 0 if state == "done" else 1


def follow(job_id: str, out=None, *, poll: float = FOLLOW_POLL_S) -> int:  # noqa: ANN001 - any text stream
    """Prints a job's updates as they arrive until the job ends; returns 0 when it finished well."""
    out = out or sys.stdout
    path, pos = log_path(job_id), 0
    deepwork.load(job_id)
    while True:
        if path.is_file():
            with path.open(encoding="utf-8") as f:
                f.seek(pos)
                for line in f:
                    if not line.endswith("\n"):
                        break
                    pos += len(line.encode("utf-8"))
                    print(_render(line), end="", file=out, flush=True)
        job = deepwork.load(job_id)
        if job.state in TERMINAL:
            print(f"\n[{job.state}] {termsafe.escape(job.note)}", file=out)
            return 0 if job.state == "done" else 1
        time.sleep(poll)


def _render(line: str) -> str:
    update = json.loads(line).get("update", {})
    kind = update.get("sessionUpdate")
    if kind == "agent_message_chunk" and isinstance(update.get("content"), dict):
        return termsafe.escape(str(update["content"].get("text", "")), multiline=True)
    if kind == "tool_call":
        return "\n[tool] " + termsafe.escape(str(update.get("title", ""))) + "\n"
    return ""


def command(cmd: str, rest: list[str]) -> int:
    """`hal jobs run|follow|accept|export|worker ...`; returns the exit status."""
    try:
        if cmd == "worker" and rest:
            return worker(rest[0])
        if cmd == "run":
            started = dispatch()
            print(f"started job {started}" if started else "nothing to start")
            while "--wait" in rest and (started or any(j.state == "running" for j in deepwork.listing())):
                time.sleep(FOLLOW_POLL_S * 4)
                started = dispatch()
            return 0
        if cmd == "follow" and rest:
            return follow(rest[0])
        if cmd in ("accept", "export") and rest:
            job = deepwork.load(rest[0])
            if job.isolated_session is None:
                raise ValueError(f"job {job.id} has no isolated copy yet (state {job.state})")
            from .code import review

            return review([cmd, job.isolated_session, *rest[1:]])
    except (ValueError, SandboxError) as exc:
        print(f"hal jobs {cmd}: {exc}", file=sys.stderr)
        return 1
    print(f"usage: hal jobs {cmd} ...", file=sys.stderr)
    return 2
