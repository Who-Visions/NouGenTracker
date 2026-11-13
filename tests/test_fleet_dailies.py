"""Dependency-free tests for the fleet dailies transport.

These never touch a real clone or a real log directory — every test builds its
own dailies tree in tmp_path. The properties under test are the ones that make
cross-machine summing trustworthy: a re-export cannot double-count, a machine
cannot claim another machine's directory, and a corrupt file cannot take the
whole report down.
"""
import importlib.util
import json

import pytest
import pathlib
from datetime import date, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fd = _load("fleet_dailies", "fleet_dailies.py")


def _inv(day, source="Claude Code", model="opus", **tokens):
    rec = {
        "timestamp": datetime.fromisoformat(f"{day}T12:00:00"),
        "source": source,
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": 0,
        "cache_read": 0,
        "reasoning": 0,
    }
    rec.update(tokens)
    return rec


# --- identity -------------------------------------------------------------

def test_slugify_matches_relay_rules():
    assert fd.slugify_machine("KushBoyGroups-Mac-mini.local") == "kushboygroups-mac-mini-local"
    assert fd.slugify_machine("Phoebus") == "phoebus"
    assert fd.slugify_machine("  ") == "unknown-machine"
    assert fd.slugify_machine("blade1tb!!!") == "blade1tb"


def test_explicit_machine_env_wins(monkeypatch):
    monkeypatch.setenv("NOUGEN_MACHINE", "Whoart")
    assert fd.resolve_machine() == "whoart"


# --- rollup ---------------------------------------------------------------

def test_rollup_groups_by_day_source_and_model():
    out = fd.rollup([
        _inv("2026-07-30", input_tokens=100, output_tokens=10),
        _inv("2026-07-30", input_tokens=50, output_tokens=5),
        _inv("2026-07-31", source="Codex", model="gpt", input_tokens=7),
    ])
    assert out["2026-07-30"]["sources"]["Claude Code"]["input_tokens"] == 150
    assert out["2026-07-30"]["invocations"] == 2
    assert out["2026-07-31"]["models"]["gpt"]["input_tokens"] == 7


def test_rollup_accepts_string_timestamps():
    out = fd.rollup([{"timestamp": "2026-07-30T09:00:00", "source": "s",
                      "model": "m", "input_tokens": 3}])
    assert out["2026-07-30"]["sources"]["s"]["input_tokens"] == 3


def test_rollup_skips_records_with_no_usable_timestamp():
    assert fd.rollup([{"timestamp": None, "input_tokens": 5}]) == {}


def test_rollup_adds_privacy_safe_openai_stats_without_double_counting_reasoning():
    invs = [
        dict(_inv("2026-07-30", source="OpenAI Codex", model="gpt",
                  input_tokens=100, cache_read=80, output_tokens=20,
                  reasoning=7), session_id="private-session-a",
             source_file="private-rollout-a.jsonl", openai_total_tokens=120,
             openai_uncached_input_tokens=20,
             openai_context_input_tokens=100, openai_context_window=200,
             openai_plan_type="plus", openai_primary_used_percent=30,
             openai_primary_window_minutes=300,
             openai_primary_resets_at=123),
        dict(_inv("2026-07-30", source="OpenAI Codex", model="gpt",
                  input_tokens=50, cache_read=50, output_tokens=10),
             session_id="private-session-b", openai_total_tokens=60,
             openai_context_input_tokens=150, openai_context_window=200,
             openai_plan_type="plus", openai_primary_used_percent=20,
             openai_primary_window_minutes=300,
             openai_primary_resets_at=456),
    ]
    stats = fd.rollup(invs)["2026-07-30"]["provider_stats"]["OpenAI Codex"]
    assert stats["distinct_sessions"] == 2
    assert stats["total_tokens"] == 180
    assert stats["uncached_input_tokens"] == 20
    assert stats["cache_hit_ratio"] == pytest.approx(130 / 280, abs=0.0001)
    assert stats["peak_context_used_percent"] == 75.0
    assert stats["plan_types"] == ["plus"]
    assert stats["rate_limits"]["primary"]["peak_used_percent"] == 30.0
    blob = json.dumps(stats)
    assert "private-session" not in blob
    assert "private-rollout" not in blob


