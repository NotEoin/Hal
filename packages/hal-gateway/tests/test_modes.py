"""Tests for `halgateway.modes`: discovery, the [hal] section, denied keys and the file checks."""

import hashlib
import os
import stat
import textwrap
from pathlib import Path

import pytest

from halgateway import cli, modes
from halgateway.models.manifest import ModelEntry, Verifier, sha256_file

WEIGHTS = b"synthetic weights"
WEIGHTS_SHA = hashlib.sha256(WEIGHTS).hexdigest()


class AcceptAll:
    """A verifier stand-in for the bundled modes, whose weight files do not exist here."""

    entries: dict = {}

    def verify_path(self, path):
        return ModelEntry(Path(path).name, "local", Path(path), "gguf", "0" * 64, 1)


def weights(tmp_path, name="m.gguf", template_sha=None):
    path = tmp_path / "models" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(WEIGHTS)
    entry = ModelEntry(name, "local", path, "gguf", WEIGHTS_SHA, len(WEIGHTS), None, template_sha)
    return path, Verifier({name: entry}, tmp_path / "verified.json")


def user_mode(tmp_path, body, name="mine"):
    folder = tmp_path / "user-modes"
    folder.mkdir(exist_ok=True)
    (folder / f"{name}.ini").write_text(textwrap.dedent(body))
    return [str(folder)]


def render(tmp_path, name, verifier, dirs):
    return modes.render_mode(
        name, models_dir=tmp_path / "models", state_dir=tmp_path / "state", verifier=verifier, user_dirs=dirs
    )


MINIMAL = """\
    version = 1
    [hal]
    main = m
    [*]
    ctx-size = 8192
    [m]
    model = {model}
    """


def test_every_bundled_mode_renders_with_a_hal_section(tmp_path):
    names = sorted(modes.discover([]))
    assert len(names) == 10
    for name in names:
        rendered = render(tmp_path, name, AcceptAll(), [])
        text = rendered.path.read_text()
        assert "[hal]" not in text and "@HAL@" not in text and "@MODELS@" not in text
        assert rendered.hal.main and rendered.hal.context_window >= 32768
        assert stat.S_IMODE(os.stat(rendered.path).st_mode) == 0o600


def test_bundled_hal_wiring_matches_the_old_scripts(tmp_path):
    combined = render(tmp_path, "combined", AcceptAll(), []).hal
    assert (combined.main, combined.agent, combined.classifier, combined.route) == (
        "hal-orchestrator",
        "hal-agent",
        "hal-router",
        True,
    )
    swap = render(tmp_path, "orchestrate", AcceptAll(), []).hal
    assert swap.swap and swap.effort and not swap.autoload and swap.models_max == 1
    assert swap.client_timeout_ms == 7200000 and swap.delegate_note.name == "orchestrate-prompt.txt"
    assert render(tmp_path, "hybrid-auto", AcceptAll(), []).hal.effort


def test_context_window_ignores_the_classifier_and_the_agent_in_swap_modes(tmp_path):
    body = """\
        [hal]
        main = a
        agent = b
        classifier = c
        swap = true
        [*]
        ctx-size = 1000
        [a]
        ctx-size = 5000
        [b]
        ctx-size = 2000
        [c]
        ctx-size = 100
        """
    dirs = user_mode(tmp_path, body)
    assert render(tmp_path, "mine", AcceptAll(), dirs).hal.context_window == 5000


def test_preset_windows_give_each_preset_its_own_ctx_size(tmp_path):
    body = """\
        [hal]
        main = a
        [*]
        ctx-size = 1000
        [a]
        ctx-size = 131072
        [b]
        """
    dirs = user_mode(tmp_path, body)
    assert render(tmp_path, "mine", AcceptAll(), dirs).hal.preset_windows == {"a": 131072, "b": 1000}


def test_bundled_checksums_match_the_files():
    for file, digest in modes._bundled_sums().items():
        assert sha256_file(modes.BUNDLED / file) == digest, file


def test_user_mode_replaces_the_bundled_one(tmp_path):
    dirs = user_mode(tmp_path, "[hal]\nmain = x\n[x]\nctx-size = 4096\n", name="gpu")
    assert modes.discover(dirs)["gpu"].parent != modes.BUNDLED
    assert render(tmp_path, "gpu", AcceptAll(), dirs).hal.main == "x"


