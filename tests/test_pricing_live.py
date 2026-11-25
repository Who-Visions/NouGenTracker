"""Tests for pricing_live.py dynamic pricing resolver and token_tracker wiring.

Covers:
  - Saved fixture parsing for Anthropic, Gemini, OpenAI
  - Graceful degradation on unknown/broken HTML
  - Resolution order:
      (a) Env override NOUGEN_PRICE_<MODELKEY> winning over cache/live/const
      (b) Valid cache within TTL
      (c) TTL-expired cache falling through to live fetch (and updating cache)
      (d) Fetch failure falling back to MODEL_PRICING with 'fallback-const' tag
      (e) Unknown model falling through to DEFAULT_PRICING
  - Network discipline: single fetch attempt per vendor per process
  - Integration with token_tracker.price_for
"""

import datetime
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

import pricing_live


def _load_tracker():
    spec = importlib.util.spec_from_file_location("token_tracker", ROOT / "token_tracker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def clean_resolver_state(monkeypatch, tmp_path):
    """Ensure clean process state and isolated cache for each test."""
    pricing_live.reset_process_state()
    # Route cache to isolated tmp_path
    tmp_cache = tmp_path / "pricing_cache.json"
    monkeypatch.setenv("NOUGEN_PRICING_CACHE_PATH", str(tmp_cache))
    monkeypatch.setenv("NOUGEN_PRICING_TTL_HOURS", "24")
    yield tmp_cache
    pricing_live.reset_process_state()


# --- 1. Fixture Parsers ------------------------------------------------------

def test_parse_anthropic_fixture():
    fixture_path = FIXTURES_DIR / "anthropic_pricing.md"
    assert fixture_path.is_file(), "Anthropic fixture missing"
    text = fixture_path.read_text(encoding="utf-8")
    parsed = pricing_live.parse_anthropic_pricing(text)

    assert len(parsed) > 0, "Anthropic parser returned no models"
    # Claude Opus 5 ($5 input, $25 output, $0.50 cache read)
    assert "claude-opus-5" in parsed
    assert parsed["claude-opus-5"] == (5.0, 25.0, 0.5)

    # Claude Sonnet 5 ($2 input, $10 output, $0.20 cache read)
    assert "claude-sonnet-5" in parsed
    assert parsed["claude-sonnet-5"] == (2.0, 10.0, 0.2)

    # Claude Haiku 4.5 ($1 input, $5 output, $0.10 cache read)
    assert "claude-haiku-4-5" in parsed or "claude-haiku-4.5" in parsed
    key = "claude-haiku-4-5" if "claude-haiku-4-5" in parsed else "claude-haiku-4.5"
    assert parsed[key] == (1.0, 5.0, 0.1)


def test_parse_gemini_fixture():
    fixture_path = FIXTURES_DIR / "gemini_pricing.html"
    assert fixture_path.is_file(), "Gemini fixture missing"
    text = fixture_path.read_text(encoding="utf-8")
    parsed = pricing_live.parse_gemini_pricing(text)

    assert len(parsed) > 0, "Gemini parser returned no models"
    # Gemini 3.5 Flash ($1.50 in, $9.00 out, $0.15 cache)
    assert "gemini-3.5-flash" in parsed or "gemini-3-5-flash" in parsed
    k35 = "gemini-3.5-flash" if "gemini-3.5-flash" in parsed else "gemini-3-5-flash"
    assert parsed[k35] == (1.50, 9.00, 0.15)

    # Gemini 3.1 Flash-Lite ($0.25 in, $1.50 out, $0.025 cache)
    assert "gemini-3.1-flash-lite" in parsed or "gemini-3-1-flash-lite" in parsed
    k31 = "gemini-3.1-flash-lite" if "gemini-3.1-flash-lite" in parsed else "gemini-3-1-flash-lite"
    assert parsed[k31] == (0.25, 1.50, 0.025)

    # Gemini 2.5 Pro ($1.25 in, $10.00 out, $0.125 cache)
    assert "gemini-2.5-pro" in parsed or "gemini-2-5-pro" in parsed
    k25 = "gemini-2.5-pro" if "gemini-2.5-pro" in parsed else "gemini-2-5-pro"
    assert parsed[k25] == (1.25, 10.00, 0.125)


def test_parse_openai_fixture():
    fixture_path = FIXTURES_DIR / "openai_pricing.html"
    assert fixture_path.is_file(), "OpenAI fixture missing"
    text = fixture_path.read_text(encoding="utf-8")
    parsed = pricing_live.parse_openai_pricing(text)

    assert len(parsed) > 0, "OpenAI parser returned no models"
    # chat-latest ($5.00 in, $30.00 out, $0.50 cache)
    assert "chat-latest" in parsed
    assert parsed["chat-latest"] == (5.0, 30.0, 0.5)

    # gpt-5.3-codex ($1.75 in, $14.00 out, $0.175 cache)
    assert "gpt-5.3-codex" in parsed or "gpt-5-3-codex" in parsed
    kcodex = "gpt-5.3-codex" if "gpt-5.3-codex" in parsed else "gpt-5-3-codex"
    assert parsed[kcodex] == (1.75, 14.00, 0.175)

    # gpt-5.4-mini ($0.75 in, $4.50 out, $0.075 cache)
    assert "gpt-5.4-mini" in parsed or "gpt-5-4-mini" in parsed
    kmini = "gpt-5.4-mini" if "gpt-5.4-mini" in parsed else "gpt-5-4-mini"
    assert parsed[kmini] == (0.75, 4.50, 0.075)


def test_unknown_layout_degrades_gracefully():
    """Unknown table layouts must degrade to no-data: never crash, never guess."""
    bad_html = "<html><body><table><tr><td>Foo</td><td>Bar</td></tr></table></body></html>"
    bad_md = "| Random | Column |\n|---|---|\n| hello | world |\n"

    assert pricing_live.parse_anthropic_pricing(bad_html) == {}
    assert pricing_live.parse_anthropic_pricing(bad_md) == {}
    assert pricing_live.parse_gemini_pricing(bad_html) == {}
    assert pricing_live.parse_openai_pricing(bad_html) == {}


# --- 2. Resolution Order Tests -----------------------------------------------

def test_resolution_order_env_override_wins(monkeypatch, clean_resolver_state):
    """Tier (a) env override NOUGEN_PRICE_<MODELKEY>="in,out,cacheread" wins over all else."""
    model = "test-model-alpha"
    # Populate lower tiers: cache, live mock, fallback dict
    clean_resolver_state.write_text(
        json.dumps({
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "models": {model: [1.0, 2.0, 0.1]},
        }),
        encoding="utf-8",
    )
    fallback = {model: (5.0, 10.0, 0.5, pricing_live.DOC)}

    # Set env override
    monkeypatch.setenv("NOUGEN_PRICE_TEST_MODEL_ALPHA", "99.0, 199.0, 9.9")

    price = pricing_live.resolve_price(model, fallback_pricing=fallback)
    assert price[0] == 99.0
    assert price[1] == 199.0
    assert price[2] == 9.9
    assert price[3] == pricing_live.ENV
    assert price[3] == "env"


def test_resolution_order_valid_cache_hits(clean_resolver_state):
    """Tier (b): valid cache within TTL answers with CACHED ('doc-cached')."""
    model = "cached-model-beta"
    clean_resolver_state.write_text(
        json.dumps({
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "models": {model: [3.5, 14.0, 0.35]},
        }),
        encoding="utf-8",
    )
    fallback = {model: (1.0, 4.0, 0.1, pricing_live.DOC)}

    # Ensure no network fetch happens: mark all vendors attempted
    pricing_live._ATTEMPTED_VENDORS.update(["anthropic", "gemini", "openai"])

    price = pricing_live.resolve_price(model, fallback_pricing=fallback)
    assert price[0] == 3.5
    assert price[1] == 14.0
    assert price[2] == 0.35
    assert price[3] == pricing_live.CACHED
    assert price[3] == "doc-cached"


def test_ttl_expired_cache_falls_through_to_fetch(clean_resolver_state, monkeypatch):
    """Tier (c): TTL-expired cache triggers live fetch, updates cache, and answers with LIVE."""
    model = "gemini-live-model"
    # Write cache with timestamp 48 hours old (TTL is 24h)
    old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)).isoformat()
    clean_resolver_state.write_text(
        json.dumps({
            "fetched_at": old_time,
            "models": {model: [1.0, 2.0, 0.1]},
        }),
        encoding="utf-8",
    )

    # Mock fetch_vendor_pricing to return updated rates. The update stays
    # within the delta guard (< NOUGEN_PRICING_DELTA_MAX_PCT, default 50%) so
    # this test exercises TTL fall-through alone; a larger jump is the delta
    # guard's own test, where the OLD value must answer and the new one is
    # quarantined.
    def mock_fetch(vendor, timeout_s=None):
        if vendor == "gemini":
            return ({model: (1.4, 2.8, 0.14)}, "https://ai.google.dev/pricing")
        return ({}, "")

    monkeypatch.setattr(pricing_live, "fetch_vendor_pricing", mock_fetch)

    price = pricing_live.resolve_price(model)
    assert price[0] == 1.4
    assert price[1] == 2.8
    assert price[2] == 0.14
    assert price[3] == pricing_live.LIVE
    assert price[3] == "doc-live"

    # Verify cache on disk was refreshed
    data = json.loads(clean_resolver_state.read_text(encoding="utf-8"))
    assert model in data["models"]
    assert data["models"][model] == [1.4, 2.8, 0.14]


