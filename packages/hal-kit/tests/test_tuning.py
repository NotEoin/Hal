"""Tests for `halkit.tuning`, with worked numbers."""

import pytest

from halkit import tuning
from halkit.hardware import Gpu, Hardware
from halkit.modelinfo import ModelInfo

MIB = 1024 * 1024


def gpu(total, free=None, index=0):
    return Gpu(index, "nvidia", "Card", total, free if free is not None else total)


def hw(*gpus, ram_total=32768, ram_free=24576):
    return Hardware(tuple(gpus), ram_total, ram_free, 16, False)


def model12b(**over):
    """A 12B Q4 model: 7000 MiB of weights, 48 layers, 8 KV heads, head size 128, 128K native."""
    base = dict(
        source="s",
        name="m12b",
        kind="gguf",
        size_bytes=7000 * MIB,
        layers=48,
        heads=32,
        kv_heads=8,
        head_dim=128,
        context_native=131072,
    )
    return ModelInfo(**{**base, **over})


def test_kv_bytes_per_token_follows_the_formula():
    assert tuning.kv_bytes_per_token(48, 8, 128, "f16") == 2 * 48 * 8 * 128 * 2 == 196608
    assert tuning.kv_bytes_per_token(48, 8, 128, "q8_0") == 196608 / 2 * 34 / 32
    assert tuning.kv_bytes_per_token(48, 8, 128, "q4_0") == 196608 / 2 * 18 / 32


def test_unknown_kv_type_is_an_error():
    with pytest.raises(ValueError, match="unknown KV cache type"):
        tuning.kv_bytes_per_token(1, 1, 1, "q5")


def test_headroom_is_the_larger_of_1gib_and_8_percent():
    assert tuning.headroom_mib(6144) == 1024
    assert tuning.headroom_mib(24564) == 1966
    assert tuning.headroom_mib(24564, override_mib=500) == 500


def test_worked_example_24gb_card_with_a_12b_q4_model():
    # usable = 24564 - 1966 - 600 = 21998; spare = 21998 - 7000 = 14998 MiB
    # f16: 14998 MiB / 192 KiB per token = 79 989 tokens, floored to 79 872 (under the 131 072 cap)
    plan = tuning.plan_model(model12b(), hw(gpu(24564)), kv_type="f16")
    assert (plan.status, plan.budget_mib, plan.kv_type) == ("gpu", 21998, "f16")
    assert plan.context == 79872
    assert plan.gpu_layers == tuning.ALL_LAYERS


def test_context_is_clamped_to_the_models_native_maximum():
    plan = tuning.plan_model(model12b(context_native=8192), hw(gpu(24564, 24000)), kv_type="q4_0")
    assert plan.context == 8192


def test_context_is_clamped_to_the_configured_cap():
    plan = tuning.plan_model(model12b(), hw(gpu(24564, 24000)), kv_type="q4_0", context_cap=32768)
    assert plan.context == 32768


def test_auto_keeps_f16_when_it_reaches_the_goal():
    assert tuning.plan_model(model12b(), hw(gpu(24564, 24000))).kv_type == "f16"


def test_auto_drops_to_a_smaller_cache_type_on_a_tight_card():
    # 12 GB card, 8000 MiB of weights: budget 10664; spare 2664 MiB: f16 14 208, q8_0 26 7xx, q4_0 49 xxx
    plan = tuning.plan_model(model12b(size_bytes=8000 * MIB), hw(gpu(12288)))
    assert plan.kv_type == "q4_0" and plan.status == "gpu" and plan.context >= 32768


def test_two_cards_pool_their_budgets_and_split_by_usable_memory():
    plan = tuning.plan_model(
        model12b(size_bytes=30000 * MIB), hw(gpu(24576, 24000), gpu(12288, 11900, 1)), kv_type="q8_0"
    )
    one = tuning.usable_mib(gpu(24576, 24000)), tuning.usable_mib(gpu(12288, 11900))
    assert plan.budget_mib == sum(one) and plan.status == "gpu"
    assert plan.tensor_split == (round(one[0] / sum(one), 3), round(one[1] / sum(one), 3))


