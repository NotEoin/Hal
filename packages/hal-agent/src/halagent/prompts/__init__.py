"""Rendering of the coding system prompt from the templates in this directory.

Schedule numbers are fractions of the gemma harness schedule (6, 12, 20 and 25 of 28 calls), so
they scale with the real budget. Placeholders use `{name}` and are filled by plain replacement,
because the tool block contains literal JSON braces.
"""

from __future__ import annotations

from importlib import resources

from halagent.budget import Budget

LOCALIZE_FRACTION = 0.21  # calls 1..6 of 28 find the code (03-agent 3.11.2)
FIRST_EDIT_FRACTION = 0.43  # first edit by call 12 of 28
CHECK_FRACTION = 0.71  # checks end at call 20 of 28
FINISH_MARGIN = 3  # the last three calls only finish
MAX_BASE_CHARS = 6_400  # 1,600 tokens by the 4-character heuristic (REQ-AGT-70)


def _read(name: str) -> str:
    return resources.files("halagent.prompts").joinpath(name).read_text("utf-8").rstrip("\n")


def _fill(template: str, values: dict[str, object]) -> str:
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def schedule(budget: Budget) -> dict[str, int]:
    """The call numbers of the task-mode schedule for `budget`."""
    n = budget.max_tool_calls
    first_edit_by = max(1, round(FIRST_EDIT_FRACTION * n))
    check_until = max(first_edit_by + 1, round(CHECK_FRACTION * n))
    return {
        "localize_until": max(1, round(LOCALIZE_FRACTION * n)),
        "first_edit_by": first_edit_by,
        "check_from": first_edit_by + 1,
        "check_until": check_until,
        "finish_from": max(check_until + 1, n - FINISH_MARGIN),
    }


def render_code_prompt(
    budget: Budget,
    *,
    task: bool,
    workdir: str,
    sandbox: str = "box",
    extras: str = "",
    tool_lines: str = "",
) -> str:
    """The system prompt: base template, then project instructions and skills, then tools.

    `extras` is the already rendered instructions and skills text; `tool_lines` is non-empty
    only for `tool_format = "prompt"`.
    """
    values: dict[str, object] = {
        "workdir": workdir,
        "sandbox": sandbox,
        "max_tool_calls": budget.max_tool_calls,
        "minutes": max(1, round(budget.max_wall_s / 60)),
        "warn_calls": budget.warn_calls,
        **schedule(budget),
    }
    template = _read("code_task.md" if task else "code_interactive.md")
    parts = [_fill(_fill(template, {"common": _read("code_common.md"), "style": _read("style.md")}), values)]
    if extras:
        parts.append(extras)
    if tool_lines:
        parts.append(_fill(_read("tool_prompt.md"), {"tool_lines": tool_lines}))
    return "\n\n".join(parts)