def test_rollup_openai_stats_are_zero_safe():
    stats = fd.rollup([
        dict(_inv("2026-07-30", source="OpenAI Codex", model="gpt"),
             session_id="s")
    ])["2026-07-30"]["provider_stats"]["OpenAI Codex"]
    assert stats["cache_hit_ratio"] == 0.0
    assert stats["peak_context_used_percent"] == 0.0


def test_rollup_claude_stats_are_like_for_like_with_codex():
    # Claude: 10 plain input + 990 written to cache + 9,000 read from cache.
    # Codex reports no write bucket, so its uncached input already includes
    # the new tokens. The comparable Claude figure is input + cache_creation.
    invs = [
        dict(_inv("2026-11-12", input_tokens=4, cache_creation=600,
                  cache_read=5_000, output_tokens=50), session_id="s1"),
        dict(_inv("2026-11-12", input_tokens=6, cache_creation=390,
                  cache_read=4_000, output_tokens=30), session_id="s2"),
        dict(_inv("2026-11-12", source="OpenAI Codex", model="gpt",
                  input_tokens=700, cache_read=9_300, output_tokens=40),
             session_id="c1", openai_uncached_input_tokens=700,
             openai_total_tokens=10_040),
    ]
    ps = fd.rollup(invs)["2026-11-12"]["provider_stats"]
    claude = ps["Claude Code"]
    assert claude["uncached_input_tokens"] == 1_000  # 10 + 990, not 10
    assert claude["cache_write_tokens"] == 990
    assert claude["cache_hit_ratio"] == pytest.approx(9_000 / 10_000)
    assert claude["distinct_sessions"] == 2
    assert claude["total_tokens"] == 10_000 + 80
    codex = ps["OpenAI Codex"]
    assert codex["uncached_input_tokens"] == 700
    assert codex["cache_hit_ratio"] == pytest.approx(9_300 / 10_000)
    # OpenAI reports no write bucket, so Codex must not claim a measured zero.
    assert "cache_write_tokens" not in codex
    assert "s1" not in json.dumps(claude)


def test_rollup_claude_stats_are_zero_safe():
    stats = fd.rollup([dict(_inv("2026-11-12"), session_id="s")])[
        "2026-11-12"]["provider_stats"]["Claude Code"]
    assert stats["cache_hit_ratio"] == 0.0
    assert stats["uncached_input_tokens"] == 0


# --- export ---------------------------------------------------------------

def test_export_is_idempotent_per_day(tmp_path):
    invs = [_inv("2026-07-30", input_tokens=100)]
    for _ in range(3):
        fd.export_days(invs, machine="boxa", today=date(2026, 7, 31), dailies_dir=tmp_path)

    files = list((tmp_path / "boxa").glob("*.json"))
    assert len(files) == 1, "re-export must replace the day, not accumulate files"
    assert json.loads(files[0].read_text())["totals"]["input_tokens"] == 100


def test_today_is_marked_partial(tmp_path):
    fd.export_days(
        [_inv("2026-07-30"), _inv("2026-07-31")],
        machine="boxa", today=date(2026, 7, 31), dailies_dir=tmp_path,
    )
    done = json.loads((tmp_path / "boxa" / "2026-07-30.json").read_text())
    today = json.loads((tmp_path / "boxa" / "2026-07-31.json").read_text())
    assert done["partial"] is False
    assert today["partial"] is True


