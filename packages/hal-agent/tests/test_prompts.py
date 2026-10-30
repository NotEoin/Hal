"""Tests for `halagent.prompts`: schedule maths, rendering, size and the prompt lint."""

import re

import pytest

from halagent.budget import Budget
from halagent.prompts import MAX_BASE_CHARS, render_code_prompt, schedule
from halagent.tools import Toolset


def budget(n=50, wall=900):
    return Budget(max_tool_calls=n, max_model_requests=120, max_wall_s=wall)


@pytest.mark.parametrize(
    "n, expected",
    [(28, (6, 12, 13, 20, 25)), (50, (10, 22, 23, 36, 47))],
)
def test_schedule_scales_the_gemma_numbers(n, expected):
    s = schedule(budget(n))
    keys = ("localize_until", "first_edit_by", "check_from", "check_until", "finish_from")
    assert tuple(s[k] for k in keys) == expected


def test_task_prompt_states_the_computed_budget():
    text = render_code_prompt(budget(), task=True, workdir="/work")
    assert "50 tool calls and 15 minutes" in text and "Calls 1 to 10" in text and "From call 47" in text
    assert "{" not in text.replace('{"', "")  # no unfilled placeholder


def test_interactive_prompt_has_no_budget_or_finish_steps():
    text = render_code_prompt(budget(), task=False, workdir="/work")
    assert "## Budget" not in text and "call finish" not in text and "## Answering" in text


def test_prompt_puts_scratch_files_in_scratch_not_tmp():
    assert "/scratch" in render_code_prompt(budget(), task=True, workdir="/work")


@pytest.mark.parametrize("task", [True, False])
def test_base_prompt_stays_under_the_token_cap(task):
    assert len(render_code_prompt(budget(), task=task, workdir="/work")) < MAX_BASE_CHARS


def test_extras_and_tool_block_come_after_the_base():
    text = render_code_prompt(
        budget(), task=False, workdir="/w", extras="## Skills\nx", tool_lines="- read(): r"
    )
    assert text.index("## Working style") < text.index("## Skills") < text.index("## Tools")
    assert text.endswith("- read(): r")


def test_prompt_does_not_write_tool_calls_as_shell_commands(tmp_path):
    names = "|".join(Toolset(tmp_path, task_mode=True).tools)
    rx = re.compile(rf"^\s*(?:{names})\s*[({{]|`(?:{names})\s*[({{ ]", re.M)
    for task in (True, False):
        assert not rx.search(render_code_prompt(budget(), task=task, workdir="/w"))
