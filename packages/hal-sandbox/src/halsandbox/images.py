"""Sandbox image template, pins and build command (02-sandbox REQ-BOX-22 to REQ-BOX-25).

Renders `images/Dockerfile.sandbox.in` from `images/pins.toml`, stages a build context (hash-pinned
requirements, the Hal wheels, the box-side scripts), builds with docker and runs the image's
`hal-box-entry --selftest` in a hardened container. Building is the only sandbox step that may use
the network; `run_argv` and the selftest never pull (`--pull never`, `--network none`).

Every input is pinned: base images by digest, Python dependencies by hash, `uv` by hash. The image
tag carries a hash of the whole context, so a changed pin or script gives a new tag and an old tag
never silently changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import string
import subprocess
import tempfile
import tomllib
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from halkit import paths, proc
from halkit.files import write_atomic

from . import __version__
from .errors import SandboxError

log = logging.getLogger(__name__)

IMAGES_DIR = Path(__file__).resolve().parents[2] / "images"
BOXSIDE_DIR = Path(__file__).resolve().parent / "boxside"
BOX_SCRIPTS = {
    "hal_box_entry.py": "hal-box-entry",
    "hal_relay.py": "hal-relay",
    "hal_mcp_bridge.py": "hal-mcp-bridge",
}
BOX_SHEBANG = "#!/usr/bin/python3 -I\n"  # the image's python3, isolated from env and cwd
WHEEL_PACKAGES = ("hal-kit", "hal-agent")
FLAVORS = {"sandbox": "full", "sandbox-slim": "slim"}  # image name -> flavor
FULL_PACKAGES = "build-essential pkg-config"
APT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
BUILD_TIMEOUT_S = 1800.0  # REQ-BOX: a cold apt and pip install on a slow link
WHEEL_TIMEOUT_S = 300.0
SELFTEST_TIMEOUT_S = 120.0  # runc start-up plus the tool checks
DOCKER_QUICK_S = 60.0
SIZE_BUDGET_MB = {
    "slim": 520,
    "full": 880,
}  # `docker images` SIZE, measured 2026-10-30 (511 and 869) plus margin
CTX_LEN = 12  # hex characters of the context hash kept in the tag
REPO_MARKER = "uv.lock"  # a workspace checkout has it above the package directory

NODE_FROM = "FROM ${NODE_DIGEST} AS node\n"
NODE_COPY = (
    "COPY --from=node /usr/local/bin/node /usr/local/bin/node\n"
    "COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm\n"
    "RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm\n"
)


@dataclass(frozen=True)
class ImageInfo:
    """What `images.json` records for a built image."""

    tag: str
    id: str
    size: int
    built: str
    ctx: str


def load_pins(path: Path | None = None) -> dict:
    """Reads `pins.toml`; raises `SandboxError` when it is missing or unreadable."""
    path = path or IMAGES_DIR / "pins.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SandboxError(f"cannot read image pins {path}: {exc}") from exc


def check_pins(pins: dict, *, node: bool = False) -> None:
    """Raises `SandboxError` for an unpinned base, an empty tool pin or a pin without a date."""
    bases = ["debian", *(["node"] if node else [])]
    for name in bases:
        ref = str(pins.get("base", {}).get(name, ""))
        if not DIGEST_RE.search(ref):
            raise SandboxError(f"image pin base.{name} must end in @sha256:<64 hex digits>")
    uv = pins.get("tools", {}).get("uv", {})
    for key in ("version", "published"):
        if not str(uv.get(key, "")).strip():
            raise SandboxError(f"image pin tools.uv.{key} is empty; fill it from PyPI before building")


def render_dockerfile(
    pins: dict, *, flavor: str, node: bool, extra_apt: Sequence[str], ctx: str, version: str = __version__
) -> str:
    """Renders the sandbox Dockerfile; `ctx` is the context hash recorded in a label."""
    if flavor not in FLAVORS.values():
        raise SandboxError(f"unknown image flavor {flavor!r}")
    for name in extra_apt:
        if not APT_NAME_RE.match(name):
            raise SandboxError(f"invalid apt package name {name!r}")
    check_pins(pins, node=node)
    base = pins["base"]
    template = string.Template((IMAGES_DIR / "Dockerfile.sandbox.in").read_text(encoding="utf-8"))
    return template.substitute(
        NODE_FROM=string.Template(NODE_FROM).substitute(NODE_DIGEST=base.get("node", "")) if node else "",
        NODE_COPY=NODE_COPY if node else "",
        BASE_DIGEST=base["debian"],
        FULL_PACKAGES=FULL_PACKAGES if flavor == "full" else "",
        EXTRA_APT=" ".join(extra_apt),
        CTX=ctx,
        FLAVOR=flavor,
        VERSION=version,
    )


def find_repo_root() -> Path:
    """The workspace checkout above this package; raises `SandboxError` when running installed."""
    root = IMAGES_DIR.parents[2]
    if not (root / REPO_MARKER).is_file():
        raise SandboxError("building images needs a Hal checkout (no uv.lock above hal-sandbox)")
    return root


def build_wheels(repo_root: Path, dest: Path, *, uv: str = "uv") -> None:
    """Builds the Hal wheels into `dest`; every wheel version must equal this package's version."""
    dest.mkdir(parents=True, exist_ok=True)
    for pkg in WHEEL_PACKAGES:
        try:
            proc.run(
                [uv, "build", "--wheel", "--package", pkg, "-o", str(dest)],
                timeout=WHEEL_TIMEOUT_S,
                cwd=repo_root,
            )
        except (OSError, proc.ProcTimeout, subprocess.CalledProcessError) as exc:
            raise SandboxError(f"cannot build the {pkg} wheel: {exc}") from exc
    for wheel in dest.glob("*.whl"):
        wheel_version = wheel.name.split("-")[1]
        if wheel_version != __version__:
            raise SandboxError(f"{wheel.name} has version {wheel_version}, hal-sandbox is {__version__}")


