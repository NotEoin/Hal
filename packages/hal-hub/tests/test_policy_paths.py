"""Tests for the policy's path rules (REQ-SEC-09, REQ-SEC-100) and active workspaces.

Every case runs against a synthetic home under `tmp_path`, so `realpath` sees real files and
links without touching the user's home. Attack cases: ATK-13 (a workspace link to a key) and
ATK-15 (traversal forms).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from halhub.policy import active_workspaces, check_host_path, decide, load_policy, prepare, walk_policy
from halhub.policy.model import (
    Context,
    Origin,
    ParamSpec,
    RateSnapshot,
    Taint,
    Tier,
    ToolCall,
    ToolMeta,
    Verdict,
)
from halhub.policy.workspaces import process_start
from halkit.fsafe import walk

SLEEPER_TIMEOUT_S = 30  # the helper process is killed at teardown; this only bounds a stuck test

READ = ToolMeta(
    "files_read", "organiser", "0.1.0", Tier.READ, (ParamSpec("path", format="path", required=True),)
)
MOVE = ToolMeta(
    "files_move",
    "organiser",
    "0.1.0",
    Tier.WRITE,
    (
        ParamSpec("src", format="path", required=True),
        ParamSpec("dst", format="path", access="write", required=True),
    ),
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = (tmp_path / "home").resolve()
    for folder in ("Documents/notes", "src/x", "Downloads", ".ssh", "Music"):
        (home / folder).mkdir(parents=True)
    (home / "Documents/notes/a.md").write_text("a")
    (home / "Documents/notes/.env").write_text("TOKEN=synthetic")
    (home / ".ssh/id_ed25519").write_text("synthetic key")
    (home / ".ssh/id_rsa").write_text("synthetic key")
    (home / "src/x/evil").symlink_to(home / ".ssh/id_ed25519")
    return home


def policy_for(home: Path, tmp_path: Path, text: str = ""):
    path = tmp_path / "policy.toml"
    path.write_text("schema_version = 1\n" + text)
    path.chmod(0o600)
    return load_policy(path, home=home)


def decision(meta: ToolMeta, args: dict, home: Path, policy, workspaces: tuple[Path, ...] = ()):
    call = ToolCall(meta.name, args, "0123456789ab", Origin.TUI, "c_" + "0" * 16)
    ctx = Context(
        now=datetime(2026, 10, 29, tzinfo=UTC),
        session_kind="chat",
        taint=Taint.CLEAN,
        grants=(),
        rate=RateSnapshot(),
        approver_available=True,
        paths=prepare(call, meta, home=home),
        workspaces=workspaces,
        home=home,
    )
    return decide(call, meta, policy, ctx)


def test_file_in_a_read_root_is_allowed_with_the_resolved_path(home, tmp_path):
    d = decision(READ, {"path": "~/Documents/notes/a.md"}, home, policy_for(home, tmp_path))
    assert (d.verdict, d.args["path"]) == (Verdict.ALLOW, str(home / "Documents/notes/a.md"))


def test_key_under_ssh_is_hard_denied(home, tmp_path):
    assert (
        decision(READ, {"path": "~/.ssh/id_ed25519"}, home, policy_for(home, tmp_path)).rule
        == "path:hard-deny"
    )


def test_hard_deny_holds_even_when_a_read_root_covers_it(home, tmp_path):
    policy = policy_for(home, tmp_path, "[paths]\nread_roots = ['~']\n")
    assert decision(READ, {"path": "~/.ssh/id_ed25519"}, home, policy).rule == "path:hard-deny"
    assert decision(READ, {"path": "~/Documents/notes/.env"}, home, policy).rule == "path:hard-deny"


def test_path_in_an_active_workspace_is_denied(home, tmp_path):
    ws = (home / "src/x",)
    d = decision(READ, {"path": "~/src/x/README"}, home, policy_for(home, tmp_path), ws)
    assert (d.verdict, d.rule) == (Verdict.DENY, "path:workspace")


def test_symlink_escape_via_host_tool_atk_13(home, tmp_path):
    policy = policy_for(home, tmp_path)
    evil = str(home / "src/x/evil")
    assert (
        decision(READ, {"path": evil}, home, policy, (home / "src/x",)).rule == "path:workspace"
    )
    assert decision(READ, {"path": evil}, home, policy).rule == "path:hard-deny"


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        ("../../.ssh/id_rsa", "path:relative"),
        ("~/Documents/../.ssh/id_rsa", "path:hard-deny"),
        ("/proc/self/root{home}/.ssh/id_rsa", "path:hard-deny"),
        ("a\x00b", "bad-args:nul"),
        ("/" + "a" * 5000, "path:too_long"),
        ("-rf", "path:relative"),
    ],
)
def test_path_traversal_forms_are_denied_atk_15(home, tmp_path, value, rule):
    d = decision(READ, {"path": value.format(home=home)}, home, policy_for(home, tmp_path))
    assert (d.verdict, d.rule) == (Verdict.DENY, rule)


def test_path_outside_the_roots_is_denied(home, tmp_path):
    (home / "elsewhere.txt").write_text("x")
    assert (
        decision(READ, {"path": "~/elsewhere.txt"}, home, policy_for(home, tmp_path)).rule
        == "path:outside-roots"
    )


def test_write_access_uses_the_write_roots(home, tmp_path):
    policy = policy_for(home, tmp_path)
    ok = decision(MOVE, {"src": "~/Documents/notes/a.md", "dst": "~/Downloads/a.md"}, home, policy)
    assert ok.verdict is Verdict.ALLOW
    bad = decision(MOVE, {"src": "~/Downloads/a.md", "dst": "~/Documents/notes/b.md"}, home, policy)
    assert bad.rule == "path:outside-roots"


def test_deny_entries_and_globs_from_policy_are_applied(home, tmp_path):
    policy = policy_for(home, tmp_path, "[paths]\ndeny = ['~/Documents/notes', '*/secret-*']\n")
    assert decision(READ, {"path": "~/Documents/notes/a.md"}, home, policy).rule == "path:denied"
    (home / "Music/secret-x.mp3").write_text("x")
    assert decision(READ, {"path": "~/Music/secret-x.mp3"}, home, policy).rule == "path:denied"


def test_hal_state_directory_is_denied_wherever_it_lives(home, tmp_path, hal_home):
    policy = policy_for(home, tmp_path, f"[paths]\nread_roots = ['{hal_home.root}']\n")
    (hal_home.state / "x.json").write_text("{}")
    assert decision(READ, {"path": str(hal_home.state / "x.json")}, home, policy).rule == "path:hal-dir"


def test_tool_rule_roots_replace_the_global_roots(home, tmp_path):
    policy = policy_for(home, tmp_path, "[tools.files_read]\nread_roots = ['~/Music']\n")
    assert decision(READ, {"path": "~/Documents/notes/a.md"}, home, policy).rule == "path:outside-roots"


def test_a_root_that_is_a_symlink_still_admits_its_files(home, tmp_path):
    real = (tmp_path / "data-docs").resolve()
    real.mkdir()
    (real / "b.md").write_text("b")
    (home / "Docs").symlink_to(real)
    policy = policy_for(home, tmp_path, "[paths]\nread_roots = ['~/Docs']\n")
    assert decision(READ, {"path": "~/Docs/b.md"}, home, policy).verdict is Verdict.ALLOW


@pytest.mark.parametrize(
    "value",
    [
        "~/Documents/notes/a.md",
        "~/.ssh/id_ed25519",
        "~/src/x/evil",
        "~/Documents/notes/.env",
        "relative/x",
        "~/elsewhere",
    ],
)
def test_check_host_path_matches_decide(home, tmp_path, value):
    policy = policy_for(home, tmp_path)
    ws = (home / "src/x",)
    d = decision(READ, {"path": value}, home, policy, ws)
    rule, resolved = check_host_path(
        value, access="read", tool="files_read", policy=policy, workspaces=ws, home=home
    )
    assert rule == (None if d.verdict is Verdict.ALLOW else d.rule)
    assert (resolved is not None) == (rule is None)


def test_walk_policy_matches_check_host_path_for_every_yielded_path(home, tmp_path):
    policy = policy_for(home, tmp_path, "[paths]\nread_roots = ['~/Documents']\n")
    ws = (home / "src/x",)
    wp = walk_policy("files_read", policy=policy, workspaces=ws, home=home)
    found = list(walk(home / "Documents", policy=wp))
    assert home / "Documents/notes/a.md" in found
    assert not any(p.name in (".env", "evil") or "repos/x" in str(p) for p in found)
    for path in found:
        rule, _ = check_host_path(
            str(path), access="read", tool="files_read", policy=policy, workspaces=ws, home=home
        )
        assert rule is None, path


# ---- active workspaces -------------------------------------------------------------------------


def write_owner(runtime: Path, sid: str, pid: int, pidstart: int, work: Path) -> None:
    folder = runtime / "s" / sid
    folder.mkdir(parents=True)
    owner = {"pid": pid, "pidstart": pidstart, "uid": 1000, "kind": "code", "backend": "docker"}
    owner |= {"container": f"hal-code-{sid}", "work": str(work), "created": "2026-10-29T08:00:00Z"}
    (folder / "owner.json").write_text(json.dumps(owner))


@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({SLEEPER_TIMEOUT_S})"])
    yield proc
    proc.kill()
    proc.wait(timeout=SLEEPER_TIMEOUT_S)


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")
def test_workspace_of_another_process_is_denied(home, tmp_path, sleeper):
    runtime = tmp_path / "run"
    live, dead, reused = home / "src/x", home / "Documents/notes", home / "Music"
    start = process_start(sleeper.pid)
    assert start is not None
    write_owner(runtime, "0123456789ab", sleeper.pid, start, live)
    write_owner(runtime, "ba9876543210", 2**22 + 7, 1, dead)  # no such pid
    write_owner(runtime, "aaaaaaaaaaaa", sleeper.pid, start + 1, reused)  # pid reused by another start
    (runtime / "s" / "bbbbbbbbbbbb").mkdir()
    (runtime / "s" / "bbbbbbbbbbbb" / "owner.json").write_text("{not json")
    workspaces = active_workspaces(runtime)
    assert workspaces == (live,)
    d = decision(READ, {"path": str(live / "README")}, home, policy_for(home, tmp_path), workspaces)
    assert d.rule == "path:workspace"


def test_no_runtime_directory_means_no_active_workspaces(tmp_path):
    assert active_workspaces(tmp_path / "missing") == ()


def test_process_start_reads_field_22_after_a_command_name_with_spaces(tmp_path):
    stat = tmp_path / "42" / "stat"
    stat.parent.mkdir()
    fields = ["S"] + [str(n) for n in range(4, 30)]
    stat.write_text("42 (my (odd) cmd) " + " ".join(fields))
    assert process_start(42, proc=tmp_path) == 22
    assert process_start(43, proc=tmp_path) is None


def test_policy_records_symlinked_roots_and_hal_dirs(home, tmp_path, hal_home):
    (home / "Docs").symlink_to(home / "Documents")
    policy = policy_for(home, tmp_path, "[paths]\nread_roots = ['~/Docs']\n")
    assert hal_home.state in policy.hal_dirs and hal_home.runtime in policy.hal_dirs
    assert policy.real_paths[home / "Docs"] == home / "Documents"
