"""Tests for hardened Gemini Markdoc & HTML pricing parser and temporal schedules."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

import pricing_live
import token_tracker

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_pricing_state(monkeypatch, tmp_path):
    pricing_live.reset_process_state()
    cache_file = tmp_path / "pricing_cache.json"
    monkeypatch.setenv("NOUGEN_PRICING_CACHE_PATH", str(cache_file))
    monkeypatch.setenv("NOUGEN_PRICING_TTL_HOURS", "24")
    yield cache_file
    pricing_live.reset_process_state()


def test_real_response_gemini_markdoc_fixture():
    md_path = FIXTURES_DIR / "gemini_pricing.md"
    assert md_path.is_file()
    text = md_path.read_text(encoding="utf-8")

    # Pre-transition date (2026-11-11)
    parsed_nov = pricing_live.parse_gemini_pricing(text, as_of="2026-11-11")
    assert parsed_nov["gemini-4-carbon"] == (2.0, 10.0, 0.1)
    assert parsed_nov["gemini-4-argon"] == (2.0, 10.0, 0.1)
    assert parsed_nov["gemini-3.8-flash"] == (0.75, 3.75, 0.075)
    assert parsed_nov["gemini-3.6-flash"] == (0.75, 3.75, 0.075)
    assert parsed_nov["gemini-3.1-flash-lite"] == (0.25, 1.5, 0.025)
    assert parsed_nov["gemini-3.1-pro-preview"] == (2.0, 12.0, 0.2)
    assert parsed_nov["gemini-3.1-pro-preview-customtools"] == (2.0, 12.0, 0.2)
    assert parsed_nov["gemini-2.5-pro"] == (1.25, 10.0, 0.125)

    # Non-text / TTS / STT / image / embedding models must NOT leak into text billing
    for excluded in (
        "gemini-3.8-flash-tts",
        "gemini-3.8-flash-lite-tts",
        "gemini-3.1-flash-tts-preview",
        "gemini-2.5-flash-preview-tts",
        "gemini-2.5-pro-preview-tts",
        "gemini-3.5-transcribe",
        "gemini-3.5-transcribe-live",
        "gemini-2.5-flash-image",
        "gemini-embedding-2",
    ):
        assert excluded not in parsed_nov, f"{excluded} must be excluded from text billing"

    # Post Dec 1, 2026 transition
    parsed_dec = pricing_live.parse_gemini_pricing(text, as_of="2026-12-01")
    assert parsed_dec["gemini-4-carbon"] == (4.0, 20.0, 0.2)
    assert parsed_dec["gemini-4-argon"] == (4.0, 20.0, 0.2)
    assert parsed_dec["gemini-3.8-flash"] == (0.75, 3.75, 0.075)

    # Post Jan 1, 2027 transition
    parsed_jan = pricing_live.parse_gemini_pricing(text, as_of="2027-01-01")
    assert parsed_jan["gemini-3.8-flash"] == (1.5, 7.5, 0.15)
    assert parsed_jan["gemini-3.6-flash"] == (1.5, 7.5, 0.15)


def test_four_column_markdoc_uses_paid_header_not_last_column():
    """P1 fix: trailing Enterprise column must not hijack Paid Tier pricing."""
    doc = """
## Gemini 4 Test  {% id="gemini-4-test" %}
*[`gemini-4-test`](/docs/models/gemini-4-test)*

{% tabs %}
  {% tab title="Standard" %}
  {% table %}
  * Feature
  * Free Tier
  * Paid Tier, per 1M tokens in USD
  * Enterprise Tier

  ---

  * Input price
  * Not available
  * $2.50
  * $99.00

  ---

  * Output price (including thinking tokens)
  * Not available
  * $12.50
  * $499.00

  ---

  * Context caching price
  * Not available
  * $0.25
  * $9.90
  {% /table %}
  {% /tab %}
{% /tabs %}
"""
    parsed = pricing_live.parse_gemini_markdown_pricing(doc)
    assert parsed["gemini-4-test"] == (2.50, 12.50, 0.25)


def test_reordered_columns_fail_safe_and_ambiguous_shape_fails_closed():
    """Column resolution works by header name, and ambiguous/missing shapes fail closed."""
    reordered = """
