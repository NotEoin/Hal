"""The CI workflow runs what REQ-SYS-21 requires: ruff, the ASCII gate, the Python matrix.

The workflow is YAML and the standard library has no YAML reader, so these tests read the text.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
PYTHONS = ("3.11", "3.12", "3.13")  # REQ-SYS-02


def test_workflow_tests_every_supported_python():
    matrix = re.search(r"python: \[(.*?)\]", WORKFLOW)
    assert matrix and tuple(re.findall(r'"([\d.]+)"', matrix.group(1))) == PYTHONS


def test_workflow_runs_ruff_check_and_format_check():
    assert "ruff check ." in WORKFLOW
    assert "ruff format --check" in WORKFLOW


def test_workflow_runs_the_ascii_gate_and_the_tests():
    assert "scripts/check-ascii.sh" in WORKFLOW
    assert "uv run pytest" in WORKFLOW


def test_workflow_installs_from_the_lock_file_without_changing_it():
    assert WORKFLOW.count("uv sync --all-packages --frozen") == 2


def test_workflow_token_is_read_only():
    assert re.search(r"^permissions:\n  contents: read$", WORKFLOW, re.M)


def test_every_job_has_a_timeout():
    jobs = WORKFLOW.split("\njobs:\n", 1)[1]
    assert len(re.findall(r"^  [a-z]+:\n", jobs, re.M)) == len(re.findall(r"timeout-minutes: \d+", jobs))


def test_workflow_does_not_run_marked_tests():
    assert "-m " not in WORKFLOW  # the deselection lives in pyproject.toml, once
