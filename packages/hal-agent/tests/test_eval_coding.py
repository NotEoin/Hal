"""Tests for `halagent.eval.coding`: task format, gold/null check and the attempt pipeline."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from halagent.eval.coding import AgentRun, CheckResult, TaskError, gold_null_check, load_tasks, run_coding
from halagent.settings import AgentSettings

TASKS = Path(__file__).parents[3] / "eval" / "coding" / "tasks"


class LocalRunner:
    """Runs checks with bash in the temp copy (no box); the 'agent' applies a canned edit."""

    def __init__(self, edit=None):
        self.edit = edit

    def run_agent(self, workspace, task, settings, timeout_s):
        if self.edit:
            self.edit(workspace)
        return AgentRun("finished", 1.5, model_requests=3, tool_calls=2, first_edit_call=1)

    def run_check(self, workspace, command, timeout_s):
        env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"}
        done = subprocess.run(["bash", "-c", command], cwd=workspace, env=env, capture_output=True, text=True,
                              timeout=timeout_s, check=False)  # fmt: skip
        return CheckResult(done.returncode, done.stdout[-500:])


def test_shipped_tasks_load_and_split_by_name():
    tasks = load_tasks(TASKS)
    assert {t.id for t in tasks} >= {"csv-quote", "leap-century", "dedupe-stable"}
    assert all(t.license == "MIT" for t in tasks)
    assert {t.id for t in load_tasks(TASKS, "heldout")} == {
        "dedupe-stable",
        "lru-evict",
        "rename-api",
        "semver-prerelease",
    }


def test_task_set_has_twelve_tasks_with_gold_patches():
    tasks = load_tasks(TASKS)
    assert len(tasks) == 12
    assert all((TASKS / t.id / "gold.patch").read_text().startswith("diff --git") for t in tasks)


@pytest.mark.parametrize("task", load_tasks(TASKS), ids=lambda t: t.id)
def test_gold_patch_passes_and_empty_patch_fails(task):
    assert gold_null_check(task, LocalRunner()) == (True, True)


def test_unknown_task_key_fails_naming_it(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "task.toml").write_text('id = "x"\nbogus = 1\n')
    with pytest.raises(TaskError, match="x"):
        load_tasks(tmp_path)


def _fix_leap(work):
    path = work / "calendar_utils" / "leap.py"
    path.write_text(
        path.read_text().replace(
            "year % 4 == 0\n", "year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)\n"
        )
    )


def test_resolved_attempt_is_graded_from_its_patch_and_recorded(tmp_path):
    task = [t for t in load_tasks(TASKS) if t.id == "leap-century"]
    report = run_coding("m", task, repeat=2, seed=10, runner=LocalRunner(_fix_leap), out_dir=tmp_path / "o",
                        settings=AgentSettings(endpoint="x"))  # fmt: skip
    a = report.attempts[0]
    assert (a.resolved, a.empty_patch, a.seed, a.patch_lines, a.first_edit_call) == (True, False, 10, 2, 1)
    assert [x.seed for x in report.attempts] == [10, 11]
    assert (tmp_path / "o" / "results.jsonl").read_text().count("\n") == 2
    assert report.summary()["resolved"] == 2


def test_agent_that_changes_nothing_is_an_empty_patch_and_not_resolved(tmp_path):
    task = [t for t in load_tasks(TASKS) if t.id == "csv-quote"]
    report = run_coding("m", task, repeat=1, seed=0, runner=LocalRunner(), out_dir=tmp_path,
                        settings=AgentSettings(endpoint="x"))  # fmt: skip
    a = report.attempts[0]
    assert (a.resolved, a.empty_patch, a.check_exit != 0) == (False, True, True)


def test_edit_to_a_hidden_test_cannot_make_the_check_pass(tmp_path):
    def cheat(work):
        (work / "hidden").mkdir()
        (work / "hidden" / "test_leap.py").write_text("def test_x():\n    assert True\n")

    task = [t for t in load_tasks(TASKS) if t.id == "leap-century"]
    report = run_coding("m", task, repeat=1, seed=0, runner=LocalRunner(cheat), out_dir=tmp_path,
                        settings=AgentSettings(endpoint="x"))  # fmt: skip
    a = report.attempts[0]
    assert a.stop_reason == "finished" and not a.resolved and a.check_exit != 0


def test_a_runner_that_raises_fails_the_attempt_not_the_run(tmp_path):
    class Broken(LocalRunner):
        def run_agent(self, *args):
            raise RuntimeError("box did not start")

    task = load_tasks(TASKS)[:1]
    report = run_coding("m", task, repeat=1, seed=0, runner=Broken(), out_dir=tmp_path,
                        settings=AgentSettings(endpoint="x"))  # fmt: skip
    assert (
        report.attempts[0].stop_reason.startswith("error: box did not start")
        and not report.attempts[0].resolved
    )


def test_agent_run_reads_counters_from_the_headless_result():
    result = {
        "stop_reason": "done", "num_model_requests": 4, "num_tool_calls": 3,
        "stats": {"first_edit_call": 2, "peak_prompt_tokens": 900, "counters": {"repaired": 2}},
    }  # fmt: skip
    run = AgentRun.from_result(result, 3.0)
    assert (run.model_requests, run.first_edit_call, run.counters) == (4, 2, {"repaired": 2})
