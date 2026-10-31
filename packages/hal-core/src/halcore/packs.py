"""`hal packs`: the pinned catalogue, pack environments and the `packs.json` record.

Constraints:
- A catalogue pack installs only from its pinned 40-hex commit, and the installed commit is
  checked afterwards (PEP 610 `direct_url.json`); a mismatch rolls the install back (REQ-CLI-62, 63).
- `URL@REF` accepts a tag or a full commit, never a branch, because a branch moves (REQ-CLI-62).
- Every subprocess goes through `halkit.proc.run` with a timeout, and git never prompts.
- No pack code is imported here: modules are found from the wheel's `RECORD` (REQ-CLI-103).
- `packs.json` is written atomically; the hub reads it (`halhub.discovery.read_installed`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from halhub import install
from halhub.manifest import InstallStep, ModuleSpec, load_manifest
from . import config
from halkit import paths, proc, termsafe, timeutil

log = logging.getLogger(__name__)

RECORD_VERSION = 2  # matches halhub.discovery.PACKS_RECORD_VERSION
PACK_INSTALL_TIMEOUT_S = 1800  # a cold uv cache builds several packs (06-core-cli "Packs")
LS_REMOTE_TIMEOUT_S = 60
QUICK_TIMEOUT_S = 120
ZERO_COMMIT = "0" * 40
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
TAG_RE = re.compile(r"^v?[0-9][0-9A-Za-z.+-]*$")
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}


def _env(**extra: str) -> dict[str, str]:
    """The process environment plus git's no-prompt switch; `proc.run` replaces the env it is given."""
    return {**os.environ, **GIT_ENV, **extra}


class PackError(Exception):
    """A pack command refused or failed; the message says why."""