## Gemini Reorder
*[`gemini-4-reorder`](/docs/models/gemini-4-reorder)*

{% table %}
  * Feature
  * Paid Tier, per 1M tokens in USD
  * Free Tier

  ---

  * Input price
  * $1.00
  * Free

  ---

  * Output price
  * $5.00
  * Free

  ---

  * Context caching price
  * $0.10
  * Free
{% /table %}
"""
    assert pricing_live.parse_gemini_markdown_pricing(reordered)["gemini-4-reorder"] == (1.0, 5.0, 0.1)

    no_paid_header = reordered.replace("Paid Tier, per 1M tokens in USD", "Commercial Tier, per 1M tokens in USD")
    assert pricing_live.parse_gemini_markdown_pricing(no_paid_header) == {}

    two_paid_headers = reordered.replace("Free Tier", "Paid Tier Batch, per 1M tokens in USD")
    assert pricing_live.parse_gemini_markdown_pricing(two_paid_headers) == {}

    row_mismatch = reordered.replace("* Free\n", "")
    assert pricing_live.parse_gemini_markdown_pricing(row_mismatch) == {}


def test_grounding_per_1000_requests_does_not_poison_token_units():
    """P2 fix: grounding per-1000-request rows never hijack headerless token pricing or corrupt 1M headers."""
    doc_with_header_unit = """
## Gemini Grounded
*[`gemini-4-grounded`](/docs/models/gemini-4-grounded)*

{% table %}
  * Feature
  * Free Tier
  * Paid Tier, per 1M tokens in USD

  ---

  * Input price
  * Free
  * $1.50

  ---

  * Output price
  * Free
  * $9.00

  ---

  * Context caching price
  * Free
  * $0.15

  ---

  * Grounding with Google Search
  * Not available
  * $14 / 1k search queries
{% /table %}
"""
    assert pricing_live.parse_gemini_markdown_pricing(doc_with_header_unit)["gemini-4-grounded"] == (1.50, 9.00, 0.15)

    # Header lacks token unit + later row has "per 1,000 requests" + "tokens" mentioned elsewhere -> MUST fail closed
    headerless = doc_with_header_unit.replace("Paid Tier, per 1M tokens in USD", "Paid Tier")
    assert pricing_live.parse_gemini_markdown_pricing(headerless) == {}


def test_text_output_plus_audio_input_model_is_excluded():
    """P2 fix: STT/audio-input models with text output must NOT be mislabeled as all-text."""
    stt_doc = """
## Gemini 4 Transcribe
*[`gemini-4-transcribe`](/docs/models/gemini-4-transcribe)*

{% tabs %}
  {% tab title="Standard" %}
  {% table %}
  * Feature
  * Free Tier
  * Paid Tier, per 1M tokens in USD

  ---

  * Input price
  * Free
  * $3.50 or $0.005/min (audio)

  ---

  * Output price (including thinking tokens)
  * Free
  * $21.00 or $0.004/min (text)

  ---

  * Context caching price
  * Free
  * $0.35 (text)
  {% /table %}
  {% /tab %}
{% /tabs %}
"""
    assert pricing_live.parse_gemini_markdown_pricing(stt_doc) == {}


def test_tts_audio_output_is_excluded():
    """TTS models with text input and audio-only output must be excluded."""
    tts_doc = """
## Gemini 4 TTS
*[`gemini-4-tts`](/docs/models/gemini-4-tts)*

{% table %}
  * Feature
  * Free Tier
  * Paid Tier, per 1M tokens in USD

  ---

  * Input price
  * Free
  * $0.50 (text)

  ---

  * Output price
  * Free
  * $9.00 (audio)

  ---

  * Context caching price
  * Free
  * $0.05 (text)
{% /table %}
"""
    assert pricing_live.parse_gemini_markdown_pricing(tts_doc) == {}


def test_missing_standard_tab_and_malformed_slug_fail_closed():
    """{% tabs %} missing Standard tab or blank slug fails closed instead of parsing Batch/guesswork."""
    batch_only = """
