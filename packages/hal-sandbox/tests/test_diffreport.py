"""Tests for `halsandbox.diffreport` (01-security "Workspace scan", REQ-BOX-50)."""

import pytest

from halsandbox import diffreport as dr

NEW = "000000"
FILE = "100644"
EXEC = "100755"


def kinds(path, old=NEW, new=FILE, **kw):
    return {(f.kind, f.severity) for f in dr.flags_for(path, old, new, **kw)}


@pytest.mark.parametrize("path", [".envrc", ".env", ".env.local", "sub/.envrc"])
def test_dotenv_and_direnv_files_are_high(path):
    assert ("automation", dr.HIGH) in kinds(path)


@pytest.mark.parametrize(
    "path",
    [".github/workflows/ci.yml", ".gitlab-ci.yml", ".pre-commit-config.yaml", ".husky/pre-commit",
     ".claude/settings.json", ".mcp.json"],
)  # fmt: skip
def test_ci_commit_hook_and_harness_files_are_high(path):
    assert ("automation", dr.HIGH) in kinds(path)


@pytest.mark.parametrize(
    "path",
    ["Makefile", "sub/Makefile", "rules.mk", "justfile", "Taskfile.yml", "package.json", "pyproject.toml",
     "setup.py", "conftest.py", "noxfile.py", "tox.ini", ".vscode/tasks.json", ".idea/workspace.xml",
     "a.code-workspace", "x.desktop", "x.service", "x.timer", "AGENTS.md", "CLAUDE.md",
     ".github/copilot-instructions.md", ".codex/config.toml", ".qwen/a", ".opencode/a", ".cursor/rules",
     ".agents/skills/s/SKILL.md", ".bashrc", ".zshrc", ".profile"],
)  # fmt: skip
def test_run_by_habit_and_editor_and_instruction_files_are_flagged(path):
    assert any(kind == "automation" for kind, _ in kinds(path))


def test_a_plain_source_file_has_no_flags():
    assert dr.flags_for("src/app.py", NEW, FILE) == []


def test_a_shell_script_is_flagged_only_when_executable():
    assert dr.flags_for("run.sh", NEW, FILE) == []
    assert ("automation", dr.MEDIUM) in kinds("run.sh", new=EXEC)


def test_newly_executable_file_is_flagged_but_an_already_executable_one_is_not():
    assert ("executable_added", dr.MEDIUM) in kinds("tool", new=EXEC)
    assert ("executable_added", dr.MEDIUM) in kinds("tool", old=FILE, new=EXEC)
    assert dr.flags_for("tool", EXEC, EXEC) == []


@pytest.mark.parametrize("path", [".gitattributes", ".gitmodules", "sub/.gitattributes", ".gitconfig"])
def test_git_metadata_files_are_high(path):
    assert ("git_metadata", dr.HIGH) in kinds(path)


def test_gitlinks_and_paths_below_a_git_directory_are_nested_repos():
    assert ("nested_repo", dr.HIGH) in kinds("vendor/lib", new="160000")
    assert ("nested_repo", dr.HIGH) in kinds("vendor/.git/config")
    assert ("nested_repo", dr.HIGH) in kinds("vendor/.git")


@pytest.mark.parametrize(
    ("path", "target", "escapes"),
    [("a", "/etc/passwd", True), ("a", "../x", True), ("d/a", "../../x", True), ("d/a", "../b", False),
     ("a", "b/c", False), ("a", "..", True), ("d/a", "..", False)],
)  # fmt: skip
def test_symlink_escape_is_judged_from_the_links_own_directory(path, target, escapes):
    assert dr.symlink_escapes(path, target) is escapes
    flagged = ("symlink_outside", dr.HIGH) in kinds(path, new="120000", target=target)
    assert flagged is escapes


def test_a_symlink_with_an_unknown_target_is_treated_as_outside():
    assert ("symlink_outside", dr.HIGH) in kinds("a", new="120000")


def test_summary_counts_flagged_paths():
    assert dr.sensitive_summary(0) == "no sensitive files changed"
    assert dr.sensitive_summary(3) == "3 sensitive files changed"
