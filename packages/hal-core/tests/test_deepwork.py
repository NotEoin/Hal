"""Tests for `halcore.deepwork`: modes, the default and the job queue (RESOLUTIONS I4, I8)."""

import pytest
from halkit.testing import TempHalHome

from halcore import deepwork


@pytest.fixture
def home():
    with TempHalHome() as h:
        yield h


def test_queue_is_the_default_and_claude_is_never_chosen_without_the_key(home):
    assert deepwork.mode({}) == "queue"
    assert deepwork.mode({"agent": {}, "brain": {}}) == "queue"


def test_legacy_brain_key_maps_and_agent_key_wins():
    assert deepwork.mode({"brain": {"deep_work": "hal"}}) == "queue"
    assert deepwork.mode({"brain": {"deep_work": "claude"}}) == "claude"
    assert deepwork.mode({"agent": {"deep_work": "off"}, "brain": {"deep_work": "claude"}}) == "off"


def test_unknown_mode_fails_loudly():
    with pytest.raises(ValueError, match="unknown"):
        deepwork.mode({"agent": {"deep_work": "cloud"}})


def test_queue_mode_records_a_queued_job(home):
    routed = deepwork.submit("refactor the parser", config={})
    assert routed.action == "queued" and deepwork.load(routed.job.id).state == "queued"
    assert [j.id for j in deepwork.listing()] == [routed.job.id]


def test_off_mode_queues_nothing(home):
    routed = deepwork.submit("x", config={"agent": {"deep_work": "off"}})
    assert routed.action == "refused" and deepwork.listing() == []


def test_ask_mode_queues_only_after_confirmation(home):
    cfg = {"agent": {"deep_work": "ask"}}
    assert deepwork.submit("x", config=cfg, confirm=lambda q: False).action == "refused"
    assert deepwork.submit("x", config=cfg).action == "refused"
    assert deepwork.submit("x", config=cfg, confirm=lambda q: True).action == "queued"


def test_claude_mode_is_reported_with_its_label_and_queues_nothing(home):
    routed = deepwork.submit("x", config={"agent": {"deep_work": "claude"}})
    assert routed.action == "claude" and "not private, not sandboxed" in routed.reason
    assert deepwork.listing() == []


def test_only_a_queued_job_can_be_cancelled(home):
    job = deepwork.submit("x", config={}).job
    assert deepwork.cancel(job.id).state == "cancelled"
    with pytest.raises(ValueError, match="cancelled"):
        deepwork.cancel(job.id)


def test_job_ids_cannot_escape_the_jobs_folder(home):
    with pytest.raises(ValueError, match="bad job id"):
        deepwork.load("../../etc/passwd")