def catalog_path() -> Path:
    """The packaged catalogue file."""
    return Path(str(resources.files("halcore") / "data" / "catalog.toml"))


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    """The parsed catalogue; raises `PackError` for a wrong schema or a pack with no source."""
    try:
        data = tomllib.loads((path or catalog_path()).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PackError(f"cannot read the pack catalogue: {exc}") from exc
    if data.get("schema") != 1:
        raise PackError(f"catalogue schema {data.get('schema')!r} is not 1")
    for name, pack in data.get("packs", {}).items():
        if pack.get("source") not in data.get("sources", {}):
            raise PackError(f"catalogue pack {name} names an unknown source {pack.get('source')!r}")
    return data


def is_pinned(commit: str) -> bool:
    """True for a real 40-hex commit, not the release-time placeholder."""
    return bool(COMMIT_RE.match(commit)) and commit != ZERO_COMMIT


def parse_ref(spec: str) -> tuple[str, str]:
    """Splits `URL@REF`; refuses a REF that is neither a tag nor a full commit id."""
    url, sep, ref = spec.rpartition("@")
    if not sep or not url or "://" not in url and ":" not in url:
        raise PackError(f"{spec!r} is not URL@REF")
    if not (COMMIT_RE.match(ref) or TAG_RE.match(ref)):
        raise PackError(f"ref {ref!r} is not a tag or a full commit id; a branch moves, so it is refused")
    return url, ref


def mirror(repo: str) -> str:
    """`repo` with everything up to its last `/` replaced by `HAL_PACKS_MIRROR`, when set."""
    prefix = os.environ.get("HAL_PACKS_MIRROR", "").rstrip("/")
    return f"{prefix}/{repo.rsplit('/', 1)[-1]}" if prefix else repo


def resolve_ref(url: str, ref: str) -> str:
    """The commit for a tag (annotated tags resolve through the peeled line) or the commit itself."""
    if COMMIT_RE.match(ref):
        return ref
    out = proc.run(
        ["git", "ls-remote", url, f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"],
        timeout=LS_REMOTE_TIMEOUT_S, env=_env(),
    ).stdout.decode()  # fmt: skip
    lines = [ln.split() for ln in out.splitlines() if ln.strip()]
    peeled = [c for c, r in lines if r.endswith("^{}")]
    plain = [c for c, r in lines if not r.endswith("^{}")]
    found = (peeled or plain or [""])[0]
    if not COMMIT_RE.match(found):
        raise PackError(f"tag {ref} was not found at {url}")
    return found


def record_path() -> Path:
    """Where `packs.json` lives."""
    return paths.state_dir() / "packs.json"


def read_record(path: Path | None = None) -> dict[str, Any]:
    """The installed-pack record; an absent file is an empty record, a broken one an error."""
    path = path or record_path()
    if not path.exists():
        return {"version": RECORD_VERSION, "packs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("packs"), dict):
        raise PackError(f"{path} has no packs table")
    return data


def write_record(data: dict[str, Any], path: Path | None = None) -> None:
    """Writes the record atomically (temp file, then rename)."""
    path = path or record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def env_dir(kind: str, pack: str) -> Path:
    """The environment folder for a pack of the given kind."""
    if kind == "shared":
        return paths.data_dir() / "envs" / "packs"
    if kind in ("own", "system"):
        return paths.data_dir() / "envs" / pack
    raise PackError(f"unknown env kind {kind!r}")


def _uv() -> str:
    uv = shutil.which("uv")
    if not uv:
        raise PackError("uv is not on PATH; run scripts/install.sh first")
    return uv


def ensure_env(kind: str, pack: str) -> Path:
    """Creates the environment when missing and returns its python."""
    env = env_dir(kind, pack)
    python = env / "bin" / "python"
    if python.exists():
        return python
    argv = [_uv(), "venv", str(env)]
    argv += ["--system-site-packages", "--python", "/usr/bin/python3"] if kind == "system" else ["--python", "python3"]
    proc.run(argv, timeout=QUICK_TIMEOUT_S, env=_env())
    return python


def installed_commit(python: Path, distribution: str) -> str:
    """The commit PEP 610 recorded for `distribution` in the env of `python`, or ''."""
    stem = distribution.replace("-", "_").lower()
    for info in (python.parent.parent / "lib").glob("python*/site-packages/*.dist-info"):
        if info.name.split("-")[0].replace("-", "_").lower() != stem:
            continue
        try:
            data = json.loads((info / "direct_url.json").read_text(encoding="utf-8"))
            return str(data["vcs_info"]["commit_id"])
        except (OSError, ValueError, KeyError):
            return ""
    return ""


def find_modules(python: Path, distribution: str) -> dict[str, dict[str, Any]]:
    """Modules of an installed wheel: every `hal-module.toml` its `RECORD` lists, no import."""
    stem = distribution.replace("-", "_").lower()
    out: dict[str, dict[str, Any]] = {}
    for info in (python.parent.parent / "lib").glob("python*/site-packages/*.dist-info"):
        if info.name.split("-")[0].replace("-", "_").lower() != stem:
            continue
        site = info.parent
        try:
            lines = (info / "RECORD").read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            rel = line.split(",")[0]
            if rel.endswith("hal-module.toml"):
                manifest = site / rel
                try:
                    name = tomllib.loads(manifest.read_text(encoding="utf-8"))["module"]["name"]
                except (OSError, KeyError, tomllib.TOMLDecodeError):
                    continue
                out[name] = {"manifest": str(manifest), "interpreter": [str(python)]}
    return out


def install_pip(entry: dict[str, Any], repo: str, commit: str, kind: str, pack: str) -> dict[str, Any]:
    """Two-step install of a pip pack; rolls back and raises on a commit mismatch (REQ-CLI-63, 110)."""
    python = ensure_env(kind, pack)
    reqs = Path(str(resources.files("halcore") / "data" / "requirements" / f"{'system' if kind == 'system' else 'shared'}.txt"))
    if not any(ln.strip() and not ln.startswith("#") for ln in reqs.read_text().splitlines()):
        raise PackError(f"{reqs.name} has no hashed requirements in this release; refusing an unpinned install")
    uv = _uv()
    base = [uv, "pip", "install", "--python", str(python)]
    proc.run([*base, "--require-hashes", "-r", str(reqs)], timeout=PACK_INSTALL_TIMEOUT_S, env=_env())
    dist = str(entry["distribution"])
    sub = f"#subdirectory={entry['subdirectory']}" if entry.get("subdirectory") else ""
    proc.run(
        [*base, "--no-deps", "--no-build-isolation", f"{dist} @ git+{repo}@{commit}{sub}"],
        timeout=PACK_INSTALL_TIMEOUT_S, env=_env(),
    )  # fmt: skip
    got = installed_commit(python, dist)
    if got != commit:
        proc.run([uv, "pip", "uninstall", "--python", str(python), dist], timeout=QUICK_TIMEOUT_S, check=False)
        raise PackError(f"{pack}: installed commit {got or 'unknown'} is not the pin {commit}; rolled back")
    return {
        "distribution": dist, "commit": commit, "env": kind, "python": str(python),
        "modules": find_modules(python, dist), "setup": "pending", "installed_at": _utc(),
    }  # fmt: skip


SCAN_SKIP = frozenset({".git", ".venv", "node_modules", "__pycache__"})
SCAN_DEPTH = 4  # a pack keeps its manifests near the top of the tree (packs/<name>/src/<pkg>)


def scan_modules(root: Path, python: Path) -> dict[str, dict[str, Any]]:
    """Modules of a source checkout: every `hal-module.toml` under `root`, run with `python`."""
    out: dict[str, dict[str, Any]] = {}
    for here, dirs, names in os.walk(root):
        depth = len(Path(here).relative_to(root).parts)
        dirs[:] = [d for d in dirs if d not in SCAN_SKIP] if depth < SCAN_DEPTH else []
        if "hal-module.toml" not in names:
            continue
        manifest = Path(here) / "hal-module.toml"
        try:
            name = tomllib.loads(manifest.read_text(encoding="utf-8"))["module"]["name"]
        except (OSError, KeyError, tomllib.TOMLDecodeError):
            continue
        out[name] = {"manifest": str(manifest), "interpreter": [str(python)]}
    return out


def pack_settings(root: Path) -> dict[str, Any]:
    """The `[tool.hal-pack]` table of `root/pyproject.toml` (env, system-site-packages, python)."""
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return dict(data.get("tool", {}).get("hal-pack", {}))


def _sync_own(clone: Path, pack: str) -> Path:
    """`uv sync --frozen` of the project at `clone` into the pack's own env; returns its python."""
    env = env_dir("own", pack)
    proc.run(
        [_uv(), "sync", "--frozen", "--project", str(clone)],
        timeout=PACK_INSTALL_TIMEOUT_S, env=_env(UV_PROJECT_ENVIRONMENT=str(env)),
    )  # fmt: skip
    return env / "bin" / "python"


def install_own(source: str, repo: str, commit: str, entry: dict[str, Any], pack: str) -> dict[str, Any]:
    """Clones `repo` at `commit` into `data_dir()/packs/<source>` and syncs its lockfile into its own env."""
    clone = paths.data_dir() / "packs" / source
    if not clone.exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        proc.run(["git", "clone", "--filter=blob:none", "--no-checkout", repo, str(clone)],
                 timeout=PACK_INSTALL_TIMEOUT_S, env=_env())  # fmt: skip
    proc.run(["git", "-C", str(clone), "fetch", "--quiet", "origin", commit], timeout=PACK_INSTALL_TIMEOUT_S, env=_env(), check=False)
    proc.run(["git", "-C", str(clone), "checkout", "--quiet", "--detach", commit], timeout=QUICK_TIMEOUT_S, env=_env())
    head = proc.run(["git", "-C", str(clone), "rev-parse", "HEAD"], timeout=QUICK_TIMEOUT_S).stdout.decode().strip()
    if head != commit:
        raise PackError(f"{pack}: {repo} is at {head or 'unknown'}, not the pin {commit}")
    root = clone / str(entry.get("subdirectory", ""))
    python = _sync_own(root, pack)
    return {
        "distribution": str(entry.get("distribution", pack)), "commit": commit, "env": "own", "python": str(python),
        "modules": scan_modules(root, python), "setup": "pending", "installed_at": _utc(),
    }  # fmt: skip


def install_local(path: Path) -> tuple[str, dict[str, Any]]:
    """Installs a development checkout (REQ-CLI-63); returns `(pack name, record)` marked `local`."""
    root = path.resolve()
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")).get("project", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PackError(f"{root}: no readable pyproject.toml ({exc})") from exc
    dist = str(project.get("name", ""))
    if not dist:
        raise PackError(f"{root}/pyproject.toml has no [project].name")
    kind = str(pack_settings(root).get("env", "shared"))
    if kind not in ("shared", "own", "system"):
        raise PackError(f"[tool.hal-pack].env {kind!r} is not shared, own or system")
    pack = dist.removeprefix("hal-")
    if kind == "own":
        python = _sync_own(root, pack)
    else:
        python = ensure_env(kind, pack)
        proc.run([_uv(), "pip", "install", "--python", str(python), "-e", str(root)], timeout=PACK_INSTALL_TIMEOUT_S, env=_env())
    record = {
        "distribution": dist, "commit": "", "env": kind, "python": str(python), "local": True, "path": str(root),
        "modules": scan_modules(root, python), "setup": "pending", "installed_at": _utc(),
    }  # fmt: skip
    return pack, record


def add_local(path: Path) -> str:
    """Installs and records a local pack; returns its name."""
    pack, new = install_local(path)
    record = read_record()
    record["packs"][pack] = new
    write_record(record)
    return pack


def expand_set(name: str) -> list[str]:
    """The pack names of catalogue set `name`; raises `PackError` for an unknown set."""
    sets = load_catalog().get("sets", {})
    if name not in sets:
        raise PackError(f"unknown set {name!r}; sets: {', '.join(sorted(sets)) or 'none'}")
    return list(sets[name])


def disabled() -> list[str]:
    """`[hub].disabled` from the config file."""
    return list(config.load().get("hub", {}).get("disabled", []))


def set_disabled(names: list[str]) -> None:
    """Rewrites [hub].disabled in the config file, leaving everything else as it was."""
    path = config.config_path()
    text = path.read_text() if path.exists() else ""
    line = "disabled = [" + ", ".join(f'"{n}"' for n in sorted(set(names))) + "]"
    if re.search(r"^disabled\s*=.*$", text, re.M):
        text = re.sub(r"^disabled\s*=.*$", line, text, count=1, flags=re.M)
    elif re.search(r"^\[hub\]\s*$", text, re.M):
        text = re.sub(r"^\[hub\]\s*$", "[hub]\n" + line, text, count=1, flags=re.M)
    else:
        text += f"\n[hub]\n{line}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _utc() -> str:
    return timeutil.iso_z()


def add_catalog(name: str, record: dict[str, Any] | None = None) -> dict[str, Any]:
    """Installs one catalogue pack at its pin; a pack already at the pin is left alone (REQ-CLI-65)."""
    cat = load_catalog()
    if name not in cat.get("packs", {}):
        raise PackError(f"{name}: not in the catalogue; see `hal packs list --available`")
    entry = cat["packs"][name]
    source = cat["sources"][entry["source"]]
    commit = str(source["commit"])
    if not is_pinned(commit):
        raise PackError(f"{name}: the catalogue has no pinned commit for {entry['source']} in this build")
    record = read_record() if record is None else record
    have = record["packs"].get(name)
    if have and have.get("commit") == commit:
        return have
    kind = str(entry.get("env", "shared"))
    repo = mirror(str(source["repo"]))
    if kind == "own":
        new = install_own(str(entry["source"]), repo, commit, entry, name)
    else:
        new = install_pip(entry, repo, commit, kind, name)
    new.update(source=entry["source"], tag=source.get("tag", ""))
    record["packs"][name] = new
    write_record(record)
    return new


def add_url(spec: str, *, confirm: Callable[[str], bool], distribution: str, subdirectory: str = "") -> dict[str, Any]:
    """Installs `URL@REF` into the shared env after the summary and a typed `y` (REQ-CLI-62)."""
    url, ref = parse_ref(spec)
    commit = resolve_ref(url, ref)
    summary = f"{distribution} from {url}\n  ref {ref}\n  commit {commit}\n  unpinned dependencies: not hash-checked"
    if not confirm(summary):
        raise PackError("not confirmed; nothing installed")
    entry = {"distribution": distribution, "subdirectory": subdirectory}
    new = install_pip(entry, url, commit, "shared", distribution)
    new.update(source=url, tag=ref if not COMMIT_RE.match(ref) else "")
    record = read_record()
    record["packs"][distribution] = new
    write_record(record)
    return new


def remove(name: str) -> Path | None:
    """Uninstalls a pack and drops its record; data stores stay. Returns the moved own-env folder."""
    record = read_record()
    have = record["packs"].get(name)
    if have is None:
        raise PackError(f"{name} is not installed")
    moved: Path | None = None
    if have.get("env") == "own":
        env = env_dir("own", name)
        if env.exists():
            moved = paths.data_dir() / "removed" / f"{name}-{timeutil.iso_z().replace('-', '').replace(':', '')}"
            moved.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(env), moved)
    elif have.get("python") and have.get("distribution"):
        proc.run(
            [_uv(), "pip", "uninstall", "--python", have["python"], have["distribution"]],
            timeout=QUICK_TIMEOUT_S, check=False,
        )  # fmt: skip
    del record["packs"][name]
    write_record(record)
    return moved


def doctor(names: list[str] | None = None) -> list[tuple[str, str, str]]:
    """Rows `(pack, check, result)`; a result starting with `FAIL` means the pack needs attention."""
    record = read_record()
    cat = load_catalog()
    rows: list[tuple[str, str, str]] = []
    for name, have in sorted(record["packs"].items()):
        if names and name not in names:
            continue
        pin = ""
        if name in cat.get("packs", {}):
            pin = str(cat["sources"][cat["packs"][name]["source"]]["commit"])
        commit = have.get("commit", "")
        if have.get("local"):
            rows.append((name, "commit", "dev checkout, unpinned"))
        elif pin and commit != pin:
            rows.append((name, "commit", f"FAIL installed {commit[:12]} is not the pin {pin[:12]}"))
        else:
            rows.append((name, "commit", f"ok {commit[:12]}"))
        for mod, ent in sorted(have.get("modules", {}).items()):
            ok = Path(ent["manifest"]).is_file() and Path(ent["interpreter"][0]).exists()
            rows.append((name, f"module {mod}", "ok" if ok else "FAIL manifest or interpreter missing"))
        setup = have.get("setup", "pending")
        rows.append((name, "setup", setup if setup == "done" else f"FAIL setup {setup}; run `hal packs setup {name}`"))
    return rows


def _load_specs(have: dict[str, Any]) -> list[ModuleSpec]:
    """The manifests of a recorded pack, each with the interpreter it was installed with."""
    return [
        load_manifest(Path(ent["manifest"]), source="installed", distribution=have.get("distribution"), interpreter=tuple(ent["interpreter"]))
        for _, ent in sorted(have.get("modules", {}).items())
    ]  # fmt: skip


def step_text(module: ModuleSpec, step: InstallStep) -> str:
    """The block a person reads before typing `y` for one install step (REQ-CLI-64)."""
    where = module.root if step.cwd == "root" else module.state_dir()
    lines = [
        f"{module.name} wants to run an install step ({step.id}):",
        f"  summary: {step.summary}",
        f"  argv:    {' '.join(step.run)}",
        f"  in:      {where}",
        f"  network: {'yes' if step.network else 'no'}",
        "It may change files outside Hal. Type y to run it, anything else to skip: ",
    ]
    return termsafe.escape("\n".join(lines), multiline=True)


def _typed_step(module: ModuleSpec, step: InstallStep) -> bool:
    """True when a person typed `y` on a terminal; with no terminal the step stays pending."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(step_text(module, step).rsplit("\n", 1)[0])
        print("  not run: no terminal to confirm on")
        return False
    print(step_text(module, step), end="", flush=True)
    return sys.stdin.readline().strip() == "y"


def setup(name: str, *, confirm: Callable[[ModuleSpec, InstallStep], bool] = _typed_step, force: bool = False) -> str:
    """Runs a pack's install steps through `halhub.install.run_steps`; returns and records the setup state.

    `none` (no steps), `done`, `pending` (a step was not confirmed) or `failed`. A failed step
    disables nothing here: `hal packs doctor` reports it and the hub refuses the module until it ran.
    """
    record = read_record()
    have = record["packs"].get(name)
    if have is None:
        raise PackError(f"{name} is not installed")
    state = "none"
    for spec in _load_specs(have):
        if not spec.install:
            continue
        states = install.run_steps(spec, confirm=lambda step, spec=spec: confirm(spec, step), force=force)
        if any(st.status != "ok" for st in states):
            state = "failed"
            break
        state = "done" if all(prior is not None and prior.status == "ok" for _, prior in install.plan(spec)) else "pending"
        if state == "pending":
            break
    have["setup"] = state
    write_record(record)
    return state


def finish_setup(name: str) -> str:
    """Runs the steps with typed confirmation; a failed step disables the pack (REQ-CLI-64)."""
    return finish_setup_with(_typed_step, name)


def finish_setup_with(confirm: Callable[[ModuleSpec, InstallStep], bool], name: str) -> str:
    """`finish_setup` with another confirmation callback (tests pass one)."""
    state = setup(name, confirm=confirm)
    if state == "failed":
        set_disabled([*disabled(), name])
        print(f"  {name}: disabled until `hal packs setup {name}` succeeds; `hal packs enable {name}` overrides")
    elif state == "pending":
        print(f"  {name}: install steps are pending; run `hal packs setup {name}` on a terminal")
    return state


def _typed_yes(summary: str) -> bool:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise PackError("a terminal is needed to confirm; nothing installed")
    print(summary)
    return input("Install? Type y to continue: ").strip() == "y"


def _list(available: bool, as_json: bool) -> int:
    record = read_record()
    cat = load_catalog()
    off = set(disabled())
    rows = []
    for name, have in sorted(record["packs"].items()):
        state = "disabled" if name in off else "enabled"
        rows.append({"pack": name, "state": state, "commit": have.get("commit", ""), "modules": sorted(have.get("modules", {}))})
    if available:
        for name, pack in sorted(cat.get("packs", {}).items()):
            if name not in record["packs"]:
                rows.append({"pack": name, "state": "available", "summary": pack.get("summary", "")})
    if as_json:
        print(json.dumps(rows, indent=1))
        return 0
    for r in rows:
        print(f"  {r['pack']:14} {r['state']:10} {r.get('commit', '')[:12] or r.get('summary', '')}")
    if not rows:
        print("  no packs installed; `hal packs list --available` shows the catalogue")
    return 0


def main(argv: list[str]) -> int:
    """Runs `hal packs`; returns the exit status."""
    import argparse  # noqa: PLC0415 - only the CLI path needs it

    ap = argparse.ArgumentParser(prog="hal packs", description="install and manage packs from the pinned catalogue")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list", help="installed packs")
    s.add_argument("--available", action="store_true", help="also show catalogue packs")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("add", help="NAME... from the catalogue, or URL@REF")
    s.add_argument("names", nargs="*")
    s.add_argument("--local", metavar="PATH", help="install a development checkout, unpinned")
    s.add_argument("--set", dest="set_name", metavar="NAME", help="install every pack of a catalogue set")
    s.add_argument("--dist", default="", help="distribution name for URL@REF")
    s.add_argument("--subdirectory", default="")
    for cmd in ("remove", "enable", "disable", "setup"):
        sub.add_parser(cmd, help=f"{cmd} a pack").add_argument("name")
    sub.add_parser("update", help="move installed packs to the catalogue pin").add_argument("names", nargs="*")
    s = sub.add_parser("doctor", help="check installed packs")
    s.add_argument("names", nargs="*")
    s.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        return _run(a)
    except (PackError, proc.ProcTimeout, OSError) as exc:
        print(f"hal packs: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"hal packs: {exc.cmd[0]} failed with status {exc.returncode}", file=sys.stderr)
        return 1


def _run(a: Any) -> int:
    if a.cmd == "list":
        return _list(a.available, a.json)
    if a.cmd == "add":
        if not (a.names or a.local or a.set_name):
            raise PackError("name a pack, or use --set NAME or --local PATH")
        if a.local:
            name = add_local(Path(a.local))
            print(f"  {name}: installed from {a.local} (dev checkout, unpinned); setup {finish_setup(name)}")
        for n in [*a.names, *(expand_set(a.set_name) if a.set_name else [])]:
            if "@" in n and "://" in n:
                if not a.dist:
                    raise PackError("URL@REF needs --dist NAME (the distribution to install)")
                add_url(n, confirm=_typed_yes, distribution=a.dist, subdirectory=a.subdirectory)
                name = a.dist
            else:
                name = n.removeprefix("hal-")
                if name in read_record()["packs"] and add_catalog(name).get("setup") != "pending":
                    print(f"  {name}: already installed at the pin")
                    continue
                add_catalog(name)
            print(f"  {name}: installed; setup {finish_setup(name)}")
        return 0
    if a.cmd == "update":
        record = read_record()
        for n in a.names or sorted(record["packs"]):
            if n in load_catalog().get("packs", {}):
                add_catalog(n, record)
        return 0
    if a.cmd == "remove":
        moved = remove(a.name)
        print(f"removed {a.name}" + (f"; its folder is at {moved}" if moved else ""))
        return 0
    if a.cmd in ("enable", "disable"):
        if a.name not in read_record()["packs"]:
            raise PackError(f"{a.name} is not installed")
        names = [n for n in disabled() if n != a.name] + ([a.name] if a.cmd == "disable" else [])
        set_disabled(names)
        print(f"{a.name} {a.cmd}d; run `hal units install` to re-render units")
        return 0
    if a.cmd == "setup":
        state = finish_setup(a.name)
        print(f"  {a.name}: setup {state}")
        return 1 if state == "failed" else 0
    rows = doctor(a.names)
    if a.json:
        print(json.dumps([{"pack": p, "check": c, "result": r} for p, c, r in rows], indent=1))
    else:
        for p, c, r in rows:
            print(f"  {p:14} {c:16} {r}")
    return 1 if any(r.startswith("FAIL") for _, _, r in rows) else 0
