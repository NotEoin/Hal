"""Tests for the sandbox image template, pins and build argv (no docker needed)."""

import re

import pytest

from halsandbox import images
from halsandbox.errors import SandboxError

PINS = images.load_pins()


def test_every_input_is_pinned():
    for ref in (PINS["base"]["debian"], PINS["base"]["node"]):
        assert images.DIGEST_RE.search(ref)
    reqs = (images.IMAGES_DIR / "requirements-box.txt").read_text()
    entries = [b for b in re.split(r"\n(?=[A-Za-z0-9])", reqs) if b.strip()]
    assert entries and all("--hash=sha256:" in e for e in entries)
    assert "--hash=sha256:" in (images.IMAGES_DIR / "requirements-tools.txt").read_text()
    template = (images.IMAGES_DIR / "Dockerfile.sandbox.in").read_text()
    assert "--require-hashes" in template and "--no-deps" in template


def test_pins_have_dates():
    uv = PINS["tools"]["uv"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", uv["published"]) and uv["version"]
    assert f"uv=={uv['version']}" in (images.IMAGES_DIR / "requirements-tools.txt").read_text()


def test_an_empty_uv_pin_refuses_the_build():
    bad = {**PINS, "tools": {"uv": {"version": "", "published": ""}}}
    with pytest.raises(SandboxError, match="tools.uv.version"):
        images.check_pins(bad)


def test_a_base_without_a_digest_is_refused():
    with pytest.raises(SandboxError, match="base.debian"):
        images.check_pins({**PINS, "base": {"debian": "debian:trixie-slim"}})


def render(**kw):
    args = {"flavor": "full", "node": False, "extra_apt": (), "ctx": "c" * 64, **kw}
    return images.render_dockerfile(PINS, **args)


def test_full_flavor_adds_the_c_toolchain_and_slim_does_not():
    assert "build-essential pkg-config" in render()
    assert "build-essential" not in render(flavor="slim")


def test_node_adds_a_second_stage_only_when_asked():
    with_node, without = render(node=True), render()
    assert with_node.startswith("# syntax") and "AS node" in with_node and "COPY --from=node" in with_node
    assert "node" not in without.replace("node_modules", "")


def test_extra_apt_names_are_validated_and_rendered():
    assert " cargo " in render(extra_apt=["cargo"])
    with pytest.raises(SandboxError, match="invalid apt package"):
        render(extra_apt=["x; rm -rf /"])


def test_rendered_dockerfile_has_no_unfilled_placeholders_and_keeps_dpkg_format():
    text = render()
    assert "${" not in text.replace("${Package} ${Version}", "")
    assert "hal.image.context=" + '"' + "c" * 64 in text


def test_context_hash_is_stable_and_changes_with_content(tmp_path):
    (tmp_path / "a").write_text("1")
    first = images.context_hash(tmp_path)
    assert images.context_hash(tmp_path) == first
    (tmp_path / "a").write_text("2")
    assert images.context_hash(tmp_path) != first


def test_stage_bin_rewrites_the_shebang_and_names_the_scripts(tmp_path):
    images.stage_bin(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hal-box-entry", "hal-mcp-bridge", "hal-relay"]
    assert (tmp_path / "hal-relay").read_text().startswith(images.BOX_SHEBANG)


def test_build_is_the_only_networked_step(tmp_path):
    build = images.build_argv("t:1", tmp_path)
    run = images.run_argv("t:1", ["--selftest"])
    assert "--pull=false" in build and "--network" not in build
    assert run[run.index("--network") + 1] == "none" and run[run.index("--pull") + 1] == "never"
    assert "--read-only" in run and run[run.index("--cap-drop") + 1] == "ALL"


def test_tag_carries_flavor_node_and_a_short_context_hash():
    assert images.image_tag("full", True, "ab" * 32) == "hal-sandbox:full-node-abababababab"


def test_unknown_image_name_is_refused():
    with pytest.raises(SandboxError, match="unknown image"):
        images.build("sandbox-huge")
