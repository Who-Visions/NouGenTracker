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
import json
import pathlib
from datetime import date, datetime, timedelta, timezone

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
         output_tokens=0, reasoning=0, session_id=SID, timestamp=None):
    row = {
        "session_id": session_id,
        "model": model,
        "input_tokens": input_tokens,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "output_tokens": output_tokens,
        "reasoning": reasoning,
    }
    if timestamp is not None:
        row["timestamp"] = timestamp
    return row


def _utc(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


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


def test_window_bounds_are_bounds_not_a_bill():    # This session's window: 223,638 tokens (session_status, 2026-11-25 ~01:49 EST).
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


# --- Per-row era pricing (Codex P2, review 6053325778) -----------------------
# session_cost used to ignore each row's timestamp and bill every row at
# today's flat rate. A session spanning a published price change therefore
# billed its history at the new rate. Each row is now priced at the rate in
# force at that row's own timestamp.


def test_session_spanning_a_price_change_bills_each_row_at_its_own_era():
    # gemini-4-carbon doubles on 2026-12-01 (published, ai.google.dev):
    #   before: input $2.00 / cache_read $0.10 per MTok
    #   from 2026-12-01: input $4.00 / cache_read $0.20 per MTok
    # Row A (2026-11-30): 1M fresh x 2.00 + 1M cached x 0.10 = $2.10.
    # Row B (2026-12-01): 1M fresh x 4.00 + 1M cached x 0.20 = $4.20.
    # Total $6.30 — NOT $8.40, which is what billing both rows at today's
    # (post-change) rate would give. The cached split is era-priced too.
    total, _by_model, rows, _unpriced = tt.session_cost(SID, [
        _row("gemini-4-carbon", input_tokens=1_000_000, cache_read=1_000_000,
             timestamp=_utc(2026, 11, 30)),
        _row("gemini-4-carbon", input_tokens=1_000_000, cache_read=1_000_000,
             timestamp=_utc(2026, 12, 1)),
    ])
    assert [r["cost_usd"] for r in rows] == [pytest.approx(2.10), pytest.approx(4.20)]
    assert total == pytest.approx(6.30)
    assert total != pytest.approx(8.40)


def test_row_before_billing_start_bills_zero_codex_example():
    # Codex's own example: a gpt-rosalind-research row dated 2026-10-01 with
    # 1M input tokens returned $5 from session_cost, while
    # model_bill(..., when=row['timestamp']) correctly returns $0 — the
    # catalog starts billing on 2026-10-05. The same session on both sides
    # of the start must bill $0 then $5.
    total, _by_model, rows, _unpriced = tt.session_cost(SID, [
        _row("gpt-rosalind-research", input_tokens=1_000_000,
             timestamp=_utc(2026, 10, 1)),
        _row("gpt-rosalind-research", input_tokens=1_000_000,
             timestamp=_utc(2026, 10, 5)),
    ])
    assert [r["cost_usd"] for r in rows] == [pytest.approx(0.0), pytest.approx(5.0)]
    assert total == pytest.approx(5.0)


def test_row_exactly_on_a_price_change_boundary_uses_the_new_rate():
    # Day-granular boundary: 2026-11-30 23:59 is still the old band,
    # 2026-12-01 00:00 is the new band.
    total, _by_model, rows, _unpriced = tt.session_cost(SID, [
        _row("gemini-4-carbon", input_tokens=1_000_000,
             timestamp=_utc(2026, 11, 30, 23, 59)),
        _row("gemini-4-carbon", input_tokens=1_000_000,
             timestamp=_utc(2026, 12, 1, 0, 0)),
    ])
    assert [r["cost_usd"] for r in rows] == [pytest.approx(2.0), pytest.approx(4.0)]
    assert total == pytest.approx(6.0)


def test_row_without_a_timestamp_falls_back_to_todays_flat_rate():
    # Safe default, documented in session_cost: no timestamp means no era
    # to choose, so the flat table (today's rate) answers — the old
    # behaviour, kept for rows that predate timestamp capture.
    total, _by_model, rows, _unpriced = tt.session_cost(SID, [
        _row("gemini-4-carbon", input_tokens=1_000_000),
    ])
    assert total == pytest.approx(2.0)
    assert rows[0]["cost_usd"] == pytest.approx(2.0)


def test_string_and_epoch_timestamps_price_at_their_own_era():
    # Hand-built rows may carry an ISO string or an epoch instead of the
    # tz-aware datetime the parsers write. Both must resolve to the same
    # era as the datetime form — the string's written date, the epoch's UTC
    # date — not silently fall back to today's rate.
    total, _by_model, rows, _unpriced = tt.session_cost(SID, [
        _row("gemini-4-carbon", input_tokens=1_000_000,
             timestamp="2026-11-30T10:00:00Z"),
        _row("gemini-4-carbon", input_tokens=1_000_000,
             timestamp=int(_utc(2026, 12, 1).timestamp())),
        _row("gemini-4-carbon", input_tokens=1_000_000,
             timestamp="not-a-date"),
    ])
    assert [r["cost_usd"] for r in rows] == [
        pytest.approx(2.0), pytest.approx(4.0), pytest.approx(2.0)]
    assert total == pytest.approx(8.0)


# --- Resolver-boundary hardening (PR #50 follow-up, Jensen HOLE-FOUND) -------
# The per-row era fix guarded session_cost, but the resolver itself
# (pricing_live._lookup_price_schedule) trusted str(when)[:10] and compared
# lexicographically, so a garbage / far-future / non-str `when` sorted into
# the newest open-ended band and billed at the highest rate. These tests
# FAIL on the old code (each probe returned the new-era rate or $4.00) and
# pass on the fixed boundary. gemini-4-carbon: old era (2.00, 10.00, 0.10),
# new era (4.00, 20.00, 0.20), flat fallback (2.00, 10.00, 0.10).


def test_direct_garbage_when_falls_back_to_flat_rate():
    # Old code: "not-a-date" >= "2026-12-01" lexicographically ('n' > '2'),
    # so price_for returned the NEW-era rate (4.00, 20.00, 0.20).
    for bad in ("not-a-date", "", "2026-13-40", 12345.6):
        assert tt.price_for("gemini-4-carbon", bad)[:3] == (2.0, 10.0, 0.1)
        assert tt.model_bill(
            "gemini-4-carbon", {"input_tokens": 1_000_000}, bad)[0] == pytest.approx(2.0)


def test_direct_far_future_date_falls_back_to_flat_rate():
    # Old code: the new-era band is open-ended, so any date after 2026-12-01
    # — including a typo year — billed at the highest rate forever.
    assert tt.price_for("gemini-4-carbon", "2099-12-31")[:3] == (2.0, 10.0, 0.1)
    assert tt.model_bill(
        "gemini-4-carbon", {"input_tokens": 1_000_000}, "2099-12-31")[0] == pytest.approx(2.0)


def test_direct_non_string_when_falls_back_to_flat_rate():
    # Old code: str(["2026-11-30"])[:10] is "['2026-11-" ('[' > '2'), and
    # str({"date": ...}) starts with '{' — both sorted into the new era.
    for bad in (["2026-11-30"], {"date": "2026-11-30"}, {"2026-11-30"},
                b"2026-11-30", ("2026-11-30",)):
        assert tt.price_for("gemini-4-carbon", bad)[:3] == (2.0, 10.0, 0.1)


def test_direct_datetime_like_strings_are_not_sliced_into_a_day():
    # A datetime string or a sloppy day string is not a YYYY-MM-DD day:
    # the resolver must reject it (flat fallback), not slice [:10] off it.
    for bad in ("2026-12-01T00:00:00", "2026-12-01 ", " 2026-12-01",
                "20261201", "2027-02-30"):
        assert tt.price_for("gemini-4-carbon", bad)[:3] == (2.0, 10.0, 0.1)


def test_real_near_future_transition_still_resolves():
    # Guard against over-tightening: a published transition a few weeks out
    # is a real future date and must keep resolving (far-future horizon is
    # 366 days past today, not "today").
    assert tt.price_for("gemini-3.6-flash", "2027-01-01")[:3] == (1.5, 7.5, 0.15)
    assert tt.price_for("gemini-3.6-flash", "2026-12-31")[:3] == (0.75, 3.75, 0.075)


def test_date_and_datetime_objects_resolve_their_own_day():
    assert tt.price_for("gemini-4-carbon", date(2026, 11, 30))[:3] == (2.0, 10.0, 0.1)
    assert tt.price_for("gemini-4-carbon", date(2026, 12, 1))[:3] == (4.0, 20.0, 0.2)
    assert tt.price_for("gemini-4-carbon",
                        datetime(2026, 11, 30, 12, 0))[:3] == (2.0, 10.0, 0.1)
    # tz-aware datetime: 23:30 EST 2026-11-30 is 04:30 UTC 2026-12-01.
    assert tt.price_for(
        "gemini-4-carbon",
        datetime(2026, 11, 30, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    )[:3] == (4.0, 20.0, 0.2)


def test_scheduled_price_uses_the_validated_boundary():
    # _scheduled_price duplicated the vulnerable str(when)[:10] pattern and
    # returned the new-era rate for "not-a-date". It now delegates to the
    # validated resolver boundary (zero callers in-tree; kept for API compat).
    assert tt._scheduled_price("gemini-4-carbon", "not-a-date") is None
    assert tt._scheduled_price("gemini-4-carbon", "2099-12-31") is None
    assert tt._scheduled_price("gemini-4-carbon", ["2026-11-30"]) is None
    assert tt._scheduled_price("gemini-4-carbon", "2026-11-30")[:3] == (2.0, 10.0, 0.1)
    assert tt._scheduled_price("gemini-4-carbon", "2026-12-01")[:3] == (4.0, 20.0, 0.2)


def test_non_string_timestamps_bill_at_flat_rate_not_new_era():
    # Old code: _row_when's final `return ts` passed list/dict/set/bytes/
    # tuple through unvalidated, and the resolver mis-billed each row at
    # $4.00 per 1M input instead of the $2.00 flat fallback. `True` is not
    # an epoch either (bool subclasses int), so it falls back too.
    rows = [_row("gemini-4-carbon", input_tokens=1_000_000, timestamp=ts)
            for ts in (["2026-11-30"], {"date": "2026-11-30"}, {"2026-11-30"},
                       b"2026-11-30", ("2026-11-30",), True)]
    total, _by_model, priced, _unpriced = tt.session_cost(SID, rows)
    assert [r["cost_usd"] for r in priced] == [pytest.approx(2.0)] * len(rows)
    assert total == pytest.approx(2.0 * len(rows))


def test_tz_aware_timestamps_bill_on_their_utc_day():
    # The price change is effective at UTC midnight, so the same instant
    # must bill the same era however its timezone is spelled. Old code
    # took the wall-clock day in the timestamp's own tz: 23:30 EST on
    # 2026-11-30 billed $2.00 while the same instant in UTC billed $4.00.
    total, _by_model, rows, _unpriced = tt.session_cost(SID, [
        _row("gemini-4-carbon", input_tokens=1_000_000,
             timestamp="2026-11-30T23:30:00-05:00"),
        _row("gemini-4-carbon", input_tokens=1_000_000,
             timestamp="2026-12-01T04:30:00Z"),
        # Mirror image: 00:30 +14 on 2026-12-01 is 10:30 UTC on 2026-11-30.
        _row("gemini-4-carbon", input_tokens=1_000_000,
             timestamp="2026-12-01T00:30:00+14:00"),
    ])
    assert [r["cost_usd"] for r in rows] == [
        pytest.approx(4.0), pytest.approx(4.0), pytest.approx(2.0)]
    assert total == pytest.approx(10.0)


def test_naive_datetime_and_date_objects_still_bill_on_their_own_day():
    # Naive timestamps have no tz to normalize from, so they keep their own
    # wall-clock day; date objects bill on the day they name.
    total, _by_model, rows, _unpriced = tt.session_cost(SID, [
        _row("gemini-4-carbon", input_tokens=1_000_000,
             timestamp=datetime(2026, 11, 30, 12, 0)),
        _row("gemini-4-carbon", input_tokens=1_000_000,
             timestamp=datetime(2026, 12, 1, 0, 30)),
        _row("gemini-4-carbon", input_tokens=1_000_000,
             timestamp=date(2026, 11, 30)),
        _row("gemini-4-carbon", input_tokens=1_000_000,
             timestamp=date(2026, 12, 1)),
    ])
    assert [r["cost_usd"] for r in rows] == [
        pytest.approx(2.0), pytest.approx(4.0),
        pytest.approx(2.0), pytest.approx(4.0)]
    assert total == pytest.approx(12.0)


def test_fleet_summary_bill_helpers_reject_a_garbage_when():
    # fleet_summary._bill / _bill_cold pass rec["date"] raw into
    # model_bill / price_for. The resolver boundary fix covers them;
    # this pins the path so a future bypass is caught here, not in prod.
    import fleet_summary as fs
    bucket = {"input_tokens": 1_000_000, "output_tokens": 0,
              "cache_creation": 0, "cache_read": 0, "reasoning": 0}
    assert fs._bill("gemini-4-carbon", bucket, "not-a-date") == pytest.approx(2.0)
    assert fs._bill_cold("gemini-4-carbon", bucket, "not-a-date") == pytest.approx(2.0)
    assert fs._bill("gemini-4-carbon", bucket, "2099-12-31") == pytest.approx(2.0)


def test_fleet_summary_bills_a_garbage_daily_date_at_the_flat_rate(tmp_path):
    # End to end: a daily whose "date" field is garbage (filename stays a
    # real date, so the dailies glob still picks it up) must bill at the
    # flat $2.00, not the $4.00 new-era rate the old resolver returned.
    import fleet_summary as fs
    bucket = {"input_tokens": 1_000_000, "output_tokens": 0,
              "cache_creation": 0, "cache_read": 0, "reasoning": 0}
    rec = {
        "counter": "test", "date": "not-a-date", "machine": "m1",
        "invocations": 1,
        "exact": dict(bucket),
        "estimated": {"input_tokens": 0, "output_tokens": 0,
                      "cache_creation": 0, "cache_read": 0, "reasoning": 0},
        "models": {"gemini-4-carbon": dict(bucket)},
        "generated_at": "2026-11-30T23:59:00+00:00",
    }
    d = tmp_path / "m1"
    d.mkdir()
    (d / "2026-11-30.json").write_text(json.dumps(rec), encoding="utf-8")
    summary = fs.fleet_summary(root=tmp_path, paid=0.0)
    assert summary.total_cost == pytest.approx(2.0)
    assert summary.machines["m1"]["cost"] == pytest.approx(2.0)


def test_run_daily_day_bill_rejects_a_garbage_when():
    # run_daily._day_bill passes day.get("date") raw into model_bill.
    # Same boundary fix covers it; pinned here.
    import run_daily
    day = {"date": "not-a-date", "models": {"gemini-4-carbon": {
        "input_tokens": 1_000_000, "output_tokens": 0, "cache_creation": 0,
        "cache_read": 0, "reasoning": 0}}}
    assert run_daily._day_bill(day) == pytest.approx(2.0)
