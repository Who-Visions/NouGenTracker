"""Pricing-table tests.

A missing model does not fail loudly — it silently bills at DEFAULT_PRICING,
which is how 246M claude-opus-5 tokens came to be costed at a fifth of their
real price. These assert the table covers what the fleet actually runs.
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
pc = _load("pricing_catalog", "pricing_catalog.py")

CLAUDE_5_FAMILY = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5")


def test_five_family_models_are_priced():
    for model in CLAUDE_5_FAMILY:
        assert model in tt.MODEL_PRICING, f"{model} missing — would bill at the default"


def test_opus_5_is_not_billed_at_the_fallback_rate():
    assert tt.price_for("claude-opus-5")[:3] == (5.00, 25.00, 0.500)
    assert tt.price_for("claude-opus-5")[:3] != tt.DEFAULT_PRICING[:3]


def test_sonnet_5_is_priced_at_list():
    assert tt.price_for("claude-sonnet-5")[:3] == (2.00, 10.00, 0.200)


def test_cache_read_is_a_tenth_of_input_across_claude_models():
    """The documented ratio. A typo here is a silent mis-bill, not a failure."""
    for model in CLAUDE_5_FAMILY:
        inp, _out, cache, _src = tt.MODEL_PRICING[model]
        assert abs(cache - inp / 10) < 1e-9, f"{model}: cache {cache} != {inp}/10"


def test_the_estimated_suffix_still_resolves():
    """Antigravity appends ' (estimated)'; it must not defeat the lookup."""
    assert tt.price_for("claude-opus-5 (estimated)") == tt.price_for("claude-opus-5")


def test_unknown_models_still_fall_back_rather_than_reading_zero():
    assert tt.price_for("some-model-nobody-has-heard-of") == tt.DEFAULT_PRICING
    assert tt.DEFAULT_PRICING[0] > 0, "a $0 default would hide spend entirely"


# --- audited against the official pricing pages, 2026-08-01 ---------------
#
# A missing row is not neutral — it falls through to DEFAULT_PRICING at $1/$4,
# so a premium model reads an order of magnitude cheaper than it is. These are
# transcribed from Anthropic, Google and OpenAI's own pricing pages.

OFFICIAL = {
    "claude-fable-5": (10, 50, 1.0), "claude-mythos-5": (10, 50, 1.0),
    "claude-opus-5": (5, 25, 0.5), "claude-opus-4-8": (5, 25, 0.5),
    "claude-opus-4-7": (5, 25, 0.5), "claude-opus-4-6": (5, 25, 0.5),
    "claude-opus-4-5": (5, 25, 0.5), "claude-opus-4-1": (15, 75, 1.5),
    "claude-sonnet-4-6": (3, 15, 0.3), "claude-sonnet-4-5": (3, 15, 0.3),
    "claude-haiku-4-5": (1, 5, 0.1), "claude-haiku-3-5": (0.8, 4, 0.08),
    "gemini-3.6-flash": (0.75, 3.75, 0.075), "gemini-3.5-flash": (1.5, 9.0, 0.15),
    "gemini-3.5-flash-lite": (0.3, 2.5, 0.03), "gemini-3.1-flash-lite": (0.25, 1.5, 0.025),
    "gemini-3.1-pro-preview": (2.0, 12.0, 0.2), "gemini-3-flash-preview": (0.5, 3.0, 0.05),
    "gemini-2.5-pro": (1.25, 10.0, 0.125), "gemini-2.5-flash": (0.3, 2.5, 0.03),
    "gemini-2.5-flash-lite": (0.1, 0.4, 0.01),
    "gpt-5.6-sol": (4, 20, 0.4), "gpt-5.6-terra": (2, 12, 0.2), "gpt-5.6-luna": (0.2, 1.2, 0.02),
    "gpt-5.5": (5, 30, 0.5), "gpt-5.5-pro": (30, 180, 0.0), "gpt-5.4": (2.5, 15, 0.25),
    "gpt-5.4-mini": (0.75, 4.5, 0.075), "gpt-5.4-nano": (0.2, 1.25, 0.02),
    "gpt-5.4-pro": (30, 180, 0.0), "gpt-5.3-codex": (1.75, 14.0, 0.175),
    "chat-latest": (5, 30, 0.5),
}


@pytest.mark.parametrize("model,expected", sorted(OFFICIAL.items()))
def test_row_matches_the_official_page(model, expected):
    assert model in tt.MODEL_PRICING, f"{model} absent — would bill at $1/$4"
    assert tuple(round(x, 4) for x in tt.MODEL_PRICING[model][:3]) == \
        tuple(float(x) for x in expected)


def test_the_premium_models_are_not_billing_at_the_default():
    """gpt-5.5-pro and gpt-5.4-pro are $30/$180. Absent, they read as $1/$4 —
    a 30x undercount, and silent."""
    for model in ("gpt-5.5-pro", "gpt-5.4-pro", "claude-opus-4-1"):
        assert tt.price_for(model)[:2] != tt.DEFAULT_PRICING[:2]


# --- dated pricing --------------------------------------------------------

def test_sonnet_5_bills_intro_rates_during_the_intro_window():
    for day in ("2026-01-01", "2026-08-01", "2026-08-31"):
        assert tt.price_for("claude-sonnet-5", day)[:3] == (2.00, 10.00, 0.200), day


def test_sonnet_5_canceled_increase_does_not_change_rates():
    assert tt.price_for("claude-sonnet-5", "2026-09-01")[:3] == (2.00, 10.00, 0.200)


def test_an_undated_call_falls_back_to_the_flat_table():
    """Callers that do not care about history keep the behaviour they had."""
    assert tt.price_for("claude-sonnet-5")[:3] == (2.00, 10.00, 0.200)


def test_a_date_object_works_as_well_as_a_string():
    import datetime
    assert tt.price_for("claude-sonnet-5", datetime.date(2026, 8, 15))[:3] == (2.00, 10.00, 0.200)


def test_dating_does_not_disturb_models_that_were_never_repriced():
    for day in ("2026-01-01", "2026-12-31"):
        assert tt.price_for("claude-opus-5", day)[:3] == (5.00, 25.00, 0.500)


def test_model_bill_threads_the_date_through():
    bucket = {"input_tokens": 1_000_000}
    intro, _ = tt.model_bill("claude-sonnet-5", bucket, "2026-08-01")
    later, _ = tt.model_bill("claude-sonnet-5", bucket, "2026-09-01")
    assert intro == pytest.approx(2.00) and later == pytest.approx(2.00)


# --- family inference for unlisted variants ------------------------------

def test_effort_variant_prices_from_its_family_not_the_default():
    """`gemini-3.6-flash-high` is 3.6-flash at a different effort. Landing it
    on DEFAULT_PRICING under-bills the newest model, because the default is
    lower than every current Gemini rate."""
    base = tt.price_for("gemini-3.6-flash")
    variant = tt.price_for("gemini-3.6-flash-high")
    assert variant[:3] == base[:3]
    assert variant != tt.DEFAULT_PRICING


def test_an_inferred_rate_never_claims_to_be_documented():
    assert tt.price_for("gemini-3.6-flash-high")[3] == tt.EST
    assert tt.price_for("gemini-3.6-flash")[3] == tt.DOC


def test_family_inference_does_not_invent_prices():
    """Only known variant suffixes are stripped, so an unrelated name still
    falls to the default instead of borrowing someone else's rate."""
    assert tt.price_for("totally-made-up-model") == tt.DEFAULT_PRICING
    assert tt.price_for("auto-gemini-3") == tt.DEFAULT_PRICING


