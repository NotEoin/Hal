"""Tests for the state model of the interactive `hal tune`: fit, edits, diff and write, on fakes only."""

from __future__ import annotations

import dataclasses
import tomllib

import pytest
from test_tune import CARD_24GB, CPU_ONLY, GIB, SHOW, TAGS, arch_doc

from halcore import tuneui
from halkit import hardware
from halkit.testing import TempHalHome
from halkit.testing.ollama import FakeOllama

QWEN_TAG = {"name": "qwen3:8b", "size": 5 * GIB, "digest": "f" * 64, "details": {"family": "qwen3", "parameter_size": "8B"}}


@pytest.fixture
def home():
    with TempHalHome() as h:
        yield h


@pytest.fixture
def server():
    show = {**SHOW, "qwen3:8b": arch_doc(36, 32, 8, 128, 40960)}
    with FakeOllama([*TAGS, QWEN_TAG], show) as s:
        yield s


def session(home, server, hw=CARD_24GB, extra=""):
    home.config.write_text(f'# my notes\n[models.sources.local]\nkind = "ollama"\nurl = "{server.url}"\n{extra}')
    return tuneui.open_state(hw=hardware.from_dict(hw))


def test_choosing_a_model_shows_a_gpu_fit_on_a_24gb_card(home, server):
    s = session(home, server)
    s.choose("chat", s.find("local", "gemma:12b"))
    fit = s.fit("chat")
    assert fit.status == "gpu" and fit.context >= 16384 and 0 < fit.used_mib <= fit.budget_mib


def test_the_same_model_on_a_machine_without_a_gpu_runs_on_the_cpu(home, server):
    s = session(home, server, CPU_ONLY)
    s.choose("chat", s.find("local", "gemma:12b"))
    assert s.fit("chat").status == "cpu"


def test_a_model_larger_than_vram_and_ram_does_not_fit(home, server):
    s = session(home, server, {**CPU_ONLY, "ram_total_mib": 8192, "ram_free_mib": 6000})
    s.choose("chat", s.find("local", "big:70b"))
    assert s.fit("chat").status == "does-not-fit"
    with pytest.raises(ValueError, match="does not fit"):
        s.role_plans()


def test_a_chosen_context_is_checked_against_the_vram_and_marks_the_role_manual(home, server):
    s = session(home, server)
    s.choose("chat", s.find("local", "gemma:12b"))
    auto = s.fit("chat")
    s.set_context("chat", 16384)
    small = s.fit("chat")
    assert small.context == 16384 and small.used_mib < auto.used_mib and s.drafts["chat"].manual
    s.set_context("chat", 131072)
    assert s.fit("chat").status in ("hybrid", "gpu") and s.fit("chat").used_mib > auto.used_mib


def test_a_context_above_the_model_limit_or_below_the_floor_is_refused(home, server):
    s = session(home, server)
    s.choose("code", s.find("local", "coder:7b"))  # native context 32768
    with pytest.raises(ValueError, match="at most 32768"):
        s.set_context("code", 65536)
    with pytest.raises(ValueError, match="at least"):
        s.set_context("code", 100)
    assert not s.drafts["code"].manual


def test_trying_a_context_does_not_change_the_draft(home, server):
    s = session(home, server)
    s.choose("chat", s.find("local", "gemma:12b"))
    before = s.fit("chat")
    s.try_context("chat", 8192)
    assert s.fit("chat") == before and not s.drafts["chat"].manual


def test_effort_is_offered_only_for_a_model_the_capability_table_says_can_think(home, server):
    s = session(home, server)
    s.choose("chat", s.find("local", "gemma:12b"))
    assert s.effort_options("chat")[0] == ()
    with pytest.raises(ValueError, match="no thinking control"):
        s.set_effort("chat", "high")
    s.choose("fast", s.find("local", "qwen3:8b"))
    assert "high" in s.effort_options("fast")[0]
    s.set_effort("fast", "high")
    s.choose("fast", s.find("local", "gemma:12b"))  # switching to a model without support drops the level
    assert s.drafts["fast"].effort == ""


