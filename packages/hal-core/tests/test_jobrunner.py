"""Tests for the boxed job runner: worker outcome, restart survival, follow, accept routing."""

import io
import sys
import types

import pytest

from halcore import deepwork, jobrunner, sessions
from halcore.acp.transport import SubprocessTransport
from test_acp_client import FAKE_AGENT


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_STATE", str(tmp_path / "state"))
    return tmp_path


def fake_job_box(tmp_path, monkeypatch, mode="ok", log=None):
    script = tmp_path / "agent.py"
    script.write_text(FAKE_AGENT)

    async def start(**kwargs):
        if log is not None:
            log.append(kwargs)
        transport = await SubprocessTransport.spawn(
            [sys.executable, str(script), mode], env=None, stderr_log=tmp_path / "stderr.log"
        )
        iso = types.SimpleNamespace(sid="aabbccddeeff")
        session = types.SimpleNamespace(iso=iso, id="0123456789ab", broker=None)
        return types.SimpleNamespace(session=session, transport=transport, close=transport.close)

    monkeypatch.setattr(sessions.BoxedAgent, "start", staticmethod(start))


def queued(state, task="do it"):
    return deepwork.submit(task, state, config={}).job


def test_worker_runs_the_task_in_a_job_box_and_records_the_copy(state, monkeypatch):
    log = []
    fake_job_box(state, monkeypatch, log=log)
    job = queued(state)
    assert jobrunner.worker(job.id) == 0
    done = deepwork.load(job.id)
    assert done.state == "done" and done.isolated_session == "aabbccddeeff" and done.result == "hi"
    assert done.pid is None and done.finished
    assert log[0]["kind"] == "job" and log[0]["project"] == state
    assert "agent_message_chunk" in jobrunner.log_path(job.id).read_text()


def test_worker_marks_a_refused_box_failed_with_the_reason(state, monkeypatch):
    from halsandbox.errors import SandboxError

    async def refuse(**kwargs):
        raise SandboxError("docker answered before and does not now")

    monkeypatch.setattr(sessions.BoxedAgent, "start", staticmethod(refuse))
    job = queued(state)
    assert jobrunner.worker(job.id) == 1
    failed = deepwork.load(job.id)
    assert failed.state == "failed" and "docker answered" in failed.note


def test_worker_refuses_a_job_that_is_not_queued(state):
    job = queued(state)
    deepwork.cancel(job.id)
    assert jobrunner.worker(job.id) == 1


def test_running_job_with_a_dead_worker_is_requeued_after_a_restart(state):
    job = queued(state)
    job.state, job.pid = "running", 999_999_999
    deepwork.save(job)
    assert jobrunner.reconcile() == [job.id]
    assert deepwork.load(job.id).state == "queued"


def test_dispatch_starts_one_job_at_a_time(state, monkeypatch):
    first, second = queued(state, "a"), queued(state, "b")
    first.created = "2026-10-30T10:00:00Z"
    second.created = "2026-10-30T10:00:01Z"
    deepwork.save(first)
    deepwork.save(second)
    launched = []
    monkeypatch.setattr(jobrunner, "launch", lambda j: launched.append(j.id) or "direct")
    assert jobrunner.dispatch() == first.id
    running = deepwork.load(first.id)
    running.state, running.pid = "running", __import__("os").getpid()
    deepwork.save(running)
    monkeypatch.setattr(jobrunner, "worker_alive", lambda pid: True)
    assert jobrunner.dispatch() is None and launched == [first.id]
    assert deepwork.load(second.id).state == "queued"


def test_follow_prints_the_reply_until_the_job_is_done(state, monkeypatch):
    fake_job_box(state, monkeypatch)
    job = queued(state)
    jobrunner.worker(job.id)
    out = io.StringIO()
    assert jobrunner.follow(job.id, out, poll=0.01) == 0
    assert "hi" in out.getvalue() and "[done]" in out.getvalue()


def test_follow_escapes_terminal_codes_from_the_agent(state):
    job = queued(state)
    update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "a\x1b[2Jb"}}
    jobrunner.log_path(job.id).write_text(__import__("json").dumps({"t": "x", "update": update}) + "\n")
    job.state = "failed"
    deepwork.save(job)
    out = io.StringIO()
    jobrunner.follow(job.id, out, poll=0.01)
    assert "\x1b" not in out.getvalue()


def test_accept_applies_the_jobs_isolated_copy_through_the_host_review(state, monkeypatch):
    job = queued(state)
    job.isolated_session = "aabbccddeeff"
    deepwork.save(job)
    seen = []
    monkeypatch.setattr("halcore.code.review", lambda argv: seen.append(argv) or 0)
    assert jobrunner.command("accept", [job.id, "--allow-flags"]) == 0
    assert seen == [["accept", "aabbccddeeff", "--allow-flags"]]


def test_accept_without_a_copy_says_so(state, capsys):
    job = queued(state)
    assert jobrunner.command("accept", [job.id]) == 1
    assert "no isolated copy" in capsys.readouterr().err


def test_cancelling_a_running_job_stops_its_worker(state, monkeypatch):
    job = queued(state)
    job.state, job.pid = "running", 4242
    deepwork.save(job)
    stopped = []
    monkeypatch.setattr(jobrunner, "stop_worker", stopped.append)
    deepwork.cancel(job.id)
    assert stopped == [4242] and deepwork.load(job.id).state == "cancelled"