def test_exact_entries_are_untouched_by_the_fallback():
    for model in ("gemini-3.5-flash-high", "gemini-3.1-pro-low", "claude-opus-5"):
        assert tt.price_for(model)[3] == tt.DOC


# --- Meta (Muse Spark) ----------------------------------------------------
#
# Verified 2026-11-25 against Meta's own pricing page
# (dev.meta.ai/docs/pricing-rate-limits.md): Standard $1.25/$4.25/$0.15,
# Contributor $0.10/$0.20/$0.002 per MTok. Without these rows Muse billed at
# the $1/$4 unknown-model default — the same silent undercount that hit
# claude-opus-5 (255M tokens) before its row existed.

MUSE_MODELS = (
    "muse-spark-1.3", "muse-spark", "meta/muse-spark", "meta-muse-spark",
    "muse-spark-1.3-contributor", "meta/muse-spark-contributor",
    "meta-muse-spark-contributor",
)


def test_muse_spark_models_are_priced():
    for model in MUSE_MODELS:
        assert model in tt.MODEL_PRICING, f"{model} missing — would bill at the default"


def test_muse_spark_standard_is_priced_at_list():
    assert tt.price_for("muse-spark-1.3")[:3] == (1.25, 4.25, 0.15)
    assert tt.price_for("muse-spark")[:3] == (1.25, 4.25, 0.15)
    assert tt.price_for("meta/muse-spark")[:3] == (1.25, 4.25, 0.15)
    assert tt.price_for("meta-muse-spark")[:3] == (1.25, 4.25, 0.15)
    assert tt.price_for("muse-spark-1.3")[:3] != tt.DEFAULT_PRICING[:3]


def test_muse_spark_contributor_is_priced_at_list():
    assert tt.price_for("muse-spark-1.3-contributor")[:3] == (0.10, 0.20, 0.002)
    assert tt.price_for("meta/muse-spark-contributor")[:3] == (0.10, 0.20, 0.002)
    assert tt.price_for("meta-muse-spark-contributor")[:3] == (0.10, 0.20, 0.002)


def test_muse_spark_estimated_suffix_still_resolves():
    assert tt.price_for("muse-spark-1.3 (estimated)") == tt.price_for("muse-spark-1.3")


def test_muse_spark_catalog_lookup_returns_the_standard_record():
    rec = pc.lookup_model("muse-spark")
    assert rec is not None
    assert rec["rates"]["text"] == {"cache_read": 0.15, "input": 1.25, "output": 4.25}
    assert rec["vendor"] == "meta"
    assert rec["billing_status"] == "priced"
    assert pc.lookup_model("meta/muse-spark-contributor")["rates"]["text"] == {
        "cache_read": 0.002, "input": 0.10, "output": 0.20,
    }