def test_export_writes_no_session_detail(tmp_path):
    """These files land in a public repo — only counts may leave the machine."""
    fd.export_days(
        [dict(_inv("2026-07-30"), session_id="/Users/someone/secret-project",
              source_file="transcript.jsonl")],
        machine="boxa", today=date(2026, 7, 31), dailies_dir=tmp_path,
    )
    blob = (tmp_path / "boxa" / "2026-07-30.json").read_text()
    assert "secret-project" not in blob
    assert "transcript.jsonl" not in blob


# --- aggregate ------------------------------------------------------------

def _publish(tmp_path, machine, day, **tokens):
    fd.export_days([_inv(day, **tokens)], machine=machine,
                   today=date(2026, 12, 31), dailies_dir=tmp_path)


def test_aggregate_sums_across_machines(tmp_path):
    _publish(tmp_path, "boxa", "2026-07-30", input_tokens=100)
    _publish(tmp_path, "boxb", "2026-07-30", input_tokens=40)
    agg = fd.aggregate(fd.load_fleet(tmp_path))

    assert agg["machine_count"] == 2
    assert agg["totals"]["input_tokens"] == 140
    assert agg["days"]["2026-07-30"]["input_tokens"] == 140


def test_directory_beats_a_lying_machine_field(tmp_path):
    """The path is what git merged; the field is only what a box claimed."""
    _publish(tmp_path, "boxa", "2026-07-30", input_tokens=100)
    path = tmp_path / "boxa" / "2026-07-30.json"
    record = json.loads(path.read_text())
    record["machine"] = "boxb"
    path.write_text(json.dumps(record))

    agg = fd.aggregate(fd.load_fleet(tmp_path))
    assert list(agg["machines"]) == ["boxa"]


def test_corrupt_and_foreign_schema_files_are_skipped(tmp_path):
    _publish(tmp_path, "boxa", "2026-07-30", input_tokens=100)
    (tmp_path / "boxb").mkdir()
    (tmp_path / "boxb" / "2026-07-30.json").write_text("{not json")
    (tmp_path / "boxc").mkdir()
    (tmp_path / "boxc" / "2026-07-30.json").write_text(json.dumps({"schema": 999}))

    agg = fd.aggregate(fd.load_fleet(tmp_path))
    assert agg["machine_count"] == 1
    assert agg["totals"]["input_tokens"] == 100


def test_freshest_export_wins_for_one_machine_day(tmp_path):
    base = {"schema": fd.SCHEMA_VERSION, "date": "2026-07-30", "partial": False,
            "sources": {}, "models": {}}
    records = [
        dict(base, machine="boxa", generated_at="2026-07-30T01:00:00",
             totals={"input_tokens": 1, "output_tokens": 0, "cache_creation": 0,
                     "cache_read": 0, "reasoning": 0}),
        dict(base, machine="boxa", generated_at="2026-07-30T09:00:00",
             totals={"input_tokens": 500, "output_tokens": 0, "cache_creation": 0,
                     "cache_read": 0, "reasoning": 0}),
    ]
    assert fd.aggregate(records)["totals"]["input_tokens"] == 500


def test_partial_days_are_reported_not_hidden(tmp_path):
    fd.export_days([_inv("2026-07-31")], machine="boxa",
                   today=date(2026, 7, 31), dailies_dir=tmp_path)
    agg = fd.aggregate(fd.load_fleet(tmp_path))
    assert agg["partial"] == [("boxa", "2026-07-31")]


def test_empty_fleet_is_not_an_error(tmp_path):
    agg = fd.aggregate(fd.load_fleet(tmp_path / "nothing-here"))
    assert agg["machine_count"] == 0
    assert agg["totals"]["input_tokens"] == 0


# --- fingerprints, sketches, overlap --------------------------------------

def test_fingerprint_is_stable_and_discriminating():
    a = _inv("2026-07-30", input_tokens=100)
    assert fd.fingerprint(a) == fd.fingerprint(dict(a))
    assert fd.fingerprint(a) != fd.fingerprint(_inv("2026-07-30", input_tokens=101))


