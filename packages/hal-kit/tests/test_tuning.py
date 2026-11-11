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


def test_overhead_counts_the_cuda_context_the_output_buffer_and_the_mask():
    model = model12b(vocab=128256)
    logits = tuning.UBATCH * 128256 * 4
    assert tuning.overhead_mib(model) == tuning.CUDA_CONTEXT_MIB + -(-logits // MIB)
    mask = tuning.UBATCH * 65536 * 2
    assert tuning.overhead_mib(model, 1, 65536) == tuning.CUDA_CONTEXT_MIB + -(-(logits + mask) // MIB)
    assert tuning.overhead_mib(model, 2) == tuning.overhead_mib(model) + tuning.CUDA_CONTEXT_MIB
    assert tuning.overhead_mib(model, 0) == 0


def test_a_quantised_cache_adds_one_f16_layer_over_the_context():
    model = model12b(vocab=128256)
    extra = tuning.overhead_mib(model, 1, 8192, "q8_0") - tuning.overhead_mib(model, 1, 8192, "f16")
    assert extra == pytest.approx(2 * 8 * 128 * 2 * 8192 / MIB, abs=1)


# Measured on an RTX 3090 with llama.cpp b11512 (docs/dev/tuning.md "Measurements"): the
# estimate must stay within 2 percent of the card's use and never under it by more than 1 percent.
LLAMA31_8B_Q8 = dict(size_bytes=8_540_784_896, layers=32, kv_heads=8, head_dim=128, vocab=128256,
                     host_bytes=557_842_432, context_native=131072)  # fmt: skip
MEASURED = [(8192, "f16", 9070), (65536, "f16", 16294), (8192, "q8_0", 8592)]


@pytest.mark.parametrize(("context", "kv", "measured"), MEASURED)
def test_the_estimate_matches_a_measured_llama_31_8b(context, kv, measured):
    plan = tuning.place(model12b(**LLAMA31_8B_Q8), hw(gpu(24576)), kv_type=kv, context=context)
    assert plan.status == "gpu"
    assert measured * 0.99 <= plan.vram_mib <= measured * 1.02


def test_worked_example_24gb_card_with_a_12b_q4_model():
    # budget = 24564 - 1966 = 22598; on the GPU: 7000 weights + 180 + 297 (vocab 152064) = 7477
    # f16: 192 KiB + 1 KiB of mask per token; 15121 MiB / 193 KiB = 80 228, floored to 79 872
    plan = tuning.plan_model(model12b(), hw(gpu(24564)), kv_type="f16")
    assert (plan.status, plan.budget_mib, plan.kv_type, plan.total_mib) == ("gpu", 22598, "f16", 24564)
    assert plan.context == 79872 and plan.gpu_layers == tuning.ALL_LAYERS
    assert plan.vram_mib == 7000 + plan.kv_mib + plan.overhead_mib <= plan.budget_mib


def test_context_is_clamped_to_the_models_native_maximum():
    plan = tuning.plan_model(model12b(context_native=8192), hw(gpu(24564, 24000)), kv_type="q4_0")
    assert plan.context == 8192


def test_context_is_clamped_to_the_configured_cap():
    plan = tuning.plan_model(model12b(), hw(gpu(24564, 24000)), kv_type="q4_0", context_cap=32768)
    assert plan.context == 32768


def test_auto_keeps_f16_when_it_reaches_the_goal():
    assert tuning.plan_model(model12b(), hw(gpu(24564, 24000))).kv_type == "f16"


def test_auto_drops_to_a_smaller_cache_type_on_a_tight_card():
    plan = tuning.plan_model(model12b(size_bytes=8000 * MIB), hw(gpu(12288)))
    assert plan.kv_type in ("q8_0", "q4_0") and plan.status == "gpu" and plan.context >= 32768


def test_two_cards_pool_their_budgets_and_split_by_usable_memory():
    plan = tuning.plan_model(
        model12b(size_bytes=30000 * MIB), hw(gpu(24576, 24000), gpu(12288, 11900, 1)), kv_type="q8_0"
    )
    one = tuning.usable_mib(gpu(24576, 24000)), tuning.usable_mib(gpu(12288, 11900))
    assert plan.budget_mib == sum(one) and plan.status == "gpu" and plan.total_mib == 24576 + 12288
    assert plan.tensor_split == (round(one[0] / sum(one), 3), round(one[1] / sum(one), 3))


def test_a_single_gpu_has_no_tensor_split():
    assert tuning.plan_model(model12b(), hw(gpu(24564, 24000))).tensor_split == ()


def test_weights_larger_than_vram_offload_layers_to_ram():
    plan = tuning.plan_model(model12b(), hw(gpu(6144)), kv_type="f16")
    assert plan.status == "hybrid" and plan.context == tuning.SPILL_CONTEXT
    assert 0 < plan.gpu_layers < 48 and plan.cpu_moe == 0
    assert plan.vram_mib <= plan.budget_mib and plan.ram_mib > 0
    assert any("run from RAM" in line for line in plan.reasoning)


def test_hybrid_that_does_not_fit_in_ram_says_so():
    plan = tuning.plan_model(model12b(), hw(gpu(6144, 5500), ram_total=4096, ram_free=4096), kv_type="q8_0")
    assert plan.status == "does-not-fit" and any("RAM is" in line for line in plan.reasoning)


def test_ram_is_planned_from_the_total_not_what_is_free_now():
    # regression: available RAM shrank while Hal's own models ran, so hybrid plans stopped fitting
    busy = tuning.plan_model(model12b(), hw(gpu(6144), ram_free=500), kv_type="f16")
    idle = tuning.plan_model(model12b(), hw(gpu(6144)), kv_type="f16")
    assert busy == idle and busy.status == "hybrid"


def test_no_gpu_plans_cpu_only_with_a_spill_context():
    plan = tuning.plan_model(model12b(), hw(), kv_type="q8_0")
    assert (plan.status, plan.gpu_layers, plan.context, plan.vram_mib) == ("cpu", 0, tuning.SPILL_CONTEXT, 0)


def test_cpu_only_lowers_context_when_ram_is_short():
    plan = tuning.plan_model(model12b(), hw(ram_total=10240, ram_free=10240), kv_type="f16")
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


def test_place_matches_the_automatic_plan_at_its_own_context():
    card = hw(gpu(24564, 24000))
    auto = tuning.plan_model(model12b(), card, kv_type="f16")
    fit = tuning.place(model12b(), card, kv_type="f16", context=auto.context)
    assert (fit.status, fit.kv_mib, fit.gpu_layers, fit.vram_mib) == (
        "gpu",
        auto.kv_mib,
        auto.gpu_layers,
        auto.vram_mib,
    )


def test_place_moves_layers_to_ram_when_the_cache_is_too_big_for_vram():
    fit = tuning.place(model12b(), hw(gpu(12288)), kv_type="f16", context=131072)
    assert fit.status == "hybrid" and 0 < fit.gpu_layers < 48 and fit.vram_mib <= 12288


def test_place_without_a_gpu_is_cpu_or_does_not_fit():
    assert tuning.place(model12b(), hw(), kv_type="q8_0", context=8192).status == "cpu"
    small_ram = hw(ram_total=8192, ram_free=6000)
    assert tuning.place(model12b(), small_ram, kv_type="f16", context=65536).status == "does-not-fit"


def test_place_refuses_a_context_below_the_floor():
    with pytest.raises(ValueError, match="useful chat"):
        tuning.place(model12b(), hw(gpu(24564)), kv_type="f16", context=512)


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
    exact = tuning.place(gemma_like(), card, kv_type="f16", context=sliding.context)
    assert exact.status == "gpu" and exact.kv_mib == sliding.kv_mib


def test_per_layer_widths_set_the_kv_size_of_a_gemma4_style_model():
    # regression: one head count for every layer overstated Gemma 4's KV about 4x to 8x
    model = model12b(layers=30, sliding_window=1024, kv_full_width=10240, kv_window_width=102400)
    per_tok, fixed, _, estimated = tuning._kv_per_token(model, "f16", 1)  # noqa: SLF001 - the formula under test
    assert (per_tok, fixed, estimated) == (10240 * 2.0, 102400 * 2.0 * 1024, False)


def test_all_layers_by_hand_is_gpu_and_matches_the_automatic_placement():
    card = hw(gpu(24564))
    whole = tuning.place(model12b(), card, kv_type="q8_0", context=32768)
    layers = tuning.place(model12b(), card, kv_type="q8_0", context=32768, gpu_layers=tuning.ALL_LAYERS)
    assert whole == layers and layers.status == "gpu"


def test_zero_layers_by_hand_is_cpu():
    plan = tuning.place(model12b(), hw(gpu(24564)), kv_type="q8_0", context=8192, gpu_layers=0)
    assert (plan.status, plan.gpu_layers, plan.vram_mib) == ("cpu", 0, 0)


def test_some_layers_by_hand_is_hybrid():
    plan = tuning.place(model12b(), hw(gpu(24564)), kv_type="q8_0", context=8192, gpu_layers=10)
    assert plan.status == "hybrid" and plan.gpu_layers == 10 and plan.ram_mib > 0


def test_more_layers_than_the_card_holds_does_not_fit():
    plan = tuning.place(model12b(), hw(gpu(4096)), kv_type="f16", context=32768, gpu_layers=tuning.ALL_LAYERS)
    assert plan.status == "does-not-fit" and "VRAM is" in plan.reasoning[-1]


def test_layers_by_hand_need_a_known_layer_count():
    with pytest.raises(ValueError, match="layer count is unknown"):
        tuning.place(model12b(layers=None), hw(gpu(24564)), kv_type="f16", context=8192, gpu_layers=4)


def test_a_setting_by_hand_may_fill_the_whole_card_past_the_guidance():
    # regression: hand settings were capped at the card less 600 MiB (23.4 GB of a 24 GB card)
    card = hw(gpu(24576))
    auto = tuning.plan_model(model12b(), card, kv_type="f16")
    ctx = auto.context
    while tuning.place(model12b(), card, kv_type="f16", context=ctx + tuning.CONTEXT_STEP).status == "gpu":
        ctx += tuning.CONTEXT_STEP
    fit = tuning.place(model12b(), card, kv_type="f16", context=ctx)
    assert fit.budget_mib < fit.vram_mib <= 24576 and fit.vram_mib > 24576 - 200
    assert any("above the auto-tune guidance" in line for line in fit.reasoning)
    assert auto.vram_mib <= auto.budget_mib


def moe(**over):
    """A 46 GB mixture-of-experts model: 41 GB of routed experts over 48 layers."""
    base = dict(size_bytes=46000 * MIB, layers=48, kv_heads=2, head_dim=256, attention_interval=4,
                context_native=262144, experts=512, expert_bytes=41000 * MIB, vocab=248320,
                host_bytes=260 * MIB)  # fmt: skip
    return model12b(name="moe", **{**base, **over})


def test_a_moe_model_bigger_than_vram_keeps_its_experts_in_ram_not_its_layers():
    plan = tuning.plan_model(moe(), hw(gpu(24576), ram_total=65536))
    assert plan.status == "hybrid" and plan.gpu_layers == tuning.ALL_LAYERS
    assert 0 < plan.cpu_moe < 48 and plan.context == tuning.MOE_SPILL_CONTEXT
    assert plan.vram_mib <= plan.budget_mib and plan.ram_mib > 20000


def test_experts_by_hand_move_that_many_layers_of_experts():
    card = hw(gpu(24576), ram_total=65536)
    few = tuning.place(moe(), card, kv_type="q8_0", context=65536, cpu_moe=20)
    many = tuning.place(moe(), card, kv_type="q8_0", context=65536, cpu_moe=40)
    assert few.cpu_moe == 20 and many.vram_mib < few.vram_mib and many.ram_mib > few.ram_mib
    assert few.status == "does-not-fit" and many.status == "hybrid"


def test_experts_cannot_be_moved_on_a_dense_model():
    with pytest.raises(ValueError, match="not a mixture-of-experts"):
        tuning.place(model12b(), hw(gpu(24576)), kv_type="f16", context=8192, cpu_moe=4)


def test_a_card_held_by_another_program_is_planned_as_if_it_were_free():
    # regression: sizing used free memory, so a card held by Hal's own router gave 0 GPU layers
    busy = tuning.plan_model(model12b(), hw(gpu(24564, 2000)), kv_type="f16")
    idle = tuning.plan_model(model12b(), hw(gpu(24564)), kv_type="f16")
    assert busy == idle and busy.status == "gpu"


def test_a_busy_card_gets_a_one_line_note_naming_the_held_gigabytes():
    warning = tuning.busy_warning(hw(gpu(24576, 12288)))
    assert "12.0 GB of the GPU is in use right now" in warning and "\n" not in warning
    assert tuning.busy_warning(hw(gpu(24576, 24000))) == ""
    assert tuning.busy_warning(hw()) == ""


def test_an_mtp_layer_is_not_counted_on_the_gpu():
    plain = tuning.place(model12b(), hw(gpu(24576)), kv_type="f16", context=8192)
    mtp = tuning.place(model12b(idle_bytes=300 * MIB), hw(gpu(24576)), kv_type="f16", context=8192)
    assert plain.vram_mib - mtp.vram_mib == 300 and plain.weights_mib == mtp.weights_mib


def test_auto_tune_aims_for_the_models_own_maximum_without_a_cap():
    # regression: auto-tune stopped at 131072 tokens even for a model with 262144 of its own
    big = model12b(size_bytes=2000 * MIB, kv_heads=2, attention_interval=4, context_native=262144)
    plan = tuning.plan_model(big, hw(gpu(24576)), kv_type="q8_0")
    assert plan.context == 262144
    assert tuning.plan_model(big, hw(gpu(24576)), kv_type="q8_0", context_cap=65536).context == 65536


def test_a_draft_model_is_counted_as_measured_on_a_3090():
    # Qwen3.8-27B's MTP head: 1854 MiB more VRAM measured; DFlash2 grows with the draft length
    qwen = model12b(vocab=248320)
    assert 1854 <= tuning.draft_mib(1_369_590_656, "draft-mtp", 5, qwen) <= 1854 * 1.02
    dflash = [tuning.draft_mib(1_143_006_816, "draft-dflash", n, qwen) for n in (3, 8, 16)]
    assert all(est >= measured * 0.98 for est, measured in zip(dflash, (2240, 2988, 4184), strict=True))
    assert all(est <= measured * 1.12 for est, measured in zip(dflash, (2240, 2988, 4184), strict=True))


def test_the_offload_makes_room_for_a_draft_model():
    card = hw(gpu(8192))
    plain = tuning.place(model12b(), card, kv_type="q8_0", context=16384)
    drafted = tuning.place(model12b(), card, kv_type="q8_0", context=16384, draft_cost_mib=1500)
    assert drafted.vram_mib <= 8192 and drafted.vram_mib > plain.vram_mib - 1500
    assert any("draft model" in line for line in drafted.reasoning)


def test_the_spec_type_follows_the_draft_file_name():
    assert tuning.spec_type_of("/m/qwen/MTP/mtp-Qwen3.8-27B-Q4_0.gguf") == "draft-mtp"
    assert tuning.spec_type_of("/m/g/gemma-4-31b-it-qat-assistant-Q8_0.gguf") == "draft-mtp"
    assert tuning.spec_type_of("/m/q/DFLASH2/Qwen3.8-27B-DFlash2-Q4_K_M.gguf") == "draft-dflash"
    assert tuning.spec_type_of("/m/g/gemma-4-31B-it-DSpark-Q4_K_M.gguf") == "draft-dspark"
    assert tuning.spec_type_of("/m/small-draft.gguf") == "draft-simple"
