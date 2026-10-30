"""Golden-file tests for the box CLI writers (REQ-BOX-60); one parametrised file per profile set."""

import json
from pathlib import Path

import pytest

from halsandbox.boxcli import PROFILES, BoxContext, profile
from halsandbox.errors import SandboxError

GOLDEN = Path(__file__).parent / "golden" / "boxcli"
FAKE_TOKEN = "gw-token-0123456789abcdef"  # a synthetic gateway token that must never appear


def render(launch) -> str:
    """A stable text form of a launch: argv, sorted env, then each seed file."""
    parts = ["argv: " + json.dumps(list(launch.argv)), "env:"]
    parts += [f"  {k}={v}" for k, v in sorted(launch.env.items())]
    for path, data in sorted(launch.home_seed.items()):
        parts += [f"--- {path}", data.decode("ascii").rstrip("\n")]
    return "\n".join(parts) + "\n"


@pytest.mark.parametrize("auto", [True, False], ids=["auto", "ask"])
@pytest.mark.parametrize("cli", sorted(PROFILES))
def test_output_matches_the_golden_file(cli, auto):
    ctx = BoxContext(model="hal-code", context_window=65536, auto_approve=auto, user_args=("--foo",))
    text = render(profile(cli).launch(ctx))
    golden = GOLDEN / f"{cli}_{'auto' if auto else 'ask'}.txt"
    assert text == golden.read_text(encoding="ascii")


@pytest.mark.parametrize("cli", sorted(PROFILES))
def test_no_secret_value_is_written_and_the_relay_is_used(cli):
    text = render(profile(cli).launch(BoxContext(model="hal-code")))
    assert FAKE_TOKEN not in text and "127.0.0.1:8741" in text and "hal-mcp-bridge" in text
    assert "DO_NOT_TRACK=1" in text


@pytest.mark.parametrize("cli", sorted(PROFILES))
def test_a_model_alias_with_control_characters_is_refused(cli):
    with pytest.raises(SandboxError, match="printable ASCII"):
        profile(cli).launch(BoxContext(model='x"\n[evil]'))


def test_unknown_cli_is_refused():
    with pytest.raises(SandboxError, match="unsupported agent CLI"):
        profile("emacs")
