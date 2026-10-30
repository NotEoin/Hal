"""Docker-marked checks of the built sandbox images; skipped when no image or daemon is present."""

import subprocess

import pytest

from halsandbox import images
from halsandbox.errors import SandboxError

pytestmark = pytest.mark.docker


def built_tags() -> dict[str, str]:
    """Flavor -> newest local `hal-sandbox` tag, empty when docker or the images are missing."""
    try:
        out = subprocess.run(
            ["docker", "images", "hal-sandbox", "--format", "{{.Tag}}"],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout.split()  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return {}
    found: dict[str, str] = {}
    for tag in out:
        flavor = tag.split("-")[0]
        if "node" not in tag:
            found.setdefault(flavor, f"hal-sandbox:{tag}")
    return found


TAGS = built_tags()
needs_full = pytest.mark.skipif("full" not in TAGS, reason="run `hal code --build` first")


@needs_full
def test_selftest_passes():
    result = images.selftest(TAGS["full"], flavor="full")
    assert result["missing"] == [] and "uv" in result["versions"]


def test_missing_image_is_an_error_not_a_pull():
    with pytest.raises(SandboxError, match="selftest"):
        images.selftest("hal-sandbox:does-not-exist-000000000000")


@needs_full
@pytest.mark.slow
def test_image_sizes_within_budget():
    for flavor, tag in TAGS.items():
        out = subprocess.run(
            ["docker", "images", tag, "--format", "{{.Size}}"], capture_output=True, text=True, timeout=60
        ).stdout.strip()
        mb = float(out[:-2]) * (1000 if out.endswith("GB") else 1)
        assert mb <= images.SIZE_BUDGET_MB[flavor], f"{tag} is {out}"