def test_fetch_failure_falls_back_to_model_pricing_with_fallback_const(monkeypatch):
    """Tier (d): fetch failure falls back to MODEL_PRICING with 'fallback-const' tag."""
    model = "claude-fallback-model"
    fallback = {model: (5.0, 25.0, 0.5, pricing_live.DOC)}

    # Mock fetch to fail completely
    def mock_fail(vendor, timeout_s=None):
        return ({}, "")

    monkeypatch.setattr(pricing_live, "fetch_vendor_pricing", mock_fail)

    price = pricing_live.resolve_price(model, fallback_pricing=fallback)
    assert price[0] == 5.0
    assert price[1] == 25.0
    assert price[2] == 0.5
    assert price[3] == "fallback-const"
    assert price[3] == pricing_live.FALLBACK_CONST


def test_unknown_model_still_hits_default(monkeypatch):
    """Tier (e): model not in env, cache, live, or fallback hits DEFAULT_PRICING."""
    # Ensure network is mock-failed
    monkeypatch.setattr(pricing_live, "fetch_vendor_pricing", lambda v, **kw: ({}, ""))
    default = (1.00, 4.00, 0.100, pricing_live.EST)

    price = pricing_live.resolve_price("completely-fictional-model-xyz", default_pricing=default)
    assert price == default
    assert price[3] == pricing_live.EST
    assert price[3] == "est"