def test_the_diff_and_the_write_keep_comments_and_use_the_apply_path(home, server):
    s = session(home, server)
    s.choose("chat", s.find("local", "qwen3:8b"))
    s.set_effort("chat", "medium")
    s.set_context("chat", 32768)
    s.set_temperature("chat", 0.4)
    diff = s.diff()
    assert "+[models.roles.chat]" in diff and "+effort = \"medium\"" in diff
    lines = s.write()
    text = home.config.read_text()
    assert text.startswith("# my notes\n") and any(line.startswith("wrote") for line in lines)
    chat = tomllib.loads(text)["models"]["roles"]["chat"]
    assert (chat["model"], chat["context"], chat["effort"], chat["temperature"], chat["auto"]) == ("qwen3:8b", 32768, "medium", 0.4, False)
    assert "qwen3:8b@sha256:" + "f" * 64 in (home.config.parent / "models.toml").read_text()
    assert not s.has_changes() and s.diff() == ""


def test_an_untouched_session_has_nothing_to_write(home, server):
    s = session(home, server)
    assert not s.has_changes() and s.diff() == ""


def test_auto_tune_fills_every_empty_role_and_resets_a_manual_one(home, server):
    s = session(home, server)
    notes = s.auto_tune()
    assert s.drafts["chat"].model == "gemma:12b" and s.drafts["fast"].model and notes
    s.set_context("chat", 8192)
    s.auto_tune("chat")
    assert not s.drafts["chat"].manual and s.drafts["chat"].context == 0


def test_a_source_can_be_added_and_removed_in_the_draft_and_the_write_keeps_one_change_set(home, server, tmp_path):
    s = session(home, server)
    folder = tmp_path / "gguf"
    folder.mkdir()
    name = s.add_source("gguf", str(folder))
    assert name == "gguf" and "gguf" in s.sources and s.has_changes()
    assert '+[models.sources.gguf]' in s.diff()
    s.remove_source("gguf")
    assert s.diff() == ""
    s.choose("chat", s.find("local", "gemma:12b"))
    with pytest.raises(ValueError, match="use 'local'"):
        s.remove_source("local")


def test_a_bad_source_is_refused_and_leaves_the_draft_alone(home, server):
    s = session(home, server)
    with pytest.raises(ValueError):
        s.add_source("ollama", "http://example.com:11434")  # not loopback
    assert list(s.sources) == ["local"] and not s.has_changes()


def preset(key):
    from halcore import tunepresets

    return next(p for p in tunepresets.PRESETS if p.key == key)


def test_a_gpu_preset_plans_every_role_against_a_card_of_that_size(home, server):
    s = session(home, server, {**CARD_24GB, "gpus": [{**CARD_24GB["gpus"][0], "total_mib": 8192, "free_mib": 8000}]})
    s.choose("chat", s.find("local", "big:70b"))
    notes = s.apply_preset(preset("gpu-12"))
    assert (s.hw.gpus[0].total_mib, s.hw.gpus[0].free_mib) == (12288, 12288)
    assert s.fit("chat").status == "gpu" and s.model_of("chat").name != "big:70b"
    assert any(n.startswith("chat: chose") for n in notes)
    assert all(s.fit(r).status == "gpu" for r, d in s.drafts.items() if d.model)


def test_a_model_that_already_fits_the_preset_is_kept_and_retuned(home, server):
    s = session(home, server)
    s.choose("chat", s.find("local", "tiny:1b"))
    s.set_context("chat", 8192)
    s.apply_preset(preset("gpu-24"))
    assert s.drafts["chat"].model == "tiny:1b" and not s.drafts["chat"].manual and s.fit("chat").context > 8192


def test_the_cpu_preset_uses_zero_gpu_layers(home, server):
    s = session(home, server)
    s.apply_preset(preset("cpu"))
    assert s.hw.gpus == () and s.fit("chat").status == "cpu"
    assert s.plan("chat").gpu_layers == 0


def test_this_machine_plans_on_the_total_memory_of_a_card_another_program_holds(home, server):
    busy = {**CARD_24GB, "gpus": [{**CARD_24GB["gpus"][0], "free_mib": 1500}]}
    s = session(home, server, busy)
    s.apply_preset(preset("gpu-12"))
    s.apply_preset(preset("auto"))
    assert s.hw is s.real_hw and s.fit("chat").status == "gpu" and s.plan("chat").gpu_layers == -1