def test_jaccard_separates_identical_from_disjoint_sets():
    a = [fd.fingerprint(_inv("2026-07-30", input_tokens=n)) for n in range(200)]
    b = [fd.fingerprint(_inv("2026-07-30", input_tokens=n)) for n in range(1000, 1200)]
    assert fd.jaccard(fd.minhash(a), fd.minhash(a)) == 1.0
    assert fd.jaccard(fd.minhash(a), fd.minhash(b)) < 0.1


def test_jaccard_estimate_tracks_true_overlap():
    """Half-shared sets should estimate near 1/3 (|A∩B|/|A∪B| for 50% overlap)."""
    a = [fd.fingerprint(_inv("2026-07-30", input_tokens=n)) for n in range(400)]
    b = [fd.fingerprint(_inv("2026-07-30", input_tokens=n)) for n in range(200, 600)]
    est = fd.jaccard(fd.minhash(a), fd.minhash(b))
    assert 0.15 < est < 0.55, f"estimate {est} outside sampling error of 1/3"


def test_two_machines_reading_the_same_logs_are_flagged(tmp_path):
    """The failure mode that inflates a total while every file looks correct."""
    invs = [_inv("2026-07-30", input_tokens=n) for n in range(100)]
    for box in ("boxa", "boxb"):
        fd.export_days(invs, machine=box, today=date(2026, 12, 31), dailies_dir=tmp_path)

    agg = fd.aggregate(fd.load_fleet(tmp_path))
    assert agg["overlaps"], "identical logs on two machines must be detected"
    assert agg["overlaps"][0]["machines"] == ["boxa", "boxb"]
    assert agg["overlaps"][0]["jaccard"] > 0.9


def test_independent_machines_are_not_flagged(tmp_path):
    fd.export_days([_inv("2026-07-30", input_tokens=n) for n in range(100)],
                   machine="boxa", today=date(2026, 12, 31), dailies_dir=tmp_path)
    fd.export_days([_inv("2026-07-30", input_tokens=n) for n in range(5000, 5100)],
                   machine="boxb", today=date(2026, 12, 31), dailies_dir=tmp_path)
    assert fd.aggregate(fd.load_fleet(tmp_path))["overlaps"] == []


# --- exact vs estimated ---------------------------------------------------

def test_exact_and_estimated_are_not_summed_together(tmp_path):
    fd.export_days(
        [dict(_inv("2026-07-30", input_tokens=100), exact=True),
         dict(_inv("2026-07-30", input_tokens=300), exact=False)],
        machine="boxa", today=date(2026, 12, 31), dailies_dir=tmp_path,
    )
    agg = fd.aggregate(fd.load_fleet(tmp_path))
    assert agg["exact"]["input_tokens"] == 100
    assert agg["estimated"]["input_tokens"] == 300
    assert agg["totals"]["input_tokens"] == 400
    assert agg["confidence"] == 0.25


def test_confidence_is_one_when_everything_is_measured(tmp_path):
    fd.export_days([dict(_inv("2026-07-30", input_tokens=10), exact=True)],
                   machine="boxa", today=date(2026, 12, 31), dailies_dir=tmp_path)
    assert fd.aggregate(fd.load_fleet(tmp_path))["confidence"] == 1.0


# --- robust anomaly detection ---------------------------------------------

def test_modified_z_score_catches_a_spike_mean_would_hide():
    days = {f"2026-06-{d:02d}": {"input_tokens": 1000} for d in range(1, 15)}
    days["2026-06-16"] = {"input_tokens": 90000}
    found = fd.detect_anomalies(days)
    assert [a["date"] for a in found] == ["2026-06-16"]
    assert found[0]["direction"] == "spike"


def test_a_steady_series_has_no_anomalies():
    days = {f"2026-06-{d:02d}": {"input_tokens": 1000 + d} for d in range(1, 20)}
    assert fd.detect_anomalies(days) == []


def test_too_few_points_declines_to_guess():
    days = {f"2026-06-{d:02d}": {"input_tokens": 1000} for d in range(1, 4)}
    days["2026-06-05"] = {"input_tokens": 999999}
    assert fd.detect_anomalies(days) == []


