"""`setup.sh` downloads no model unless `--pull-suggested` is given (REQ-CLI-114).

The script installs software and runs `sudo`, so it is checked by reading it, not by running it.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "setup.sh"
GUARD = re.compile(r'\[ "\$PULL_SUGGESTED" -eq 1 \]')


def code_lines() -> list[tuple[int, str]]:
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    return [(i, line) for i, line in enumerate(lines) if not line.lstrip().startswith("#")]


def test_pull_suggested_is_off_unless_the_flag_is_given():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "PULL_SUGGESTED=0" in text
    assert re.search(r"--pull-suggested\) PULL_SUGGESTED=1", text)


def test_ollama_pull_appears_only_inside_the_opt_in_function():
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("pull_suggested_models()"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    outside = [(i + 1, line) for i, line in code_lines() if "ollama pull" in line and not start <= i <= end]
    assert outside == []


def test_every_call_that_downloads_models_sits_behind_the_flag():
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    calls = [
        i
        for i, line in code_lines()
        if re.match(r"\s+(pull_suggested_models|download_max_models \|\|)", line)
    ]
    assert calls, "the opt-in calls disappeared"
    for i in calls:
        assert GUARD.search(lines[i - 1]) or GUARD.search(lines[i - 2]), (
            f"line {i + 1} is not behind the flag"
        )


def test_ollama_create_runs_only_for_a_model_the_flag_pulled():
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    guard = next(
        i
        for i, line in code_lines()
        if "NEEDS_LOCAL_LLM" in line and "if [" in line and "PULL_SUGGESTED" in line
    )
    assert guard < next(i for i, line in code_lines() if "ollama create" in line)
    assert 'PULL_SUGGESTED" -eq 1' in lines[guard]


def test_setup_sh_has_valid_bash_syntax():
    import subprocess

    done = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
