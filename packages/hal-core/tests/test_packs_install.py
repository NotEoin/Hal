"""Tests for `halcore.packreview`: pinned fetch, the install review and update diffs (REQ-SEC-84, 85)."""

import subprocess
import textwrap

import pytest

from halcore import ext, packreview
from halhub.manifest import load_manifest
from halkit.testing import TempHalHome

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}  # fmt: skip
GIT_TIMEOUT_S = 30
UNIT = "[Service]\nExecStart=/usr/bin/true\n"

MANIFEST = """\
schema = 2

[module]
name = "demo"
summary = "Synthetic pack"
python = "demo.tools"

[[tools]]
name = "demo_read"
function = "read"
description = "Read a file."
tier = "read"
[tools.params.path]
description = "the file"
required = true
format = "path"
access = "read"
max_length = 4096

[[tools]]
name = "demo_send"
function = "send"
description = "Send a note."
tier = "send"
network = true
{extra}
"""


def git(cwd, *args):
    done = subprocess.run(
        ["git", *args], cwd=cwd, env={**GIT_ENV, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
        timeout=GIT_TIMEOUT_S, check=True,
    )  # fmt: skip
    return done.stdout.strip()


def make_repo(root, extra="", files=None):
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        git(root, "init", "-q", "-b", "main")
    (root / "hal-module.toml").write_text(MANIFEST.format(extra=textwrap.dedent(extra)))
    for name, text in (files or {}).items():
        (root / name).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Add pack")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def home():
    with TempHalHome() as h:
        h.config.write_text("")
        yield h


def run_install(tmp_path, repo, commit, answers):
    asked = []

    def confirm(prompt):
        asked.append(prompt)
        return answers.pop(0)

    said = []
    entry = {"repo": str(repo), "commit": commit}
    done = packreview.install("demo", entry, tmp_path / "ext", confirm=confirm, announce=said.append)
    return done, asked, "\n".join(said)


def test_install_shows_tools_and_marks_the_ones_that_run_without_a_prompt(home, tmp_path):
    repo = tmp_path / "hal-demo"
    commit = make_repo(repo)
    done, asked, said = run_install(tmp_path, repo, commit, [True])
    assert done and len(asked) == 1
    assert "demo_read: tier read" in said and "NO PROMPT" in said
    assert "demo_send: tier send, network" in said
    assert "path: format path, access read" in said
    assert (tmp_path / "ext" / "hal-demo" / "hal-module.toml").is_file()


def test_declining_the_review_installs_nothing(home, tmp_path):
    repo = tmp_path / "hal-demo"
    commit = make_repo(repo)
    done, _, said = run_install(tmp_path, repo, commit, [False])
    assert not done and "nothing installed" in said
    assert not (tmp_path / "ext" / "hal-demo").exists()
    assert not packreview.reviews_path().exists()


def test_an_entry_without_a_pin_is_refused(home, tmp_path):
    repo = tmp_path / "hal-demo"
    make_repo(repo)
    with pytest.raises(packreview.PackRefused, match="40-hex"):
        run_install(tmp_path, repo, "", [True])


def test_a_mirror_without_the_pinned_commit_is_refused(home, tmp_path):
    repo = tmp_path / "hal-demo"
    make_repo(repo)
    with pytest.raises(packreview.PackRefused, match="pinned commit"):
        run_install(tmp_path, repo, "0" * 40, [True])
    assert not (tmp_path / "ext").exists()


def test_a_manifest_that_does_not_load_stops_the_install(home, tmp_path):
    repo = tmp_path / "hal-demo"
    make_repo(repo)
    (repo / "hal-module.toml").write_text("schema = 2\n[module]\n")
    git(repo, "commit", "-q", "-am", "Break manifest")
    with pytest.raises(packreview.PackRefused, match="manifest does not load"):
        run_install(tmp_path, repo, git(repo, "rev-parse", "HEAD"), [True])


def test_the_same_commit_is_not_reviewed_twice(home, tmp_path):
    repo = tmp_path / "hal-demo"
    commit = make_repo(repo)
    run_install(tmp_path, repo, commit, [True])
    done, asked, said = run_install(tmp_path, repo, commit, [])
    assert done and asked == [] and "already installed" in said


def test_update_adding_a_unit_needs_confirmation(home, tmp_path):
    repo = tmp_path / "hal-demo"
    first = make_repo(repo)
    run_install(tmp_path, repo, first, [True])
    extra = '[[units]]\nfile = "demo.service"\n'
    second = make_repo(repo, extra, {"demo.service": UNIT})
    done, asked, said = run_install(tmp_path, repo, second, [True])
    assert done and len(asked) == 1 and "~ units" in said and "ExecStart=/usr/bin/true" in said


def test_update_dropping_path_format_needs_confirmation(home, tmp_path):
    repo = tmp_path / "hal-demo"
    first = make_repo(repo)
    run_install(tmp_path, repo, first, [True])
    text = (repo / "hal-module.toml").read_text().replace('format = "path"\n', "").replace('access = "read"\n', "")
    (repo / "hal-module.toml").write_text(text)
    git(repo, "commit", "-q", "-am", "Drop path format")
    done, asked, said = run_install(tmp_path, repo, git(repo, "rev-parse", "HEAD"), [True])
    assert done and len(asked) == 1 and "~ tool demo_read" in said


def test_an_update_with_no_new_capability_installs_without_asking(home, tmp_path):
    repo = tmp_path / "hal-demo"
    first = make_repo(repo)
    run_install(tmp_path, repo, first, [True])
    (repo / "README.md").write_text("notes\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Add notes")
    done, asked, said = run_install(tmp_path, repo, git(repo, "rev-parse", "HEAD"), [])
    assert done and asked == [] and "no change to tools" in said


def test_install_steps_are_confirmed_one_by_one(home, tmp_path):
    repo = tmp_path / "hal-demo"
    step = '[[install]]\nid = "mark"\nsummary = "touch a file"\nrun = ["/usr/bin/touch", "marker"]\n'
    commit = make_repo(repo, step)
    done, asked, _ = run_install(tmp_path, repo, commit, [True, True])
    assert done and len(asked) == 2 and "Run install command mark" in asked[1]
    assert (tmp_path / "ext" / "hal-demo" / "marker").exists()


def test_a_pipe_never_counts_as_a_typed_yes(monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    assert packreview.typed_yes("? ") is False


def test_ext_add_reports_an_unknown_name_and_a_refused_pin(home, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "hal-demo"
    make_repo(repo)
    monkeypatch.setattr(ext, "catalog", lambda: {"demo": {"repo": str(repo)}})
    monkeypatch.setattr(ext.config, "extensions_dir", lambda: tmp_path / "ext")
    monkeypatch.setattr(ext.shutil, "which", lambda name: None)
    assert ext.add(["demo", "nothing"]) == 1
    out = capsys.readouterr().out
    assert "40-hex" in out and "not in the catalogue" in out


def test_facts_of_a_manifest_name_every_tool(home, tmp_path):
    repo = tmp_path / "hal-demo"
    make_repo(repo)
    data = packreview.facts(load_manifest(repo / "hal-module.toml", source="path"))
    assert sorted(data["tools"]) == ["demo_read", "demo_send"]
