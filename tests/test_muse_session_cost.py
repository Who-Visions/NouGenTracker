"""Muse Spark per-session cost — hand-computed expectations, no fixtures.

Adapted from the standalone `muse_session_cost.py` tests in
~/workspace/nougentracker-muse-rates-patch/ to the repo loader
(`tt = _load("token_tracker", "token_tracker.py")`), calling
`tt.session_cost` / `tt.muse_window_bounds` / `tt.price_for`.

Each request is priced by ITS OWN model via `model_bill` — never sum tokens
across models under one rate (the Duet mispricing bug: helper-model tokens
under one expensive model's rate).
"""
import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tt = _load("token_tracker", "token_tracker.py")

SID = "sess-muse-test"


def _row(model, input_tokens=0, cache_read=0, cache_creation=0,
         output_tokens=0, reasoning=0, session_id=SID):
    return {
        "session_id": session_id,
        "model": model,
        "input_tokens": input_tokens,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "output_tokens": output_tokens,
        "reasoning": reasoning,
    }


def test_rate_card_standard_is_the_verified_list_price():
    assert tt.price_for("muse-spark-1.3")[:3] == (1.25, 4.25, 0.15)
    assert tt.price_for("muse-spark")[:3] == (1.25, 4.25, 0.15)
    assert tt.price_for("meta/muse-spark")[:3] == (1.25, 4.25, 0.15)
    assert tt.price_for("muse-spark-1.3")[3] == tt.DOC
    assert tt.price_for("muse-spark-1.3")[:3] != tt.DEFAULT_PRICING[:3]


def test_rate_card_contributor_is_the_verified_list_price():
    assert tt.price_for("muse-spark-1.3-contributor")[:3] == (0.10, 0.20, 0.002)
    assert tt.price_for("meta/muse-spark-contributor")[:3] == (0.10, 0.20, 0.002)


def test_cache_read_is_priced_as_cache_read_not_fresh_input():
    # 50,000 cached tokens at $0.15/MTok = $0.0075; at fresh $1.25 it would be $0.0625.
    total, _by_model, rows, _unpriced = tt.session_cost(SID, [
        _row("muse-spark", cache_read=50_000),
    ])
    assert total == pytest.approx(0.0075)
    assert rows[0]["cost_usd"] == pytest.approx(0.0075)


def test_per_request_cost_hand_computed():
    # R1: fresh 100,000 x 1.25 + cached 50,000 x 0.15 + out 10,000 x 4.25
    #   = 125,000 + 7,500 + 42,500 = 175,000 / 1e6 = $0.175
    total, _by_model, rows, _unpriced = tt.session_cost(SID, [
        _row("muse-spark", input_tokens=100_000, cache_read=50_000, output_tokens=10_000),
    ])
    assert total == pytest.approx(0.175)
    assert rows[0]["cost_usd"] == pytest.approx(0.175)


def test_reasoning_bills_at_the_output_rate():
    # output 10,000 + reasoning 5,000, both at 4.25 = 15,000 x 4.25 = 63,750 / 1e6
    total, _by_model, _rows, _unpriced = tt.session_cost(SID, [
        _row("muse-spark", output_tokens=10_000, reasoning=5_000),
    ])
    assert total == pytest.approx(0.06375)


def test_session_sum_is_the_sum_of_per_request_costs():
    # R1 $0.175 (above)
    # R2: fresh 20,000 x 1.25 + cached 200,000 x 0.15 + out 5,000 x 4.25
    #   = 25,000 + 30,000 + 21,250 = 76,250 / 1e6 = $0.07625
    # R3 (contributor): fresh 1,000,000 x 0.10 + cached 500,000 x 0.002 + out 100,000 x 0.20
    #   = 100,000 + 1,000 + 20,000 = 121,000 / 1e6 = $0.121
    # Total = 0.175 + 0.07625 + 0.121 = $0.37225
    total, by_model, rows, unpriced = tt.session_cost(SID, [
        _row("muse-spark", input_tokens=100_000, cache_read=50_000, output_tokens=10_000),
        _row("muse-spark", input_tokens=20_000, cache_read=200_000, output_tokens=5_000),
        _row("muse-spark-1.3-contributor", input_tokens=1_000_000, cache_read=500_000, output_tokens=100_000),
        _row("muse-spark", input_tokens=1_000, session_id="other-session"),
    ])
    assert total == pytest.approx(0.37225)
    assert len(rows) == 3 and unpriced == []
    assert by_model["muse-spark"]["requests"] == 2
    assert by_model["muse-spark"]["cost_usd"] == pytest.approx(0.25125)
    assert by_model["muse-spark-1.3-contributor"]["cost_usd"] == pytest.approx(0.121)


