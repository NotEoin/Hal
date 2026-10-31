"""Tests for pack install steps: typed confirmation, no-terminal and failure (REQ-CLI-64, 65)."""

from __future__ import annotations

import pytest
from halcore import config, packs
from halkit.testing import TempHalHome

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

[[install]]
id = "mark"
summary = "touch a file"
run = {run}
"""


@pytest.fixture
def pack(tmp_path):
    with TempHalHome():
        root = tmp_path / "hal-demo"
        root.mkdir()
        (root / "hal-module.toml").write_text(MANIFEST.format(run='["/usr/bin/touch", "marker"]'))
        packs.write_record(
            {
                "version": packs.RECORD_VERSION,
                "packs": {"demo": {"distribution": "hal-demo", "setup": "pending",
                                   "modules": {"demo": {"manifest": str(root / "hal-module.toml"), "interpreter": ["/usr/bin/python3"]}}}},
            }
        )  # fmt: skip
        yield root


def test_install_steps_need_typed_confirmation_and_run_once_confirmed(pack):
    shown = []
    state = packs.setup("demo", confirm=lambda spec, step: shown.append(packs.step_text(spec, step)) or True)
    assert state == "done" and (pack / "marker").exists()
    assert "argv:    /usr/bin/touch marker" in shown[0] and "network: no" in shown[0]
    assert packs.read_record()["packs"]["demo"]["setup"] == "done"


def test_declining_a_step_leaves_setup_pending_and_runs_nothing(pack):
    assert packs.setup("demo", confirm=lambda spec, step: False) == "pending"
    assert not (pack / "marker").exists()


def test_no_tty_leaves_install_steps_pending(pack, capsys):
    assert packs.setup("demo") == "pending"
    out = capsys.readouterr().out
    assert "touch a file" in out and "no terminal" in out and not (pack / "marker").exists()


def test_a_failed_step_disables_the_pack(pack):
    (pack / "hal-module.toml").write_text(MANIFEST.format(run='["/usr/bin/false"]'))
    assert packs.finish_setup_with(lambda spec, step: True, "demo") == "failed"
    assert "demo" in packs.disabled()


def test_a_step_text_shows_hidden_characters_as_escapes(pack):
    (pack / "hal-module.toml").write_text(MANIFEST.format(run='["/usr/bin/touch", "a\\u202eb"]'))
    spec = packs._load_specs(packs.read_record()["packs"]["demo"])[0]
    assert "\\u202e" in packs.step_text(spec, spec.install[0]) and chr(0x202E) not in packs.step_text(spec, spec.install[0])


def test_the_disabled_list_keeps_the_rest_of_the_config(pack):
    config.config_path().write_text('[hal]\nvault = "v"\n\n[hub]\npinned = ["a"]\n')
    packs.set_disabled(["phone"])
    packs.set_disabled(["phone", "mail"])
    text = config.config_path().read_text()
    assert 'disabled = ["mail", "phone"]' in text and 'pinned = ["a"]' in text and text.count("disabled") == 1