def test_variant_inference_from_family(clean_resolver_state):
    """Unlisted variant strips suffix and inherits family price tagged EST."""
    clean_resolver_state.write_text(
        json.dumps({
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "models": {"gemini-4-flash": [1.5, 9.0, 0.15]},
        }),
        encoding="utf-8",
    )
    pricing_live._ATTEMPTED_VENDORS.update(["anthropic", "gemini", "openai"])

    # gemini-4-flash-high should strip -high and infer from gemini-4-flash
    price = pricing_live.resolve_price("gemini-4-flash-high")
    assert price[:3] == (1.5, 9.0, 0.15)
    assert price[3] == pricing_live.EST


# --- 3. Network Discipline Tests ---------------------------------------------

def test_network_discipline_one_fetch_attempt_per_vendor():
    """One fetch attempt per vendor per process."""
    call_counts = {"anthropic": 0}

    # Store original vendor sources
    orig_sources = list(pricing_live.VENDOR_SOURCES)
    try:
        # Mock urllib to count calls
        import urllib.request

        def mock_urlopen(req, *args, **kwargs):
            call_counts["anthropic"] += 1
            raise urllib.error.URLError("Simulated offline network")

        pricing_live.urllib.request.urlopen = mock_urlopen

        # First call: should attempt fetch
        pricing_live.fetch_vendor_pricing("anthropic")
        assert call_counts["anthropic"] >= 1

        first_count = call_counts["anthropic"]

        # Second call in same process: must be skipped
        pricing_live.fetch_vendor_pricing("anthropic")
        assert call_counts["anthropic"] == first_count
    finally:
        pricing_live.VENDOR_SOURCES = orig_sources


# --- 4. Integration with token_tracker ---------------------------------------

def test_token_tracker_price_for_uses_resolver():
    """token_tracker.price_for delegates undated lookups to pricing_live."""
    tt = _load_tracker()
    # Free lanes remain $0
    assert tt.price_for("sol-ai:e4b")[:3] == (0.0, 0.0, 0.0)

    # Dated prices are respected
    assert tt.price_for("claude-sonnet-5", "2026-08-15")[:3] == (2.0, 10.0, 0.2)
    assert tt.price_for("claude-sonnet-5", "2026-09-02")[:3] == (2.0, 10.0, 0.2)

    # Known model resolves valid price
    opus_price = tt.price_for("claude-opus-4-8")
    assert opus_price[:3] == (5.0, 25.0, 0.5)

    # Unknown model falls through to DEFAULT_PRICING
    assert tt.price_for("ghost-model-never-existed") == tt.DEFAULT_PRICING