def test_degenerate_mad_does_not_divide_by_zero():
    """A flat majority makes MAD exactly 0; the fallback must still work."""
    days = {f"2026-06-{d:02d}": {"input_tokens": 1000} for d in range(1, 12)}
    days["2026-06-15"] = {"input_tokens": 500000}
    found = fd.detect_anomalies(days)
    assert [a["date"] for a in found] == ["2026-06-15"]


def test_log_space_catches_what_linear_space_misses():
    """The regression this repo's own history demanded.

    Real daily totals span 3.3e5 to 1.8e8. On raw values MAD is so inflated by
    that spread that nothing clears 3.5 and the detector reports silence; in
    log space the same series has a clear outlier. Numbers below are this
    fleet's actual 17 days.
    """
    real = [328530, 78187832, 54476639, 887763, 17556780, 180852714, 105374360,
            474001, 94570631, 50917905, 55701898, 33410174, 1195972, 101663232,
            3607399, 88173410, 165952539]
    days = {f"2026-05-{i + 1:02d}": {"input_tokens": v} for i, v in enumerate(real)}
    found = fd.detect_anomalies(days)
    assert found, "log-space detection must surface the outlier linear space hides"
    assert found[0]["direction"] == "trough"
    assert found[0]["ratio"] < 1


def test_anomaly_reports_real_tokens_not_the_log():
    days = {f"2026-06-{d:02d}": {"input_tokens": 1_000_000} for d in range(1, 15)}
    days["2026-06-20"] = {"input_tokens": 5}
    found = fd.detect_anomalies(days)
    assert found[0]["tokens"] == 5, "must report the count, not log10 of it"
    assert found[0]["ratio"] < 0.001


# --- spend: dollars as a view over tokens ---------------------------------

def _priced(model, bucket):
    """Stand-in for token_tracker.model_bill — $1/M in, $10/M out, $0.1/M cache."""
    cost = (bucket.get("input_tokens", 0) * 1.0
            + bucket.get("output_tokens", 0) * 10.0
            + bucket.get("cache_read_input_tokens", 0) * 0.1) / 1_000_000
    return cost, "test"


def test_spend_uses_the_billing_field_names_not_the_record_ones():
    """The trap: invocations say `cache_read`, model_bill wants
    `cache_read_input_tokens`. Passing the wrong shape prices cache at zero —
    and cache is 95% of this fleet's volume, so the bill looks plausible."""
    models = {"m": {"input_tokens": 0, "output_tokens": 0, "cache_read": 10_000_000,
                    "cache_creation": 0, "reasoning": 0}}
    assert fd.spend_of(models, _priced)["m"] == pytest.approx(1.0)


def test_no_price_function_means_no_spend_not_zero_spend():
    """Absent pricing must be distinguishable from genuinely free usage."""
    models = {"m": {"input_tokens": 1_000_000}}
    assert fd.spend_of(models, None) == {}


def test_spend_is_not_stored_in_the_daily_record(tmp_path):
    """Tokens are the measurement; dollars are a view. A frozen cost would bake
    each machine's pricing bugs into the fleet total permanently."""
    fd.export_days([_inv("2026-07-30", model="m", input_tokens=1000)],
                   machine="boxa", today=date(2026, 12, 31), dailies_dir=tmp_path)
    record = json.loads((tmp_path / "boxa" / "2026-07-30.json").read_text())
    assert not any("cost" in k or "spend" in k or "usd" in k for k in record)