@pytest.mark.parametrize(
    "key", ["host", "--port", "api-key", "tools", "mcp-servers-json", "hf-repo", "fim-qwen-1.5b-default"]
)
def test_denied_preset_keys_are_refused(tmp_path, key):
    path, verifier = weights(tmp_path)
    dirs = user_mode(tmp_path, MINIMAL.format(model=path) + f"{key} = x\n")
    with pytest.raises(modes.ModeError, match="not allowed"):
        render(tmp_path, "mine", verifier, dirs)
    assert not (tmp_path / "state" / "rendered").exists()


def test_unlisted_weight_file_is_refused(tmp_path):
    _, verifier = weights(tmp_path)
    other = tmp_path / "models" / "other.gguf"
    other.write_bytes(WEIGHTS)
    dirs = user_mode(tmp_path, MINIMAL.format(model=other))
    with pytest.raises(modes.ModeError, match="no entry in models.toml"):
        render(tmp_path, "mine", verifier, dirs)


def test_pickle_weight_is_refused(tmp_path):
    _, verifier = weights(tmp_path)
    dirs = user_mode(tmp_path, MINIMAL.format(model=tmp_path / "models" / "x.pt"))
    with pytest.raises(modes.ModeError, match="pickle"):
        render(tmp_path, "mine", verifier, dirs)


def test_modified_weight_file_is_refused(tmp_path):
    path, verifier = weights(tmp_path)
    path.write_bytes(b"SYNTHETIC weights")
    with pytest.raises(modes.ModeError, match="sha256 mismatch"):
        render(tmp_path, "mine", verifier, user_mode(tmp_path, MINIMAL.format(model=path)))


def test_unpinned_template_is_refused(tmp_path):
    path, verifier = weights(tmp_path)
    template = tmp_path / "evil.jinja"
    template.write_text("{{ evil }}")
    body = MINIMAL.format(model=path) + f"chat-template-file = {template}\n"
    with pytest.raises(modes.ModeError, match="template_sha256"):
        render(tmp_path, "mine", verifier, user_mode(tmp_path, body))


def test_template_pinned_by_the_model_entry_is_accepted(tmp_path):
    template = tmp_path / "mine.jinja"
    template.write_text("{{ ok }}")
    path, verifier = weights(tmp_path, template_sha=sha256_file(template))
    body = MINIMAL.format(model=path) + f"chat-template-file = {template}\n"
    assert render(tmp_path, "mine", verifier, user_mode(tmp_path, body)).hal.main == "m"


def test_bundled_name_with_other_content_is_not_trusted(tmp_path):
    path, verifier = weights(tmp_path)
    fake = tmp_path / "qwen3.8-chat.jinja"
    fake.write_text("tampered")
    body = MINIMAL.format(model=path) + f"chat-template-file = {fake}\n"
    with pytest.raises(modes.ModeError, match="template_sha256"):
        render(tmp_path, "mine", verifier, user_mode(tmp_path, body))


@pytest.mark.parametrize(
    ("hal", "message"),
    [
        ("main = ghost", "not a preset"),
        ("main = m\nbogus = 1", "unknown"),
        ("main = m\nroute = true", "needs agent and classifier"),
        ("main = m\nmodels-max = zero", "positive integer"),
        ("description = no main", "main is required"),
    ],
)
def test_bad_hal_section_is_refused(tmp_path, hal, message):
    path, verifier = weights(tmp_path)
    body = f"[hal]\n{hal}\n[*]\nctx-size = 8192\n[m]\nmodel = {path}\n"
    with pytest.raises(modes.ModeError, match=message):
        render(tmp_path, "mine", verifier, user_mode(tmp_path, body))


def test_unknown_mode_names_the_command_that_lists_them(tmp_path):
    with pytest.raises(modes.ModeError, match="hal-gateway modes"):
        render(tmp_path, "nope", AcceptAll(), [])


def test_modes_command_lists_the_bundled_modes(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text("")
    assert cli.main(["modes", "--config", str(cfg)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "orchestrate-256k" in out and "gpu-112k" in out


def test_render_command_refuses_a_mode_whose_weights_are_not_pinned(tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text("")
    assert cli.main(["render", "gpu", "--config", str(cfg)]) == cli.EXIT_REFUSED
    assert "no entry in models.toml" in capsys.readouterr().err