# --- Gate 2.c per-family cache ratio (hyperion follow-ups 3+4, 2026-09-21) --
def test_mythos_tier_cache_ratio_passes_gate():
    from pricing_live import validate_live_price
    # live page lists cache_read at 0.025 x input for the 5.1 tier
    assert validate_live_price("claude-fable-5.1", 10.0, 50.0, 0.25) == (True, "")
    assert validate_live_price("claude-mythos-5.1", 10.0, 50.0, 0.25) == (True, "")


def test_other_claude_families_still_require_ten_percent():
    from pricing_live import validate_live_price
    assert validate_live_price("claude-opus-5", 5.0, 25.0, 0.5)[0]
    ok, reason = validate_live_price("claude-opus-5", 5.0, 25.0, 0.125)  # 0.025 is wrong for opus
    assert not ok and "differs from 0.1" in reason


def test_mythos_tier_wrong_ratio_still_rejected():
    from pricing_live import validate_live_price
    ok, reason = validate_live_price("claude-fable-5.1", 10.0, 50.0, 1.0)  # 0.10 is wrong for fable-5.1
    assert not ok and "differs from 0.025" in reason


def test_point_release_inherits_parent_rate_instead_of_default(monkeypatch):
    """claude-fable-5-1 / claude-mythos-5-1 / claude-opus-5-5 fell all the way
    to the $1/$4/$0.1 unknown-model default because no live/cached price
    passed the invariant gates and the static table only has the base model
    (claude-fable-5). Stripping a trailing numeric point-release segment,
    same as the existing "high"/"preview" suffix stripping, recovers the
    parent's real rate instead of a made-up number."""
    monkeypatch.setattr(pricing_live, "_ATTEMPTED_VENDORS", {v for v, _ in pricing_live.VENDOR_SOURCES})
    fallback = {"claude-fable-5": (10.00, 50.00, 1.000, "doc")}
    inp, out, cache_read, src = pricing_live.resolve_price(
        "claude-fable-5-1", fallback_pricing=fallback, default_pricing=(1.0, 4.0, 0.1, "default-unpriced"))
    assert (inp, out, cache_read) == (10.00, 50.00, 1.000)
    assert src == pricing_live.EST


def test_unresolvable_point_release_still_falls_back_safely(monkeypatch):
    """No parent entry anywhere -> still the safe default, not a crash or a guess."""
    monkeypatch.setattr(pricing_live, "_ATTEMPTED_VENDORS", {v for v, _ in pricing_live.VENDOR_SOURCES})
    inp, out, cache_read, src = pricing_live.resolve_price(
        "totally-unknown-model-3", fallback_pricing={}, default_pricing=(1.0, 4.0, 0.1, "default-unpriced"))
    assert (inp, out, cache_read, src) == (1.0, 4.0, 0.1, "default-unpriced")


def test_cache_ratios_are_learned_from_page_prose():
    text = ("| Claude Opus 5.5 | $4 / MTok | $5 / MTok | $8 / MTok | $0.20 / MTok | $20 / MTok |\n"
            "Cache read: 0.1x base input price (0.025x on Claude Fable 5.1 and Claude Mythos 5.1; "
            "0.05x on Claude Opus 5.5)")
    learned = pricing_live.learn_claude_cache_ratios(text)
    assert learned == {"fable-5.1": 0.025, "mythos-5.1": 0.025, "opus-5.5": 0.05}
    assert pricing_live.expected_claude_cache_ratio("claude-opus-5-5") == 0.05
    assert pricing_live.expected_claude_cache_ratio("claude-opus-5") == 0.10


def test_synthetic_and_thinking_variants_resolve():
    assert pricing_live.resolve_price("<synthetic>")[:3] == (0.0, 0.0, 0.0)
    base = pricing_live.resolve_price("claude-opus-4-6", fallback_pricing={"claude-opus-4-6": (5.0, 25.0, 0.5)})
    think = pricing_live.resolve_price("claude-opus-4-6-thinking", fallback_pricing={"claude-opus-4-6": (5.0, 25.0, 0.5)})
    assert think[:3] == base[:3]


# --- _lookup_price_schedule boundary (PR #50 follow-up, Jensen HOLE-FOUND) ---
# The resolver trusted str(when)[:10] + a lexicographic band compare, so any
# garbage whose str() starts past "2" landed in the newest open-ended band.
# _validated_day is now the single boundary: only a real date/datetime or a
# strict YYYY-MM-DD string reaches the compare. These pin the boundary
# itself, below the price_for wrapper. Schedule under test:
# gemini-4-carbon: 2026-01-01..2026-11-30 -> (2.00, 10.00, 0.10, doc);
# 2026-12-01.. (open-ended) -> (4.00, 20.00, 0.20, doc).

