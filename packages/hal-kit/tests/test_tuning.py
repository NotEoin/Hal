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
    # usable = 24000 - 1966 - 600 = 21434; spare = 21434 - 7000 = 14434 MiB
    # f16: 14434 MiB / 192 KiB per token = 76 981 tokens, floored to 76 800 (under the 131 072 cap)
    plan = tuning.plan_model(model12b(), hw(gpu(24564, 24000)), kv_type="f16")
    assert (plan.status, plan.budget_mib, plan.kv_type) == ("gpu", 21434, "f16")
    assert plan.context == 76800
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
    # 12 GB card: usable 11500 - 1024... = budget 9916; spare 2916 MiB: f16 15 872, q8_0 29 952, q4_0 54 272
    plan = tuning.plan_model(model12b(), hw(gpu(12288, 11500)))
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
    # 6 GB laptop: usable 5500 - 1024 - 600 = 3876; weights 7000; ctx 16384 -> KV f16 3072 MiB
    plan = tuning.plan_model(model12b(), hw(gpu(6144, 5500)), kv_type="f16")
    assert plan.status == "hybrid" and plan.context == 16384
    per_layer = (7000 + 3072) / 48
    assert plan.gpu_layers == int(3876 // per_layer) == 18
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
