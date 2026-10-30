"""Box images for agent CLIs (02-sandbox REQ-BOX-22 to REQ-BOX-24, step 29).

A box image is the `hal-sandbox` image plus one agent CLI, installed from a committed npm lockfile
(`images/box/<cli>/package-lock.json`, integrity hashes) or, for goose, from a release tarball with a
pinned sha256. Rendering and tagging are pure; `build_box` is the only networked step.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import string
import subprocess
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from halkit import proc

from . import images
from .errors import SandboxError

log = logging.getLogger(__name__)

CLIS = ("claude", "qwen", "opencode", "codex", "crush", "goose")
MIN_PIN_AGE_DAYS = 14  # REQ-BOX-24: time for the registry to pull a compromised release
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NPM_INSTALL = (
    "COPY --from=node /usr/local/bin/node /usr/local/bin/node\n"
    "COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm\n"
    "RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm\n"
    "COPY box/${CLI}/ /opt/hal/box/${CLI}/\n"
    "RUN cd /opt/hal/box/${CLI} \\\n"
    " && npm ci --omit=dev --no-audit --no-fund \\\n"
    " && npm cache clean --force \\\n"
    " && ln -s /opt/hal/box/${CLI}/node_modules/.bin/* /usr/local/bin/\n"
    # links, not ENV PATH: the box PATH is fixed by env.BOX_PATH
)
GOOSE_INSTALL = (
    "ADD --checksum=sha256:${GOOSE_SHA256} ${GOOSE_URL} /tmp/goose.tar.gz\n"
    "RUN mkdir -p /opt/hal/box/goose/bin \\\n"
    " && tar -xzf /tmp/goose.tar.gz -C /opt/hal/box/goose/bin && rm /tmp/goose.tar.gz\n"
    "RUN ln -s /opt/hal/box/goose/bin/goose /usr/local/bin/goose\n"
)


def check_cli_pin(pins: dict, cli: str, *, today: date | None = None) -> dict:
    """Returns the pin of `cli`; raises `SandboxError` when it is missing, empty or too young."""
    if cli not in CLIS:
        raise SandboxError(f"unknown agent CLI {cli!r}; choose one of {', '.join(CLIS)}")
    pin = pins.get("cli", {}).get(cli)
    if not pin:
        raise SandboxError(f"no pin for agent CLI {cli!r} in pins.toml")
    needed = ("url", "sha256") if cli == "goose" else ("package",)
    for key in ("version", "published", *needed):
        if not str(pin.get(key, "")).strip():
            raise SandboxError(f"image pin cli.{cli}.{key} is empty; fill it before building")
    if cli == "goose" and not SHA256_RE.match(pin["sha256"]):
        raise SandboxError("image pin cli.goose.sha256 is not a 64-digit hex sha256")
    age = (today or datetime.now(UTC).date()) - date.fromisoformat(str(pin["published"]))
    if age < timedelta(days=MIN_PIN_AGE_DAYS):
        raise SandboxError(f"cli.{cli} {pin['version']} is younger than {MIN_PIN_AGE_DAYS} days")
    return pin


def render_box_dockerfile(pins: dict, cli: str, *, base_id: str) -> str:
    """Renders `Dockerfile.box.in` for `cli` on top of the local sandbox image `base_id` (a tag)."""
    pin = check_cli_pin(pins, cli)
    if cli == "goose":
        install = string.Template(GOOSE_INSTALL).substitute(GOOSE_SHA256=pin["sha256"], GOOSE_URL=pin["url"])
        node_from = ""
    else:
        install = string.Template(NPM_INSTALL).substitute(CLI=cli)
        node_from = f"FROM {pins['base']['node']} AS node\n"
    template = string.Template((images.IMAGES_DIR / "Dockerfile.box.in").read_text(encoding="utf-8"))
    return template.substitute(
        NODE_FROM=node_from, INSTALL=install, CLI=cli, CLI_VERSION=pin["version"], SANDBOX_IMAGE_ID=base_id
    )


def box_tag(cli: str, base_id: str, ctx: str, dockerfile: str = "") -> str:
    """The tag `hal-box-<cli>:<hash>`; the hash covers the base image ID, the Dockerfile and the files."""
    digest = hashlib.sha256(f"{base_id}\0{ctx}\0{dockerfile}".encode()).hexdigest()
    return f"hal-box-{cli}:{digest[: images.CTX_LEN]}"


def stage_box_context(dest: Path, cli: str) -> str:
    """Copies `images/box/<cli>/` into `dest/box/<cli>/` (npm CLIs only); returns the context hash."""
    if cli != "goose":
        src = images.IMAGES_DIR / "box" / cli
        if not (src / "package-lock.json").is_file():
            raise SandboxError(f"images/box/{cli}/package-lock.json is missing")
        shutil.copytree(src, dest / "box" / cli)
    return images.context_hash(dest)


def build_box(cli: str, *, base_tag: str, docker: str = "docker") -> str:
    """Builds `hal-box-<cli>` on the local sandbox image `base_tag`; returns its tag.

    The Dockerfile names the base by tag because buildkit cannot `FROM` a bare image ID; the tag hash
    covers the ID the tag has now, so a rebuilt base gives a new box tag.
    """
    pins = images.load_pins()
    base_id, _ = images._inspect(base_tag, docker)
    with tempfile.TemporaryDirectory(prefix="hal-box-image-") as tmp:
        ctx_dir = Path(tmp)
        ctx = stage_box_context(ctx_dir, cli)
        text = render_box_dockerfile(pins, cli, base_id=base_tag)
        (ctx_dir / "Dockerfile").write_text(text, encoding="utf-8")
        tag = box_tag(cli, base_id, ctx, text)
        try:
            proc.run(images.build_argv(tag, ctx_dir, docker=docker), timeout=images.BUILD_TIMEOUT_S)
        except (OSError, proc.ProcTimeout, subprocess.CalledProcessError) as exc:
            raise SandboxError(f"docker build of {tag} failed: {exc}") from exc
    return tag


def newest(cli: str, *, docker: str = "docker") -> str | None:
    """The newest local `hal-box-<cli>` tag, or None when none is built."""
    if cli not in CLIS:
        raise SandboxError(f"unknown agent CLI {cli!r}; choose one of {', '.join(CLIS)}")
    try:
        done = proc.run(
            [docker, "images", f"hal-box-{cli}", "--format", "{{.Repository}}:{{.Tag}}"],
            timeout=images.DOCKER_QUICK_S,
        )
    except (OSError, proc.ProcTimeout, subprocess.CalledProcessError) as exc:
        raise SandboxError(f"cannot query docker: {exc}") from exc
    tags = done.stdout.decode().split()
    return tags[0] if tags else None  # docker lists the newest first


def harnesses(*, docker: str = "docker") -> list[dict[str, object]]:
    """Each supported CLI with its pinned version, newest image tag and whether it is built."""
    pins = images.load_pins().get("cli", {})
    rows = []
    for cli in CLIS:
        tag = newest(cli, docker=docker)
        rows.append(
            {"name": cli, "version": pins.get(cli, {}).get("version", ""), "image": tag, "built": bool(tag)}
        )
    return rows