def test_a_price_correction_applies_retroactively(tmp_path):
    """One table fix must re-price every machine, including old days."""
    fd.export_days([_inv("2026-07-30", model="m", output_tokens=1_000_000)],
                   machine="boxa", today=date(2026, 12, 31), dailies_dir=tmp_path)
    records = fd.load_fleet(tmp_path)

    cheap = fd.aggregate(records, price_fn=lambda m, b: (b["output_tokens"] * 1.0 / 1e6, "x"))
    dear = fd.aggregate(records, price_fn=lambda m, b: (b["output_tokens"] * 5.0 / 1e6, "x"))
    assert cheap["spend"]["total"] == pytest.approx(1.0)
    assert dear["spend"]["total"] == pytest.approx(5.0)
    assert cheap["totals"] == dear["totals"], "tokens must not move when prices do"


def test_spend_splits_by_machine_and_model(tmp_path):
    for box, model, out in (("boxa", "opus", 1_000_000), ("boxb", "haiku", 2_000_000)):
        fd.export_days([_inv("2026-07-30", model=model, output_tokens=out)],
                       machine=box, today=date(2026, 12, 31), dailies_dir=tmp_path)
    spend = fd.aggregate(fd.load_fleet(tmp_path), price_fn=_priced)["spend"]

    assert spend["by_machine"]["boxa"] == pytest.approx(10.0)
    assert spend["by_machine"]["boxb"] == pytest.approx(20.0)
    assert spend["by_model"]["haiku"] == pytest.approx(20.0)
    assert spend["total"] == pytest.approx(30.0)


def test_an_unpriceable_model_does_not_take_the_report_down(tmp_path):
    def explodes(model, bucket):
        if model == "bad":
            raise ValueError("no price for this")
        return 1.0, "x"

    models = {"bad": {"input_tokens": 1}, "good": {"input_tokens": 1}}
    assert fd.spend_of(models, explodes) == {"good": 1.0}


def test_token_totals_are_unchanged_when_no_prices_are_supplied(tmp_path):
    fd.export_days([_inv("2026-07-30", model="m", input_tokens=500)],
                   machine="boxa", today=date(2026, 12, 31), dailies_dir=tmp_path)
    agg = fd.aggregate(fd.load_fleet(tmp_path))
    assert agg["totals"]["input_tokens"] == 500
    assert agg["spend"]["priced"] is False


# --- dirty-tree counters --------------------------------------------------

def test_a_clean_tree_stamps_a_plain_counter(monkeypatch):
    """Asserted against the function's own branch rather than the ambient repo.

    The first version of this read the real working tree, so it passed in CI
    (fresh checkout) and failed on every developer machine mid-edit — including
    the edit that introduced it. A test that only holds when nobody is working
    is not testing the code.
    """
    monkeypatch.setattr(fd, "_tracker_differs_from_head", lambda path: False)
    assert not fd.counter_fingerprint().endswith(fd.DIRTY_SUFFIX)


def test_the_real_tree_agrees_with_its_own_git_state():
    """The end-to-end version, skipped rather than failed while editing."""
    dirty = fd._tracker_differs_from_head(fd.TRACKER_SOURCE)
    counter = fd.counter_fingerprint()
    assert counter.endswith(fd.DIRTY_SUFFIX) == dirty


def test_an_uncommitted_tracker_edit_is_marked_dirty(tmp_path, monkeypatch):
    """The regression, with the attribution corrected 2026-08-01.

    It was phoebus's 17 dailies that carry an unreproducible stamp
    (22555db5d239), not whoart's 15. Measured after the fact: 71aef8ff08fa is
    reproduced by five commits — main, 01f5d76, dce1f97, d3a422b and this
    branch's own token_tracker.py — under BOTH implementations of the
    fingerprint, while no commit in the repository produces 22555db5d239.

    The diagnosis was right and this test is worth keeping; only the box was
    wrong. It was reached by taking one machine's value as the reference and
    concluding the other was fabricated, which is the same shape of error as
    reading your own push as another machine's work.
    """
    tracker = fd.TRACKER_SOURCE
    original = tracker.read_text(encoding="utf-8")
    try:
        # Change a counting function's behaviour, do not commit it.
        edited = original.replace("def usage_of(rec):", "def usage_of(rec):\n    _ = 1", 1)
        assert edited != original, "anchor for the edit was not found"
        tracker.write_text(edited, encoding="utf-8")

        counter = fd.counter_fingerprint()
        assert counter.endswith(fd.DIRTY_SUFFIX), (
            "a counter computed from uncommitted code must never look reproducible"
        )
    finally:
        tracker.write_text(original, encoding="utf-8")