def test_mixed_models_do_not_collapse_into_one_rate():
    # The Duet bug: helper-model tokens summed under one expensive model's rate.
    # A claude-haiku-4-5 request (1.00/5.00/0.10) priced at Muse Standard rates
    # would read $0.1675; at its own rates it is $0.15. The session total must
    # carry the $0.15, and the two models must stay separate in by_model.
    total, by_model, _rows, _unpriced = tt.session_cost(SID, [
        _row("claude-haiku-4-5", input_tokens=100_000, output_tokens=10_000),
        _row("muse-spark", input_tokens=100_000, output_tokens=10_000),
    ])
    assert by_model["claude-haiku-4-5"]["cost_usd"] == pytest.approx(0.15)
    assert by_model["muse-spark"]["cost_usd"] == pytest.approx(0.1675)
    assert by_model["claude-haiku-4-5"]["cost_usd"] != by_model["muse-spark"]["cost_usd"]
    assert total == pytest.approx(0.3175)


def test_cache_write_bills_at_the_generic_multiplier_until_a_rate_is_verified():
    # OPEN ITEM (PATCH.md §3): no verified Muse cache-write rate exists, so the
    # tracker bills cache creation at its generic 1.25x input multiplier.
    # R: fresh 10,000 x 1.25 + cache-write 5,000 x 1.25 x 1.25 + out 1,000 x 4.25
    #   = 12,500 + 7,812.5 + 4,250 = 24,562.5 / 1e6 = $0.0245625.
    total, _by_model, _rows, _unpriced = tt.session_cost(SID, [
        _row("muse-spark", input_tokens=10_000, cache_creation=5_000, output_tokens=1_000),
    ])
    assert total == pytest.approx(0.0245625)


def test_unknown_models_fall_back_to_the_default_inside_a_session():
    # model_bill always resolves (DEFAULT_PRICING fallback), so session_cost
    # never leaves a row unpriced today; the unpriced list only fills if a
    # future model_bill returns None. The fallback is the tracker's standing
    # rule (see test_pricing.py), not a silent $0.
    total, _by_model, rows, unpriced = tt.session_cost(SID, [
        _row("some-model-nobody-has-heard-of", input_tokens=1_000_000),
    ])
    assert total == pytest.approx(1.00)
    assert len(rows) == 1 and unpriced == []


def test_window_bounds_are_bounds_not_a_bill():
    # This session's window: 223,638 tokens (session_status, 2026-11-25 ~01:49 EST).
    # Exact values: 0.2795475 / 0.9504615 / 0.0335457 (floats; round() on the
    # output figure gives 0.950461, so compare with approx, not round()).
    b = tt.muse_window_bounds(223_638)
    assert b["all_fresh_input_usd"] == pytest.approx(0.2795475, abs=1e-9)
    assert b["all_output_usd"] == pytest.approx(0.9504615, abs=1e-9)
    assert b["all_cached_input_usd"] == pytest.approx(0.0335457, abs=1e-9)
    assert "not a bill" in b["label"]
    assert b["window_tokens"] == 223_638


def test_window_assumption_split_matches_the_reference_math():
    # Assumption split only: 85% cached / 10% fresh / 5% out on 223,638 tokens.
    # cached 190,092.3 x 0.15 = 0.0285138; fresh 22,363.8 x 1.25 = 0.0279548;
    # out 11,181.9 x 4.25 = 0.0475231; total 0.1039917 ≈ $0.104.
    b = tt.muse_window_bounds(223_638)
    n = 223_638
    total = (n * 0.85 * 0.15 + n * 0.10 * 1.25 + n * 0.05 * 4.25) / 1_000_000
    assert round(total, 6) == 0.103992
    assert abs(total - 0.104) < 0.00001
    assert b["all_cached_input_usd"] <= total <= b["all_output_usd"]
