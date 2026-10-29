"""Style gates for the whole repository.

Part 1 (REQ-SYS-20): `scripts/check-ascii.sh` passes on this checkout, and the script itself
reports what it should, tested on throwaway git repositories.

Part 2 (REQ-SYS-14, REQ-SYS-15): an AST scan of every `packages/*/src` file for timestamps that
bypass `halkit.timeutil` and subprocess calls without a timeout. Code that predates the
monorepo is listed in `LEGACY` with the phase that rewrites it; the test fails on any
violation not listed and on any listed entry that no longer occurs, so the list only shrinks.
"""

from __future__ import annotations

import ast
import collections
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from halkit import proc

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-ascii.sh"
EM_DASH = chr(0x2014)  # built from the code point so this file stays ASCII
E_ACUTE = chr(0xE9)
GATE_TIMEOUT_S = 60  # the script runs git and grep over a few hundred files


def run_gate(root: Path) -> subprocess.CompletedProcess[bytes]:
    return proc.run(["bash", str(root / "scripts" / "check-ascii.sh")], timeout=GATE_TIMEOUT_S, check=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small git repository holding a copy of the gate script."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "check-ascii.sh")
    proc.run(["git", "init", "-q", str(tmp_path)], timeout=GATE_TIMEOUT_S)
    return tmp_path


def test_this_checkout_passes_the_ascii_gate():
    result = run_gate(REPO)
    assert result.returncode == 0, result.stderr.decode()


def test_ascii_text_passes(repo):
    (repo / "a.md").write_text("plain text\n")
    result = run_gate(repo)
    assert result.returncode == 0 and b"ok" in result.stdout


def test_non_ascii_text_fails_naming_file_and_line(repo):
    (repo / "a.md").write_text(f"fine\nbad {EM_DASH} here\n")
    result = run_gate(repo)
    assert result.returncode == 1
    assert b"check-ascii: a.md:2:" in result.stderr


def test_non_ascii_in_a_tracked_file_fails(repo):
    (repo / "a.py").write_text(f"x = '{E_ACUTE}'\n")
    proc.run(["git", "-C", str(repo), "add", "a.py"], timeout=GATE_TIMEOUT_S)
    assert run_gate(repo).returncode == 1


def test_non_ascii_file_name_fails(repo):
    (repo / f"caf{E_ACUTE}.txt").write_text("ascii content\n")
    result = run_gate(repo)
    assert result.returncode == 1 and b"non-ASCII file name" in result.stderr


def test_exempt_file_is_skipped_with_its_reason_comment(repo):
    (repo / "README.md").write_text(f"{EM_DASH} badge\n")
    (repo / "scripts" / "ascii-exempt.txt").write_text("# header\nREADME.md  # public README\n")
    assert run_gate(repo).returncode == 0


def test_exemption_covers_only_the_named_file(repo):
    (repo / "README.md").write_text(f"{EM_DASH}\n")
    (repo / "OTHER.md").write_text(f"{EM_DASH}\n")
    (repo / "scripts" / "ascii-exempt.txt").write_text("README.md  # public README\n")
    result = run_gate(repo)
    assert result.returncode == 1 and b"OTHER.md" in result.stderr and b"README.md:" not in result.stderr


def test_binary_files_are_skipped(repo):
    (repo / "pic.gif").write_bytes(b"GIF89a\x00\xff\xfe\x80\x81")
    assert run_gate(repo).returncode == 0


def test_ignored_files_are_not_checked(repo):
    (repo / ".gitignore").write_text("build/\n")
    (repo / "build").mkdir()
    (repo / "build" / "out.txt").write_text(f"{EM_DASH}\n")
    assert run_gate(repo).returncode == 0


def test_outside_a_git_checkout_the_gate_refuses_to_run(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "check-ascii.sh")
    assert run_gate(tmp_path).returncode == 2


# ---- Part 2: timestamps and subprocess timeouts ----------------------------------------------

NAIVE_NOW = "naive-now"  # datetime.now() without a timezone, utcnow(), today()
STRFTIME = "time-strftime"  # time.strftime builds a local-time stamp
NO_TIMEOUT = "subprocess-no-timeout"
NAIVE_CALLS = frozenset(
    {"datetime.utcnow", "datetime.datetime.utcnow", "datetime.today", "datetime.datetime.today"}
)
SUBPROCESS_CALLS = frozenset({"run", "check_output", "call", "check_call"})
# Remaining violations in code moved from before the monorepo: (file, rule) -> count. Each goes
# when the owning spec rewrites the module (halhub in 04-hub, halcore in 06-core-cli).
LEGACY: dict[tuple[str, str], int] = {
    ("packages/hal-core/src/halcore/brain.py", NAIVE_NOW): 2,
    ("packages/hal-core/src/halcore/console.py", NAIVE_NOW): 1,
    ("packages/hal-core/src/halcore/evaluate.py", NAIVE_NOW): 1,
    ("packages/hal-core/src/halcore/evaluate.py", STRFTIME): 1,
    ("packages/hal-core/src/halcore/ext.py", NO_TIMEOUT): 3,
    ("packages/hal-core/src/halcore/ext.py", STRFTIME): 1,
    ("packages/hal-core/src/halcore/power.py", NO_TIMEOUT): 1,
    ("packages/hal-core/src/halcore/units.py", NO_TIMEOUT): 3,
}  # fmt: skip


def dotted(node: ast.AST) -> str:
    """`a.b.c` for a name or attribute chain, else an empty string."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def subprocess_aliases(tree: ast.AST) -> set[str]:
    """Local names bound by `from subprocess import run [as x]` and friends."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            names.update(a.asname or a.name for a in node.names if a.name in SUBPROCESS_CALLS)
    return names


def style_violations(source: str) -> list[tuple[int, str]]:
    """Returns `(line, rule)` for each timestamp or subprocess-timeout violation in `source`."""
    tree = ast.parse(source)
    aliases = subprocess_aliases(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = dotted(node.func)
        has_timezone = bool(node.args) or any(k.arg in ("tz", None) for k in node.keywords)
        if name in ("datetime.now", "datetime.datetime.now") and not has_timezone:
            found.append((node.lineno, NAIVE_NOW))
        elif name in (
            "datetime.utcnow",
            "datetime.datetime.utcnow",
            "datetime.today",
            "datetime.datetime.today",
        ):
            found.append((node.lineno, NAIVE_NOW))
        elif name == "time.strftime":
            found.append((node.lineno, STRFTIME))
        elif name in {f"subprocess.{c}" for c in SUBPROCESS_CALLS} | aliases:
            if not any(k.arg in ("timeout", None) for k in node.keywords):  # None is **kwargs
                found.append((node.lineno, NO_TIMEOUT))
    return found


def scan_sources(repo: Path) -> collections.Counter[tuple[str, str]]:
    """Counts violations per `(relative path, rule)` over every `packages/*/src` file."""
    counts: collections.Counter[tuple[str, str]] = collections.Counter()
    for path in sorted(repo.glob("packages/*/src/**/*.py")):
        for _, rule in style_violations(path.read_text(encoding="utf-8")):
            counts[(path.relative_to(repo).as_posix(), rule)] += 1
    return counts


def test_timestamps_and_subprocess_calls_match_the_legacy_baseline_exactly():
    actual = dict(scan_sources(REPO))
    new = {k: v for k, v in actual.items() if v > LEGACY.get(k, 0)}
    stale = {k: v for k, v in LEGACY.items() if actual.get(k, 0) < v}
    assert not new, f"new violations (use halkit.timeutil / halkit.proc.run): {new}"
    assert not stale, f"fixed, so lower or remove these LEGACY entries: {stale}"


def src(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_naive_now_is_flagged_and_aware_now_is_not():
    source = src(
        "from datetime import datetime, UTC",
        "a = datetime.now()",
        "b = datetime.now(UTC)",
        "c = datetime.now(tz=UTC)",
    )
    assert style_violations(source) == [(2, NAIVE_NOW)]


def test_utcnow_and_today_are_flagged():
    source = src("import datetime", "a = datetime.datetime.utcnow()", "b = datetime.datetime.today()")
    assert [rule for _, rule in style_violations(source)] == [NAIVE_NOW, NAIVE_NOW]


def test_time_strftime_is_flagged_but_a_datetime_method_is_not():
    source = src(
        "import time",
        "from datetime import UTC, datetime",
        "a = time.strftime('%Y')",
        "b = datetime.now(UTC).strftime('%Y')",
    )
    assert style_violations(source) == [(3, STRFTIME)]


def test_subprocess_call_without_timeout_is_flagged():
    source = src(
        "import subprocess",
        "subprocess.run(['x'])",
        "subprocess.check_output(['x'])",
        "subprocess.call(['x'])",
        "subprocess.check_call(['x'])",
    )
    assert [rule for _, rule in style_violations(source)] == [NO_TIMEOUT] * 4


def test_subprocess_call_with_timeout_or_kwargs_passthrough_is_fine():
    source = src("import subprocess", "subprocess.run(['x'], timeout=5)", "subprocess.run(['x'], **opts)")
    assert style_violations(source) == []


def test_imported_subprocess_function_without_timeout_is_flagged():
    source = src("from subprocess import check_output as co", "co(['x'])")
    assert style_violations(source) == [(2, NO_TIMEOUT)]


def test_popen_and_halkit_proc_run_are_not_flagged():
    source = (
        "import subprocess\nfrom halkit import proc\nsubprocess.Popen(['x'])\nproc.run(['x'], timeout=5)\n"
    )
    assert style_violations(source) == []


def test_print_is_banned_outside_entry_points_by_the_ruff_config():
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["ruff"]["lint"]
    assert "T20" in config["select"]
    ignored = [
        pattern
        for pattern, rules in config["per-file-ignores"].items()
        if any(r.startswith("T20") for r in rules)
    ]
    legacy = {"packages/hal-core/src/halcore/*.py"}
    assert set(ignored) <= {"**/__main__.py", "**/cli.py"} | legacy
