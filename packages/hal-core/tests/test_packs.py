"""Tests for `halcore.packs`: pins, refs, the record, install checks and doctor (REQ-CLI-62..68, 103)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from halcore import cli, packs

PIN = "a" * 40
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


@pytest.fixture
def home(monkeypatch, tmp_path: Path):
    for var in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(packs.paths, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(packs.paths, "state_dir", lambda: tmp_path / "state")
    return tmp_path


def _catalog(tmp_path: Path, commit: str) -> Path:
    path = tmp_path / "catalog.toml"
    path.write_text(
        f'schema = 1\n[sources.s]\nrepo = "https://example.invalid/s.git"\ntag = "v1"\ncommit = "{commit}"\n'
        '[packs.demo]\nsource = "s"\ndistribution = "hal-demo"\nenv = "shared"\n'
    )
    return path


def test_branch_ref_is_refused():
    with pytest.raises(packs.PackError, match="branch"):
        packs.parse_ref("https://example.invalid/r.git@main")


def test_tag_and_commit_refs_are_accepted():
    assert packs.parse_ref("https://example.invalid/r.git@v1.2.0") == ("https://example.invalid/r.git", "v1.2.0")
    assert packs.parse_ref(f"https://example.invalid/r.git@{PIN}")[1] == PIN


def test_placeholder_commit_is_not_pinned():
    assert not packs.is_pinned("0" * 40)
    assert packs.is_pinned(PIN)


def test_shipped_catalogue_loads():
    assert "organiser" in packs.load_catalog()["packs"]


def test_add_refuses_a_catalogue_with_a_placeholder_commit(home, monkeypatch):
    monkeypatch.setattr(packs, "catalog_path", lambda: _catalog(home, "0" * 40))
    with pytest.raises(packs.PackError, match="no pinned commit"):
        packs.add_catalog("demo")


def test_tag_resolves_to_its_commit_from_a_local_repo(tmp_path: Path):
    repo = tmp_path / "r"
    subprocess.run([*GIT, "init", "-q", str(repo)], check=True)
    subprocess.run([*GIT, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    subprocess.run([*GIT, "-C", str(repo), "tag", "-a", "v1", "-m", "v1"], check=True)
    head = subprocess.run([*GIT, "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    assert packs.resolve_ref(str(repo), "v1") == head
    with pytest.raises(packs.PackError, match="not found"):
        packs.resolve_ref(str(repo), "v9")


def test_packs_json_records_interpreter_per_module(home):
    record = {"version": packs.RECORD_VERSION, "packs": {"demo": {"modules": {"m": {"manifest": "/x", "interpreter": ["/py"]}}}}}
    packs.write_record(record)
    assert packs.read_record()["packs"]["demo"]["modules"]["m"]["interpreter"] == ["/py"]


def _fake_env(home: Path, dist: str, commit: str) -> Path:
    env = packs.paths.data_dir() / "envs" / "packs"
    site = env / "lib" / "python3.13" / "site-packages"
    info = site / f"{dist.replace('-', '_')}-1.dist-info"
    info.mkdir(parents=True)
    (info / "direct_url.json").write_text(json.dumps({"vcs_info": {"commit_id": commit}}))
    (site / "demo").mkdir()
    (site / "demo" / "hal-module.toml").write_text('[module]\nname = "demo"\n')
    (info / "RECORD").write_text("demo/hal-module.toml,sha256=x,1\n")
    (env / "bin").mkdir()
    (env / "bin" / "python").write_text("")
    return env


def _stub_uv(monkeypatch, calls: list, tmp: Path):
    monkeypatch.setattr(packs, "_uv", lambda: "uv")
    monkeypatch.setattr(packs.proc, "run", lambda argv, **kw: calls.append(list(argv)) or subprocess.CompletedProcess(argv, 0, b"", b""))
    monkeypatch.setattr(packs, "env_dir", lambda kind, pack: _env_of[0])
    req = tmp / "shared.txt"
    req.write_text("pkg==1 --hash=sha256:00\n")
    monkeypatch.setattr(packs.resources, "files", lambda _n: _Files(req.parent, req.name))


_env_of: list[Path] = [Path()]


class _Files:
    def __init__(self, root: Path, name: str):
        self.root, self.name = root, name

    def __truediv__(self, part: str):
        return self

    def __str__(self):
        return str(self.root / self.name)


def test_commit_mismatch_rolls_back(home, monkeypatch):
    _env_of[0] = _fake_env(home, "hal-demo", "b" * 40)
    calls: list = []
    _stub_uv(monkeypatch, calls, home)
    with pytest.raises(packs.PackError, match="rolled back"):
        packs.install_pip({"distribution": "hal-demo"}, "https://example.invalid/s.git", PIN, "shared", "demo")
    assert any("uninstall" in c for c in calls)


def test_matching_commit_records_modules_and_second_add_does_nothing(home, monkeypatch):
    tmp_path = home
    _env_of[0] = _fake_env(home, "hal-demo", PIN)
    calls: list = []
    _stub_uv(monkeypatch, calls, tmp_path)
    monkeypatch.setattr(packs, "load_catalog", lambda path=None: packs.tomllib.loads(_catalog(tmp_path, PIN).read_text()))
    first = packs.add_catalog("demo")
    assert "demo" in first["modules"]
    count = len(calls)
    packs.add_catalog("demo")
    assert len(calls) == count


def test_remove_keeps_data_and_moves_own_env_folder(home, monkeypatch):
    env = packs.paths.data_dir() / "envs" / "solo"
    env.mkdir(parents=True)
    store = packs.paths.state_dir() / "solo-data"
    store.mkdir(parents=True)
    packs.write_record({"version": 2, "packs": {"solo": {"env": "own"}}})
    moved = packs.remove("solo")
    assert moved is not None and moved.exists() and not env.exists()
    assert store.exists() and "solo" not in packs.read_record()["packs"]


def test_packs_doctor_report(home, monkeypatch):
    tmp_path = home
    monkeypatch.setattr(packs, "load_catalog", lambda path=None: packs.tomllib.loads(_catalog(tmp_path, PIN).read_text()))
    packs.write_record({"version": 2, "packs": {"demo": {"commit": "c" * 40, "setup": "pending", "modules": {"m": {"manifest": "/nope", "interpreter": ["/nope"]}}}}})
    rows = packs.doctor()
    results = {check: result for _, check, result in rows}
    assert results["commit"].startswith("FAIL")
    assert results["module m"].startswith("FAIL")
    assert results["setup"].startswith("FAIL")
    assert packs.main(["doctor"]) == 1


def test_ext_is_an_alias_for_packs():
    assert cli.ALIASES["ext"].target == ("packs",)
    assert "ext" not in cli.COMMANDS


def _local_pack(root: Path, env_table: str = "") -> Path:
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "hal-demo"\nversion = "0"\n{env_table}')
    (root / "src" / "demo").mkdir(parents=True)
    (root / "src" / "demo" / "hal-module.toml").write_text('[module]\nname = "demo"\n')
    (root / ".venv" / "x").mkdir(parents=True)
    (root / ".venv" / "x" / "hal-module.toml").write_text('[module]\nname = "ignored"\n')
    return root


def test_local_install_is_editable_and_marked_unpinned(home, monkeypatch):
    _env_of[0] = _fake_env(home, "hal-demo", PIN)
    calls: list = []
    _stub_uv(monkeypatch, calls, home)
    root = _local_pack(home / "checkout")
    assert packs.add_local(root) == "demo"
    rec = packs.read_record()["packs"]["demo"]
    assert rec["local"] is True and rec["commit"] == "" and list(rec["modules"]) == ["demo"]
    assert any(c[-2:] == ["-e", str(root)] for c in calls)
    monkeypatch.setattr(packs, "catalog_path", lambda: _catalog(home, PIN))
    assert ("demo", "commit", "dev checkout, unpinned") in packs.doctor()


def test_local_own_pack_syncs_its_lockfile_into_its_own_env(home, monkeypatch):
    _env_of[0] = home / "envs" / "demo"
    calls: list = []
    envs: list = []
    _stub_uv(monkeypatch, calls, home)
    monkeypatch.setattr(packs.proc, "run", lambda argv, **kw: envs.append(kw.get("env")) or calls.append(list(argv)) or subprocess.CompletedProcess(argv, 0, b"", b""))
    root = _local_pack(home / "checkout", '[tool.hal-pack]\nenv = "own"\n')
    packs.add_local(root)
    assert calls[0][:4] == ["uv", "sync", "--frozen", "--project"]
    assert envs[0]["UV_PROJECT_ENVIRONMENT"] == str(_env_of[0]) and "PATH" in envs[0]


def test_local_install_refuses_an_unknown_env_kind(home):
    root = _local_pack(home / "checkout", '[tool.hal-pack]\nenv = "weird"\n')
    with pytest.raises(packs.PackError, match="weird"):
        packs.install_local(root)


def test_a_set_expands_to_its_packs_and_an_unknown_set_fails():
    assert packs.expand_set("assistant") == ["organiser", "online", "memory"]
    with pytest.raises(packs.PackError, match="unknown set"):
        packs.expand_set("nope")


def test_own_pack_install_clones_at_the_pin_and_uses_its_own_env(home, monkeypatch):
    _env_of[0] = home / "data" / "envs" / "voice"
    (_env_of[0] / "bin").mkdir(parents=True)
    calls: list = []
    monkeypatch.setattr(packs, "_uv", lambda: "uv")

    def fake(argv, **kw):
        calls.append(list(argv))
        out = (PIN + "\n").encode() if "rev-parse" in argv else b""
        return subprocess.CompletedProcess(argv, 0, out, b"")

    monkeypatch.setattr(packs.proc, "run", fake)
    new = packs.install_own("hal-voice", "https://example.invalid/v.git", PIN, {"distribution": "hal-voice"}, "voice")
    assert new["env"] == "own" and new["python"].endswith("envs/voice/bin/python")
    assert any(c[:2] == ["git", "clone"] for c in calls) and any(c[:2] == ["uv", "sync"] for c in calls)


def test_own_pack_at_the_wrong_commit_is_refused(home, monkeypatch):
    monkeypatch.setattr(packs, "_uv", lambda: "uv")
    monkeypatch.setattr(packs.proc, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"b" * 40 + b"\n", b""))
    with pytest.raises(packs.PackError, match="not the pin"):
        packs.install_own("s", "https://example.invalid/s.git", PIN, {}, "voice")


def _online_setup(home, monkeypatch, tag_commit: str):
    cat = packs.tomllib.loads(_catalog(home, PIN).read_text())
    monkeypatch.setattr(packs, "load_catalog", lambda path=None: cat)
    monkeypatch.setattr(packs, "resolve_ref", lambda url, ref: tag_commit)
    packs.write_record({"version": 2, "packs": {"demo": {"commit": PIN, "setup": "done", "modules": {}}}})


def test_doctor_online_passes_when_the_tag_still_points_at_the_pin(home, monkeypatch):
    _online_setup(home, monkeypatch, PIN)
    rows = {check: result for _, check, result in packs.doctor(online=True)}
    assert rows["online"].startswith("ok")


def test_doctor_online_fails_when_the_tag_moved(home, monkeypatch):
    _online_setup(home, monkeypatch, "d" * 40)
    rows = {check: result for _, check, result in packs.doctor(online=True)}
    assert rows["online"].startswith("FAIL tag v1 now points at")


def test_doctor_online_reports_an_unreachable_source_and_offline_never_asks(home, monkeypatch):
    _online_setup(home, monkeypatch, PIN)

    def down(url, ref):
        raise packs.PackError("network is down")

    monkeypatch.setattr(packs, "resolve_ref", down)
    assert packs.doctor(online=True)[1][2].startswith("FAIL cannot check")
    assert "online" not in {check for _, check, _ in packs.doctor()}


def _pack_repo(root: Path) -> str:
    root.mkdir()
    (root / "hal-module.toml").write_text(
        'schema = 2\n[module]\nname = "demo"\nsummary = "Synthetic"\npython = "demo.tools"\n[[tools]]\nname = "demo_send"\n'
        'function = "send"\ndescription = "Send."\ntier = "send"\nnetwork = true\n'
        '[[install]]\nid = "mark"\nsummary = "touch"\nrun = ["/usr/bin/touch", "m"]\n'
    )
    (root / "pyproject.toml").write_text('[project]\nname = "hal-demo"\nversion = "0"\ndependencies = ["requests"]\n')
    subprocess.run([*GIT, "init", "-q", str(root)], check=True)
    subprocess.run([*GIT, "-C", str(root), "add", "."], check=True)
    subprocess.run([*GIT, "-C", str(root), "commit", "-q", "-m", "x"], check=True)
    return subprocess.run([*GIT, "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def test_url_pack_summary_names_tools_install_steps_and_unpinned_dependencies(home, monkeypatch):
    commit = _pack_repo(home / "repo")
    shown: list[str] = []
    monkeypatch.setattr(packs, "install_pip", lambda *a, **k: {"modules": {}})
    packs.add_url(f"file://{home / 'repo'}@{commit}", confirm=lambda text: shown.append(text) or True, distribution="hal-demo")
    text = shown[0]
    assert "tool demo_send (tier send, network)" in text and "install step mark: /usr/bin/touch m" in text
    assert "unpinned dependencies (no uv.lock): requests" in text and commit in text


def test_declining_the_url_pack_summary_installs_nothing(home, monkeypatch):
    commit = _pack_repo(home / "repo")
    called: list = []
    monkeypatch.setattr(packs, "install_pip", lambda *a, **k: called.append(1))
    with pytest.raises(packs.PackError, match="not confirmed"):
        packs.add_url(f"file://{home / 'repo'}@{commit}", confirm=lambda text: False, distribution="hal-demo")
    assert not called