def newer_sources(built: str) -> list[Path]:
    """Source files baked into the image that changed after `built` (ISO UTC), newest first.

    Only a checkout can tell: an installed Hal has no sources to compare, so it gets `[]`.
    """
    try:
        root = find_repo_root()
        since = datetime.strptime(built, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except (SandboxError, ValueError):
        return []
    dirs = [root / "packages" / pkg / "src" for pkg in WHEEL_PACKAGES] + [BOXSIDE_DIR]
    files = [f for d in dirs if d.is_dir() for f in d.rglob("*.py")]
    return sorted((f for f in files if f.stat().st_mtime > since), key=lambda f: -f.stat().st_mtime)


def stage_bin(dest: Path) -> None:
    """Copies the three box-side scripts, keeping their module names, with the isolated-python shebang."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in BOX_SCRIPTS:
        lines = (BOXSIDE_DIR / name).read_text(encoding="utf-8").splitlines(keepends=True)
        (dest / name).write_text(BOX_SHEBANG + "".join(lines[1:]), encoding="utf-8")


def context_hash(ctx_dir: Path) -> str:
    """A stable hash over the names and bytes of every file in a staged context."""
    digest = hashlib.sha256()
    for path in sorted(p for p in ctx_dir.rglob("*") if p.is_file() and p.name != "Dockerfile"):
        digest.update(path.relative_to(ctx_dir).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def image_tag(flavor: str, node: bool, ctx: str) -> str:
    """The tag for a flavor and context hash, for example `hal-sandbox:full-node-0123456789ab`."""
    return f"hal-sandbox:{flavor}{'-node' if node else ''}-{ctx[:CTX_LEN]}"


def stage_context(dest: Path, *, repo_root: Path) -> str:
    """Stages everything but the Dockerfile into `dest`; returns the context hash."""
    for name in ("requirements-box.txt", "requirements-tools.txt"):
        shutil.copy(IMAGES_DIR / name, dest / name)
    build_wheels(repo_root, dest / "wheels")
    stage_bin(dest / "bin")
    return context_hash(dest)


def build_argv(tag: str, ctx_dir: Path, *, no_cache: bool = False, docker: str = "docker") -> list[str]:
    """The `docker build` argv; the only sandbox command that may reach the network."""
    argv = [docker, "build", "--pull=false", "-t", tag, "-f", str(ctx_dir / "Dockerfile")]
    if no_cache:
        argv.append("--no-cache")
    return [*argv, str(ctx_dir)]


def run_argv(tag: str, command: Sequence[str], *, docker: str = "docker") -> list[str]:
    """A hardened `docker run` for an image check: no network, no pull, read-only, no capabilities."""
    return [
        docker, "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534",
        "--entrypoint", "/opt/hal/bin/hal-box-entry", tag, *command,
    ]  # fmt: skip


def selftest(tag: str, *, flavor: str = "full", docker: str = "docker") -> dict:
    """Runs `hal-box-entry --selftest` in `tag`; raises `SandboxError` when a tool is missing."""
    try:
        done = proc.run(
            run_argv(tag, ["--selftest", "--flavor", flavor], docker=docker),
            timeout=SELFTEST_TIMEOUT_S,
            check=False,
        )
    except (OSError, proc.ProcTimeout) as exc:
        raise SandboxError(f"selftest of {tag} did not run: {exc}") from exc
    text = done.stdout.decode("utf-8", "replace").strip()
    if done.returncode != 0:
        raise SandboxError(
            f"selftest of {tag} failed: {text or done.stderr.decode('utf-8', 'replace').strip()}"
        )
    return json.loads(text.splitlines()[-1])


def _inspect(tag: str, docker: str) -> tuple[str, int]:
    done = proc.run(
        [docker, "image", "inspect", "--format", "{{.Id}} {{.Size}}", tag], timeout=DOCKER_QUICK_S
    )
    image_id, size = done.stdout.decode().split()
    return image_id, int(size)


CONTEXT_LABEL = "hal.image.context"


def resolve(ref: str, *, docker: str = "docker") -> str:
    """Resolves `ref` to an image ID at session start (REQ-BOX-27) and checks it is a Hal image.

    A missing image names `hal box image build`; an image without the `hal.image.context` label was
    not built `FROM` a `hal-sandbox` image and is refused.
    """
    fmt = '{{.Id}} {{index .Config.Labels "' + CONTEXT_LABEL + '"}}'
    try:
        done = proc.run(
            [docker, "image", "inspect", "--format", fmt, ref], timeout=DOCKER_QUICK_S, check=False
        )
    except (OSError, proc.ProcTimeout) as exc:
        raise SandboxError(f"cannot query docker: {exc}") from exc
    if done.returncode != 0:
        raise SandboxError(f"image {ref} not found; run `hal box image build`")
    parts = done.stdout.decode().split()
    if len(parts) < 2:
        raise SandboxError(f"image {ref} was not built FROM a hal-sandbox image (no {CONTEXT_LABEL} label)")
    return parts[0]


def status() -> list[dict]:
    """The images recorded in `$STATE/sandbox/images.json`, newest first."""
    try:
        known = json.loads((paths.state_dir() / "sandbox" / "images.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted(known, key=lambda k: k.get("built", ""), reverse=True)


def record(info: ImageInfo) -> None:
    """Writes `info` into `$STATE/sandbox/images.json`, replacing the entry for the same tag."""
    path = paths.state_dir() / "sandbox" / "images.json"
    try:
        known = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = []
    known = [k for k in known if k.get("tag") != info.tag] + [asdict(info)]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(known, indent=2, sort_keys=True))


def remove_old_tags(tag: str, *, docker: str = "docker") -> list[str]:
    """Removes older tags of the same family (never forced, never another family); returns them."""
    family = tag.rsplit("-", 1)[0] + "-"
    done = proc.run(
        [docker, "images", "hal-sandbox", "--format", "{{.Repository}}:{{.Tag}}"], timeout=DOCKER_QUICK_S
    )
    old = [t for t in done.stdout.decode().split() if t.startswith(family) and t != tag]
    gone = []
    for name in old:
        if proc.run([docker, "rmi", name], timeout=DOCKER_QUICK_S, check=False).returncode == 0:
            gone.append(name)
    return gone


def build(
    name: str = "sandbox",
    *,
    node: bool = False,
    extra_apt: Sequence[str] = (),
    no_cache: bool = False,
    repo_root: Path | None = None,
    docker: str = "docker",
) -> ImageInfo:
    """Builds the image `name` (`sandbox` or `sandbox-slim`), selftests it and records it."""
    if name not in FLAVORS:
        raise SandboxError(f"unknown image {name!r}; choose one of {', '.join(FLAVORS)}")
    flavor = FLAVORS[name]
    pins = load_pins()
    check_pins(pins, node=node)
    root = repo_root or find_repo_root()
    with tempfile.TemporaryDirectory(prefix="hal-image-") as tmp:
        ctx_dir = Path(tmp)
        ctx = stage_context(ctx_dir, repo_root=root)
        text = render_dockerfile(pins, flavor=flavor, node=node, extra_apt=extra_apt, ctx=ctx)
        (ctx_dir / "Dockerfile").write_text(text, encoding="utf-8")
        tag = image_tag(flavor, node, ctx)
        log.info("building %s", tag)
        try:
            proc.run(build_argv(tag, ctx_dir, no_cache=no_cache, docker=docker), timeout=BUILD_TIMEOUT_S)
        except (OSError, proc.ProcTimeout, subprocess.CalledProcessError) as exc:
            raise SandboxError(f"docker build failed: {exc}") from exc
    selftest(tag, flavor=flavor, docker=docker)
    image_id, size = _inspect(tag, docker)
    info = ImageInfo(tag, image_id, size, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), ctx)
    record(info)
    remove_old_tags(tag, docker=docker)
    return info
