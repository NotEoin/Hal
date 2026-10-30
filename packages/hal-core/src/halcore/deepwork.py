"""Deep work: how a long task is handed on, and the queue of boxed jobs (RESOLUTIONS I4, I8).

`[agent].deep_work` is `off`, `queue` (default), `ask` or `claude`:

- `off`: a long task is refused with a reason, nothing is queued.
- `queue`: the task becomes a queued job for Hal's own boxed agent.
- `ask`: the same, after the person confirms.
- `claude`: hand it to Claude Code. Never a default and never a fallback: it needs the key set
  by the person, and the result is labelled "not private, not sandboxed".

The old `[brain].deep_work` still works when `[agent].deep_work` is unset: `claude` stays
`claude`, `off` stays `off`, and `hal` means `queue`. This module only decides and records: the
runner that starts queued jobs lives in `halcore.jobrunner`; a job stays `queued` until
`hal jobs run` starts it.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from halkit import config as kitconfig
from halkit import paths
from halkit.files import write_atomic
from halkit.timeutil import iso_z

log = logging.getLogger(__name__)

MODES = ("off", "queue", "ask", "claude")
DEFAULT_MODE = "queue"
LEGACY_BRAIN = {"hal": "queue", "off": "off", "claude": "claude"}
CLAUDE_LABEL = "not private, not sandboxed"
STATES = ("queued", "running", "done", "failed", "cancelled")
JOB_ID_BYTES = 6  # 12 hex characters, the same shape as a session id
MAX_TASK_CHARS = 20_000


def mode(config: Mapping[str, object] | None = None) -> str:
    """The deep-work mode from the config file; raises `ValueError` for an unknown value."""
    data = config if config is not None else kitconfig.read_toml(paths.config_file())
    agent = data.get("agent")
    value = agent.get("deep_work") if isinstance(agent, dict) else None
    if value is None:
        brain = data.get("brain")
        legacy = brain.get("deep_work") if isinstance(brain, dict) else None
        if legacy is not None:
            if legacy not in LEGACY_BRAIN:
                raise ValueError(f"[brain].deep_work = {legacy!r} is unknown; use hal, off or claude")
            return LEGACY_BRAIN[str(legacy)]
        return DEFAULT_MODE
    if value not in MODES:
        raise ValueError(f"[agent].deep_work = {value!r} is unknown; use one of {', '.join(MODES)}")
    return str(value)


@dataclass
class Job:
    """One queued unit of deep work."""

    id: str
    task: str
    project: str | None
    state: str = "queued"
    created: str = field(default_factory=iso_z)
    note: str = ""
    pid: int | None = None  # the worker process while the job runs
    started: str = ""
    finished: str = ""
    isolated_session: str | None = None  # the copy `hal code accept` or `export` takes
    result: str = ""  # tail of the agent's last reply


def jobs_dir() -> Path:
    """`state_dir()/jobs`, private to the user."""
    return paths.ensure_private_dir(paths.state_dir() / "jobs")


def _path(job_id: str) -> Path:
    if not job_id.isalnum() or len(job_id) != 2 * JOB_ID_BYTES:
        raise ValueError(f"bad job id {job_id!r}")
    return jobs_dir() / f"{job_id}.json"


def save(job: Job) -> None:
    """Writes a job record atomically."""
    write_atomic(_path(job.id), json.dumps(asdict(job), indent=2) + "\n")


def load(job_id: str) -> Job:
    """Reads a job; raises `ValueError` when it does not exist."""
    path = _path(job_id)
    if not path.is_file():
        raise ValueError(f"no job {job_id}")
    return Job(**json.loads(path.read_text(encoding="utf-8")))


def listing() -> list[Job]:
    """Every job, oldest first."""
    return sorted((load(p.stem) for p in jobs_dir().glob("*.json")), key=lambda j: j.created)


def cancel(job_id: str) -> Job:
    """Cancels a queued or running job; a running job's worker gets SIGTERM and closes its box."""
    job = load(job_id)
    if job.state not in ("queued", "running"):
        raise ValueError(f"job {job_id} is {job.state}; only a queued or running job can be cancelled")
    if job.state == "running" and job.pid is not None:
        from .jobrunner import stop_worker

        stop_worker(job.pid)
    job.state = "cancelled"
    job.finished = iso_z()
    save(job)
    return job


@dataclass(frozen=True)
class Routed:
    """The outcome of `submit`: a queued job, a Claude Code hand-off, or a refusal."""

    action: str  # "queued" | "claude" | "refused"
    job: Job | None = None
    reason: str = ""


def submit(
    task: str,
    project: Path | None = None,
    *,
    config: Mapping[str, object] | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> Routed:
    """Applies the deep-work mode to a long task. `claude` is reported, never run, from here."""
    if not task.strip() or len(task) > MAX_TASK_CHARS:
        raise ValueError(f"a task needs 1 to {MAX_TASK_CHARS} characters")
    chosen = mode(config)
    if chosen == "off":
        return Routed("refused", reason="deep_work is off: set [agent].deep_work to queue to allow background jobs")
    if chosen == "claude":
        return Routed("claude", reason=f"handing to Claude Code ({CLAUDE_LABEL}); chosen by [agent].deep_work")
    if chosen == "ask":
        if confirm is None or not confirm(f"Queue a background job for Hal's boxed agent?\n  {task[:200]}"):
            return Routed("refused", reason="not confirmed")
    job = Job(id=secrets.token_hex(JOB_ID_BYTES), task=task, project=str(project) if project else None)
    save(job)
    log.info("queued job %s", job.id)
    return Routed("queued", job=job)


def main(argv: list[str]) -> int:
    """`hal jobs [list|show ID|cancel ID|add TASK...]`; returns the exit status."""
    cmd = argv[0] if argv else "list"
    rest = argv[1:]
    try:
        if cmd == "list":
            rows = listing()
            for job in rows:
                print(f"{job.id}  {job.state:9} {job.created}  {job.task[:60]!r}")
            if not rows:
                print("(no jobs)")
        elif cmd in ("run", "follow", "accept", "export", "worker"):
            from . import pids
            from .jobrunner import command

            if cmd in ("run", "worker"):  # the long-running ones; `hal kill` stops them
                with pids.registered("jobs"):
                    return command(cmd, rest)
            return command(cmd, rest)
        elif cmd == "show" and rest:
            print(json.dumps(asdict(load(rest[0])), indent=2))
        elif cmd == "cancel" and rest:
            print(f"cancelled {cancel(rest[0]).id}")
        elif cmd == "add" and rest:
            routed = submit(
                " ".join(rest),
                Path.cwd(),
                confirm=lambda q: os.isatty(0) and input(q + " [y/N] ").strip().lower() == "y",
            )
            print(f"queued {routed.job.id}; `hal jobs run` starts it" if routed.job else routed.reason)
            return 0 if routed.action == "queued" else 3
        else:
            print("usage: hal jobs [list | show ID | cancel ID | add TASK... | run [--wait] | follow ID | accept ID | export ID FILE]")
            return 2
    except ValueError as exc:
        print(f"hal jobs: {exc}")
        return 1
    return 0