def test_two_exports_from_the_same_dirty_tree_still_match(tmp_path):
    """Dirty must stay comparable to itself — the marker records provenance,
    it does not make every run unique."""
    tracker = fd.TRACKER_SOURCE
    original = tracker.read_text(encoding="utf-8")
    try:
        tracker.write_text(
            original.replace("def usage_of(rec):", "def usage_of(rec):\n    _ = 2", 1),
            encoding="utf-8",
        )
        assert fd.counter_fingerprint() == fd.counter_fingerprint()
    finally:
        tracker.write_text(original, encoding="utf-8")


def test_unknowable_provenance_is_not_reported_as_dirty(tmp_path):
    """Outside a work tree there is no HEAD to compare against. That is the
    same answer a clean checkout gives, and it is the right default for the
    many places this runs with no repository at all."""
    stray = tmp_path / "token_tracker.py"
    stray.write_text("def usage_of(rec):\n    return {}\n", encoding="utf-8")
    assert not fd.counter_fingerprint(stray).endswith(fd.DIRTY_SUFFIX)


def test_a_dirty_counter_does_not_collide_with_its_clean_form():
    """The marked and unmarked forms must be distinguishable as strings, so an
    aggregator grouping by counter never merges them."""
    clean = fd.counter_fingerprint()
    assert clean + fd.DIRTY_SUFFIX != clean


# --- windowing ------------------------------------------------------------

def _publish_dated(root, machine, day):
    """Minimal well-formed daily record on disk, addressed by machine and day."""
    d = root / machine
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.json").write_text(
        json.dumps({"schema": fd.SCHEMA_VERSION, "date": day, "machine": machine}),
        encoding="utf-8",
    )


def test_load_fleet_windows_by_date(tmp_path):
    """The defect this pins: --fleet ignored --days entirely, so a 3-day and a
    31-day request returned byte-identical totals while echoing the requested
    period back. A wrong answer that looks exactly like a right one."""
    for day in ("2026-08-01", "2026-08-15", "2026-08-30"):
        _publish_dated(tmp_path, "blade1tb", day)

    assert len(fd.load_fleet(tmp_path)) == 3
    narrow = fd.load_fleet(tmp_path, since="2026-08-29")
    wide = fd.load_fleet(tmp_path, since="2026-07-01")
    assert len(narrow) == 1 and len(wide) == 3, "a window must change the result"
    assert narrow != wide


def test_window_bounds_are_inclusive(tmp_path):
    """Callers pass the day they mean, not the day before it."""
    _publish_dated(tmp_path, "whoart", "2026-08-10")
    assert len(fd.load_fleet(tmp_path, since="2026-08-10")) == 1
    assert len(fd.load_fleet(tmp_path, until="2026-08-10")) == 1
    assert len(fd.load_fleet(tmp_path, since="2026-08-11")) == 0
    assert len(fd.load_fleet(tmp_path, until="2026-08-09")) == 0


def test_undated_record_is_dropped_only_when_a_window_is_asked_for(tmp_path):
    """A record with no date cannot be placed in a window. Including it would
    silently pad a bounded report with an unbounded row; excluding it from an
    UNbounded report would lose data nobody asked to filter."""
    d = tmp_path / "phoebus"
    d.mkdir(parents=True)
    (d / "undated.json").write_text(
        json.dumps({"schema": fd.SCHEMA_VERSION, "machine": "phoebus"}),
        encoding="utf-8",
    )
    assert len(fd.load_fleet(tmp_path)) == 1
    assert len(fd.load_fleet(tmp_path, since="2026-01-01")) == 0