def test_a_single_gpu_has_no_tensor_split():
    assert tuning.plan_model(model12b(), hw(gpu(24564, 24000))).tensor_split == ()


def test_weights_larger_than_vram_offload_layers_to_ram():
    # 6 GB laptop: usable 6144 - 1024 - 600 = 4520; weights 7000; ctx 16384 -> KV f16 3072 MiB
    plan = tuning.plan_model(model12b(), hw(gpu(6144)), kv_type="f16")
    assert plan.status == "hybrid" and plan.context == 16384
    per_layer = (7000 + 3072) / 48
    assert plan.gpu_layers == int(4520 // per_layer) == 21
    assert any("system RAM" in line for line in plan.reasoning)


def test_hybrid_that_does_not_fit_in_ram_says_so():
    plan = tuning.plan_model(model12b(), hw(gpu(6144, 5500), ram_total=8192, ram_free=4096), kv_type="q8_0")
    assert plan.status == "does-not-fit" and plan.context == 0


def test_no_gpu_plans_cpu_only_with_a_spill_context():
    plan = tuning.plan_model(model12b(), hw(), kv_type="q8_0")
    assert (plan.status, plan.gpu_layers, plan.context) == ("cpu", 0, tuning.SPILL_CONTEXT)


def test_cpu_only_lowers_context_when_ram_is_short():
    plan = tuning.plan_model(model12b(), hw(ram_total=12288, ram_free=10240), kv_type="f16")
    assert plan.status == "cpu" and 2048 <= plan.context < tuning.SPILL_CONTEXT


def test_unknown_architecture_uses_a_flagged_pessimistic_estimate():
    plan = tuning.plan_model(
        model12b(layers=None, kv_heads=None, head_dim=None, context_native=None),
        hw(gpu(24564, 24000)),
        kv_type="f16",
    )
    assert plan.estimated and "architecture unknown" in plan.reasoning[0]
    known = tuning.plan_model(model12b(), hw(gpu(24564, 24000)), kv_type="f16")
    assert plan.context < known.context


def test_unknown_weight_size_is_an_error():
    with pytest.raises(ValueError, match="size of the weights"):
        tuning.plan_model(model12b(size_bytes=None), hw(gpu(24564, 24000)))


def test_fit_context_matches_the_automatic_plan_at_its_own_context():
    card = hw(gpu(24564, 24000))
    auto = tuning.plan_model(model12b(), card, kv_type="f16")
    fit = tuning.fit_context(model12b(), card, kv_type="f16", context=auto.context)
    assert (fit.status, fit.kv_mib, fit.gpu_layers) == ("gpu", auto.kv_mib, auto.gpu_layers)


def test_fit_context_moves_layers_to_ram_when_the_cache_is_too_big_for_vram():
    fit = tuning.fit_context(model12b(), hw(gpu(12288)), kv_type="f16", context=131072)
    assert fit.status == "hybrid" and 0 < fit.gpu_layers < 48


def test_fit_context_without_a_gpu_is_cpu_or_does_not_fit():
    ok = tuning.fit_context(model12b(), hw(), kv_type="q8_0", context=8192)
    assert ok.status == "cpu"
    small_ram = hw(ram_total=8192, ram_free=6000)
    assert tuning.fit_context(model12b(), small_ram, kv_type="f16", context=65536).status == "does-not-fit"


def test_fit_context_refuses_a_context_below_the_floor():
    with pytest.raises(ValueError, match="useful chat"):
        tuning.fit_context(model12b(), hw(gpu(24564)), kv_type="f16", context=512)


def gemma_like(**over):
    """A Gemma-style model: 48 layers, every 6th global, a 1024-token window on the rest."""
    return model12b(sliding_window=1024, attention_interval=6, **over)


def test_sliding_window_layers_stop_growing_at_the_window():
    # regression: every layer was costed at the full context, ~5x too much for a 5:1 Gemma
    full = tuning.kv_bytes(48, 8, 128, "f16", 32768)
    gemma = tuning.kv_bytes(48, 8, 128, "f16", 32768, window=1024, interval=6)
    per_layer = 2 * 8 * 128 * 2
    assert full == 48 * 32768 * per_layer
    assert gemma == per_layer * (8 * 32768 + 40 * 1024)
    assert tuning.kv_bytes(48, 8, 128, "f16", 512, window=1024, interval=6) == 48 * 512 * per_layer


def test_hybrid_layers_without_a_window_cost_no_per_token_cache():
    # regression: a Qwen3.5-style model (full attention every 4th layer) was costed at 4x
    hybrid = tuning.kv_bytes(48, 8, 128, "f16", 32768, interval=4)
    assert hybrid == tuning.kv_bytes(48, 8, 128, "f16", 32768) / 4


def test_a_gemma_style_model_gets_a_much_larger_context_than_plain_attention():
    card = hw(gpu(12288))
    plain = tuning.plan_model(model12b(), card, kv_type="f16")
    sliding = tuning.plan_model(gemma_like(), card, kv_type="f16")
    assert sliding.context > 4 * plain.context or sliding.context == 131072
    exact = tuning.fit_context(gemma_like(), card, kv_type="f16", context=sliding.context)
    assert exact.status == "gpu" and exact.kv_mib == sliding.kv_mib


def test_a_card_held_by_another_program_is_planned_as_if_it_were_free():
    # regression: sizing used free memory, so a card held by a big model gave 0 GPU layers
    busy = tuning.plan_model(model12b(), hw(gpu(24564, 2000)), kv_type="f16")
    idle = tuning.plan_model(model12b(), hw(gpu(24564)), kv_type="f16")
    assert busy == idle and busy.status == "gpu"


def test_a_busy_card_gets_a_one_line_warning_naming_the_held_gigabytes():
    warning = tuning.busy_warning(hw(gpu(24576, 12288)))
    assert "another program is using 12.0 GB of the GPU" in warning and "\n" not in warning
    assert tuning.busy_warning(hw(gpu(24576, 24000))) == ""
    assert tuning.busy_warning(hw()) == ""


def test_per_layer_widths_set_the_kv_size_of_a_gemma4_style_model():
    # regression: one head count for every layer overstated Gemma 4's KV about 4x to 8x
    model = model12b(layers=30, sliding_window=1024, kv_full_width=10240, kv_window_width=102400)
    per_tok, fixed, _, estimated = tuning._kv_per_token(model, "f16", 1)  # noqa: SLF001 - the formula under test
    assert (per_tok, fixed, estimated) == (10240 * 2.0, 102400 * 2.0 * 1024, False)


def test_fit_layers_all_layers_matches_fit_context_gpu_status():
    card = hw(gpu(24564))
    whole = tuning.fit_context(model12b(), card, kv_type="q8_0", context=32768)
    layers = tuning.fit_layers(model12b(), card, kv_type="q8_0", context=32768, gpu_layers=tuning.ALL_LAYERS)
    assert whole.status == layers.status == "gpu" and layers.gpu_layers == tuning.ALL_LAYERS
    assert (layers.weights_mib, layers.kv_mib) == (whole.weights_mib, whole.kv_mib)


def test_fit_layers_zero_layers_is_cpu():
    plan = tuning.fit_layers(model12b(), hw(gpu(24564)), kv_type="q8_0", context=8192, gpu_layers=0)
    assert plan.status == "cpu" and plan.gpu_layers == 0


def test_fit_layers_some_layers_is_hybrid():
    plan = tuning.fit_layers(model12b(), hw(gpu(24564)), kv_type="q8_0", context=8192, gpu_layers=10)
    assert plan.status == "hybrid" and plan.gpu_layers == 10


def test_fit_layers_over_budget_is_does_not_fit():
    plan = tuning.fit_layers(
        model12b(), hw(gpu(4096)), kv_type="f16", context=32768, gpu_layers=tuning.ALL_LAYERS
    )
    assert plan.status == "does-not-fit" and "VRAM is" in plan.reasoning[-1]


def test_fit_layers_needs_a_known_layer_count():
    with pytest.raises(ValueError, match="layer count is unknown"):
        tuning.fit_layers(model12b(layers=None), hw(gpu(24564)), kv_type="f16", context=8192, gpu_layers=4)