def test_a_role_with_no_fitting_model_is_left_alone_and_the_note_says_how_to_get_one(home):
    with FakeOllama(TAGS[:1], SHOW) as only_big:
        s = session(home, only_big)
        notes = s.apply_preset(preset("gpu-12"))
    assert s.model_of("chat") is None
    chat = next(n for n in notes if n.startswith("chat:"))
    assert "no discovered model fits 12 GB GPU" in chat and "`ollama pull gemma4:12b-it-qat`" in chat


def test_presets_cover_every_common_card_size_and_cpu():
    from halcore import tunepresets

    keys = [p.key for p in tunepresets.PRESETS]
    assert keys == ["auto", "gpu-8", "gpu-12", "gpu-16", "gpu-24", "cpu"]
    assert tunepresets.describe(preset("cpu"), hardware.from_dict(CARD_24GB)) == "no GPU layers"


def test_every_preset_recommends_a_model_for_every_role():
    from halcore import tunepresets
    from halkit import modelconf

    real = hardware.from_dict(CARD_24GB)
    for p in tunepresets.PRESETS:
        assert all(tunepresets.recommended(p, role, real) for role in modelconf.ROLES), p.key


def test_auto_recommends_the_largest_tier_the_real_card_covers():
    from halcore import tunepresets

    assert tunepresets.tier_for(preset("auto"), hardware.from_dict(CARD_24GB)) == "gpu-24"
    no_gpu = dataclasses.replace(hardware.from_dict(CARD_24GB), gpus=())
    assert tunepresets.tier_for(preset("auto"), no_gpu) == "cpu"


def test_effort_can_be_cleared_back_to_the_model_default(home, server):
    # regression: once set, the effort could not return to "default"
    state = session(home, server)
    model = state.find("local", "qwen3:8b")
    state.choose("chat", model)
    state.set_effort("chat", "medium")
    state.set_effort("chat", "default")
    assert state.drafts["chat"].effort == ""


def test_gpu_layers_set_by_hand_make_the_draft_manual_and_are_written(home, server):
    s = session(home, server)
    s.choose("chat", s.find("local", "qwen3:8b"))
    total, reason = s.layers_of("chat")
    assert total == 36 and reason == ""
    s.set_gpu_layers("chat", 20)
    fit = s.fit("chat")
    assert s.drafts["chat"].manual and fit.status == "hybrid" and fit.gpu_layers == 20
    s.write()
    chat = tomllib.loads(home.config.read_text())["models"]["roles"]["chat"]
    assert (chat["gpu_layers"], chat["auto"]) == (20, False)
    again = tuneui.open_state(hw=hardware.from_dict(CARD_24GB))
    assert again.drafts["chat"].gpu_layers == 20 and again.fit("chat").gpu_layers == 20


def test_try_layers_does_not_change_the_draft_and_all_layers_is_gpu(home, server):
    s = session(home, server)
    s.choose("chat", s.find("local", "qwen3:8b"))
    before = dataclasses.replace(s.drafts["chat"])
    assert s.try_layers("chat", 36).status == "gpu" and s.try_layers("chat", 0).status == "cpu"
    assert s.drafts["chat"] == before


def test_gpu_layers_are_refused_out_of_range_or_without_a_gpu(home, server):
    s = session(home, server)
    s.choose("chat", s.find("local", "qwen3:8b"))
    with pytest.raises(ValueError, match="36 layers"):
        s.set_gpu_layers("chat", 99)
    cpu = session(home, server, hw=CPU_ONLY)
    cpu.choose("chat", cpu.find("local", "qwen3:8b"))
    assert "no GPU" in cpu.layers_of("chat")[1]


def test_snapshot_and_restore_undo_any_change(home, server):
    s = session(home, server)
    snap = s.snapshot()
    s.choose("chat", s.find("local", "qwen3:8b"))
    s.set_context("chat", 16384)
    assert s.has_changes()
    s.restore(snap)
    assert not s.has_changes() and "chat" not in s.drafts