_SCHED = {
    "gemini-4-carbon": [
        ("2026-01-01", "2026-11-30", (2.00, 10.00, 0.100, pricing_live.DOC)),
        ("2026-12-01", None, (4.00, 20.00, 0.200, pricing_live.DOC)),
    ],
}


def test_lookup_rejects_garbage_and_non_date_when():
    for bad in ("not-a-date", "", "2026-13-40", "2027-02-30", "2026-12-01 ",
                "2026-12-01T00:00:00", "20261201", 12345.6, 1790000000,
                ["2026-11-30"], {"date": "2026-11-30"}, {"2026-11-30"},
                b"2026-11-30", ("2026-11-30",), True, None, object()):
        assert pricing_live._lookup_price_schedule(_SCHED, "gemini-4-carbon", bad) is None


def test_lookup_rejects_far_future_dates():
    # Open-ended band covers real future dates, not typo years: a day more
    # than 366 days past today is not a real usage day.
    assert pricing_live._lookup_price_schedule(_SCHED, "gemini-4-carbon", "2099-12-31") is None
    assert pricing_live._lookup_price_schedule(
        _SCHED, "gemini-4-carbon",
        datetime.datetime(2099, 12, 31, tzinfo=datetime.timezone.utc)) is None


def test_lookup_accepts_real_days_including_near_future():
    assert pricing_live._lookup_price_schedule(
        _SCHED, "gemini-4-carbon", "2026-11-30")[:3] == (2.0, 10.0, 0.1)
    assert pricing_live._lookup_price_schedule(
        _SCHED, "gemini-4-carbon", "2026-12-01")[:3] == (4.0, 20.0, 0.2)
    # A published transition a few weeks out is real, not far-future.
    assert pricing_live._lookup_price_schedule(
        _SCHED, "gemini-4-carbon", "2027-01-01")[:3] == (4.0, 20.0, 0.2)


def test_lookup_normalizes_tz_aware_datetimes_to_the_utc_day():
    est = datetime.timezone(datetime.timedelta(hours=-5))
    plus14 = datetime.timezone(datetime.timedelta(hours=14))
    # 23:30 EST 2026-11-30 == 04:30 UTC 2026-12-01: new era, both spellings.
    assert pricing_live._lookup_price_schedule(
        _SCHED, "gemini-4-carbon",
        datetime.datetime(2026, 11, 30, 23, 30, tzinfo=est))[:3] == (4.0, 20.0, 0.2)
    assert pricing_live._lookup_price_schedule(
        _SCHED, "gemini-4-carbon",
        datetime.datetime(2026, 12, 1, 4, 30, tzinfo=datetime.timezone.utc))[:3] == (4.0, 20.0, 0.2)
    # Mirror: 00:30 +14 on 2026-12-01 is 10:30 UTC on 2026-11-30: old era.
    assert pricing_live._lookup_price_schedule(
        _SCHED, "gemini-4-carbon",
        datetime.datetime(2026, 12, 1, 0, 30, tzinfo=plus14))[:3] == (2.0, 10.0, 0.1)
    # Naive datetimes and date objects keep their own wall-clock day.
    assert pricing_live._lookup_price_schedule(
        _SCHED, "gemini-4-carbon",
        datetime.datetime(2026, 11, 30, 23, 30))[:3] == (2.0, 10.0, 0.1)
    assert pricing_live._lookup_price_schedule(
        _SCHED, "gemini-4-carbon", datetime.date(2026, 12, 1))[:3] == (4.0, 20.0, 0.2)


def test_lookup_rejects_a_hostile_str_object():
    class Hostile:
        def __str__(self):
            return "2026-12-01"
    assert pricing_live._lookup_price_schedule(_SCHED, "gemini-4-carbon", Hostile()) is None


def test_historical_cache_lookup_rejects_garbage_when(clean_resolver_state, tmp_path):
    # Same boundary hole lived in resolve_historical_cache_price: a garbage
    # day compared lexicographically against snapshot dates and could match.
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({
        "fetched_at": "2026-11-01T00:00:00+00:00",
        "models": {"gemini-4-carbon": [2.0, 10.0, 0.1]},
        "history": [{"fetched_at": "2026-11-01T00:00:00+00:00",
                     "models": {"gemini-4-carbon": [2.0, 10.0, 0.1]}}],
    }), encoding="utf-8")
    assert pricing_live.resolve_historical_cache_price(
        "gemini-4-carbon", "not-a-date", cache_path=cache) is None
    assert pricing_live.resolve_historical_cache_price(
        "gemini-4-carbon", "2026-11-30", cache_path=cache) is not None