## Gemini 4 BatchOnly
*[`gemini-4-batchonly`](/docs/models/gemini-4-batchonly)*

{% tabs %}
  {% tab title="Batch" %}
  {% table %}
  * Feature
  * Free Tier
  * Paid Tier, per 1M tokens in USD

  ---

  * Input price
  * Free
  * $1.00

  ---

  * Output price
  * Free
  * $5.00

  ---

  * Context caching price
  * Free
  * $0.10
  {% /table %}
  {% /tab %}
{% /tabs %}
"""
    assert pricing_live.parse_gemini_markdown_pricing(batch_only) == {}

    malformed_slug = batch_only.replace("`gemini-4-batchonly`", "``").replace('title="Batch"', 'title="Standard"')
    assert pricing_live.parse_gemini_markdown_pricing(malformed_slug) == {}


def test_live_cache_does_not_suppress_published_schedule_transitions(monkeypatch, isolated_pricing_state):
    """Cached flat rates captured before a transition date must never override scheduled Dec/Jan transitions."""
    md_text = (FIXTURES_DIR / "gemini_pricing.md").read_text(encoding="utf-8")

    # Serve only Gemini fixture via mocked urlopen
    class _FakeResp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "ai.google.dev" in url:
            return _FakeResp(md_text.encode("utf-8"))
        raise pricing_live.urllib.error.URLError("offline other vendors")

    monkeypatch.setattr(pricing_live.urllib.request, "urlopen", fake_urlopen)

    # First resolution triggers live fetch and writes cache including extracted schedules
    res_today = pricing_live.resolve_price("gemini-4-carbon", when="2026-11-15")
    assert res_today[:3] == (2.0, 10.0, 0.1)

    # Verify cache persisted schedule bands
    cache_data = json.loads(isolated_pricing_state.read_text(encoding="utf-8"))
    assert "gemini-4-carbon" in cache_data.get("schedules", {})

    # Now query post-transition date directly against the cache written on 2026-11-11:
    # must NOT return the cached $2/$10/$0.1 snapshot for 2026-12-05!
    res_dec = pricing_live.resolve_price("gemini-4-carbon", when="2026-12-05")
    assert res_dec[:3] == (4.0, 20.0, 0.2)

    # And via token_tracker.price_for end-to-end
    assert token_tracker.price_for("gemini-4-carbon", "2026-11-30")[:3] == (2.0, 10.0, 0.1)
    assert token_tracker.price_for("gemini-4-carbon", "2026-12-01")[:3] == (4.0, 20.0, 0.2)
    assert token_tracker.price_for("gemini-4-argon", "2026-11-30")[:3] == (2.0, 10.0, 0.1)
    assert token_tracker.price_for("gemini-4-argon", "2026-12-01")[:3] == (4.0, 20.0, 0.2)


def test_fetch_vendor_pricing_handles_both_markdoc_and_html(monkeypatch):
    """Verify fetch_vendor_pricing dispatches dynamically based on content shape returned."""
    md_text = (FIXTURES_DIR / "gemini_pricing.md").read_text(encoding="utf-8")
    html_text = (FIXTURES_DIR / "gemini_pricing.html").read_text(encoding="utf-8")

    class _FakeResp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    # 1. Server returns Markdoc
    pricing_live.reset_process_state()
    monkeypatch.setattr(pricing_live.urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(md_text.encode("utf-8")))
    parsed_md, _ = pricing_live.fetch_vendor_pricing("gemini")
    assert "gemini-4-carbon" in parsed_md

    # 2. Server returns HTML
    pricing_live.reset_process_state()
    monkeypatch.setattr(pricing_live.urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(html_text.encode("utf-8")))
    parsed_html, _ = pricing_live.fetch_vendor_pricing("gemini")
    assert parsed_html["gemini-3.5-flash"] == (1.50, 9.00, 0.15)
