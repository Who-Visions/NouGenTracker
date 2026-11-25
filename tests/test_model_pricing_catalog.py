"""Offline catalog coverage and official pricing regressions, 2026-09-29."""
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

import pricing_catalog as catalog
import pricing_live
import token_tracker as tracker

FIXTURES = Path(__file__).with_name("fixtures")


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    pricing_live.reset_process_state()
    monkeypatch.setenv("NOUGEN_PRICING_CACHE_PATH", str(tmp_path / "cache.json"))
    pricing_live._ATTEMPTED_VENDORS.update(v for v, _ in pricing_live.VENDOR_SOURCES)
    yield
    pricing_live.reset_process_state()


def test_every_openai_catalog_id_is_accounted_for():
    expected = json.loads((FIXTURES / "openai_catalog_ids.json").read_text(encoding="utf-8"))
    assert len(expected) == 101
    assert all(catalog.lookup_model(model) is not None for model in expected)


def test_claude_standard_table_has_exact_catalog_rows():
    parsed = pricing_live.parse_anthropic_pricing((FIXTURES / "claude_standard_catalog.md").read_text(encoding="utf-8"))
    assert len(parsed) == 32  # 18 canonical models, plus dotted spelling aliases
    for model, rates in parsed.items():
        record = catalog.lookup_model(model)
        assert record is not None
        text = record["rates"]["text"]
        assert (text["input"], text["output"], text["cache_read"]) == rates


@pytest.mark.parametrize("model,rates", sorted(catalog.text_prices().items()))
def test_text_shadow_bill_uses_catalog_fallback(model, rates):
    assert tracker.price_for(model)[:3] == rates[:3]


def test_no_nan_negative_or_missing_provenance_in_catalog():
    data = catalog.load_catalog()
    # 185 models / 89 aliases: +2 Muse Spark records (Standard, Contributor,
    # verified 2026-11-25) and +5 Muse Spark alias spellings (slash + hyphenated).
    assert len(data["models"]) == 185
    assert len(data["aliases"]) == 89
    for record in data["models"].values():
        assert record["source_url"].startswith("https://")
        # The 2026-09-29 snapshot; Muse Spark rows were verified later, 2026-11-25.
        assert record["verified_on"] in ("2026-09-29", "2026-11-25")
        for rates in record["rates"].values():
            assert all(math.isfinite(rate) and rate >= 0 for rate in rates.values())


def test_snapshot_inference_keeps_estimate_provenance():
    assert catalog.lookup_model("gpt-4o-2024-08-06")["price_source"] == "est"
    assert tracker.price_for("gpt-4o-2024-08-06")[3] == pricing_live.EST
    # The explicit older snapshot has its own published rate, not today's rate.
    assert tracker.price_for("gpt-4o-2024-05-13")[:3] == (5.0, 15.0, 0.0)


def test_canceled_sonnet_increase_ignores_stale_cache():
    pricing_live._MEMORY_CACHE["claude-sonnet-5"] = (3.0, 15.0, 0.3)
    for day in ("2026-08-31", "2026-09-01", "2026-09-29"):
        assert tracker.price_for("claude-sonnet-5", day)[:3] == (2.0, 10.0, 0.2)


def test_google_published_future_transition_is_dated():
    for model in ("gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"):
        assert tracker.price_for(model, "2026-12-31")[:3] == (.75, 3.75, .075)
        assert tracker.price_for(model, "2027-01-01")[:3] == (1.5, 7.5, .15)
        assert catalog.lookup_model(model, "2026-12-31")["metered"]["cache_million_token_hours"] == .5
        assert catalog.lookup_model(model, "2027-01-01")["metered"]["cache_million_token_hours"] == 1.0


def test_rosalind_billing_start_is_not_backdated():
    assert tracker.price_for("gpt-rosalind-research", "2026-09-29")[:3] == (0.0, 0.0, 0.0)
    assert tracker.price_for("gpt-rosalind-research", "2026-10-05")[:3] == (5.0, 25.0, .5)


def test_embedding_moderation_and_duration_keep_their_units():
    assert tracker.price_for("text-embedding-3-small")[:3] == (.02, 0.0, 0.0)
    assert tracker.price_for("omni-moderation-latest")[:3] == (0.0, 0.0, 0.0)
    assert catalog.lookup_model("gpt-live-1")["metered"] == {"minutes": .05}
    assert catalog.lookup_model("tts-1")["metered"] == {"characters_per_million": 15.0}
    assert "gpt-live-1" not in catalog.text_prices()
    assert "gpt-image-2" not in catalog.text_prices()


def test_audio_image_and_text_prices_are_separate():
    r = catalog.lookup_model("gpt-realtime-2.1")
    assert r["rates"]["text"]["output"] == 24.0
    assert r["rates"]["audio"]["output"] == 64.0
    r = catalog.lookup_model("gemini-3.8-live")
    assert r["rates"]["text"] == {"input": .75, "output": 4.5}
    assert r["rates"]["audio"] == {"input": 3.0, "output": 12.0}
    assert catalog.lookup_model("gemini-3.1-flash-lite")["rates"]["audio"]["input"] == .5


def test_unpriced_legacy_is_explicit_and_not_fabricated():
    r = catalog.lookup_model("claude-2.1")
    assert r["billing_status"] == "unpriced"
    assert r["rates"] == {}
    assert catalog.lookup_model("imaginary-new-model") is None


def test_openai_markdown_standard_pro_cache_absence_and_tier_isolation():
    table = (FIXTURES / "openai_standard_catalog.md").read_text(encoding="utf-8")
    parsed = pricing_live.parse_openai_pricing(table)
    assert parsed["gpt-6.1-sol"] == (2.0, 10.0, .1)
    assert parsed["gpt-5-pro"] == (15.0, 120.0, 0.0)
    cheaper = table.replace("### Standard pricing data", "### Batch pricing data")
    assert pricing_live.parse_openai_pricing(cheaper) == {}
    finetuning = "Finetuning\n" + table
    assert pricing_live.parse_openai_pricing(finetuning) == {}


def test_google_parser_does_not_bill_audio_output_as_text():
    parsed = pricing_live.parse_gemini_pricing((FIXTURES / "gemini_standard_catalog.html").read_text(encoding="utf-8"))
    assert "gemini-3.8-flash-tts" not in parsed
    assert "gemini-2.5-flash-image" not in parsed
    assert parsed["gemini-3.8-flash"] == (.75, 3.75, .075)


def test_lookup_is_a_copy_and_has_no_mutating_side_effect():
    r = catalog.lookup_model("gpt-6.1-sol")
    r["rates"]["text"]["input"] = 99
    assert catalog.lookup_model("gpt-6.1-sol")["rates"]["text"]["input"] == 2


@pytest.mark.parametrize("day", ["garbage", "2027-1-1", "2027-02-30", "2027-01-01junk", "", 20270101])
def test_invalid_pricing_date_is_rejected(day):
    with pytest.raises(ValueError):
        catalog.lookup_model("gemini-3.8-flash", day)


def test_pricing_date_objects_select_the_same_boundary():
    import datetime
    for day in (datetime.date(2027, 1, 1), datetime.datetime(2027, 1, 1, 12)):
        assert catalog.lookup_model("gemini-3.8-flash", day)["rates"]["text"]["input"] == 1.5


@pytest.mark.parametrize("date_arg", ["2027-01-01", ""])
def test_cli_requires_a_model_even_for_an_empty_date(monkeypatch, capsys, date_arg):
    monkeypatch.setattr("sys.argv", ["pricing_catalog.py", "--date", date_arg])
    with pytest.raises(SystemExit) as exc:
        catalog.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "--date requires --model" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("model_arg", ["", "   ", "imaginary-new-model"])
def test_cli_invalid_model_does_not_dump_the_catalog(monkeypatch, capsys, model_arg):
    monkeypatch.setattr("sys.argv", ["pricing_catalog.py", "--model", model_arg])
    with pytest.raises(SystemExit) as exc:
        catalog.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "model is not in the verified catalog" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("date_arg", ["", "2027-02-30", "2027-01-01junk"])
def test_cli_bad_date_reports_an_error_without_a_traceback(monkeypatch, capsys, date_arg):
    monkeypatch.setattr("sys.argv", ["pricing_catalog.py", "--model", "gemini-3.8-flash", "--date", date_arg])
    with pytest.raises(SystemExit) as exc:
        catalog.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("day,input_rate,storage_rate", [
    ("2026-12-31", .75, .5),
    ("2027-01-01", 1.5, 1.0),
])
def test_cli_valid_date_emits_only_the_requested_model(monkeypatch, capsys, day, input_rate, storage_rate):
    monkeypatch.setattr("sys.argv", ["pricing_catalog.py", "--model", "gemini-3.8-flash", "--date", day])
    catalog.main()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert captured.err == ""
    assert result["model"] == "gemini-3.8-flash"
    assert result["rates"]["text"]["input"] == input_rate
    assert result["metered"]["cache_million_token_hours"] == storage_rate
    assert "models" not in result


@pytest.mark.parametrize("date_arg,expected_status", [("2027-01-01", 0), ("2027-02-30", 2)])
def test_standalone_cli_from_another_directory(tmp_path, date_arg, expected_status):
    result = subprocess.run(
        [sys.executable, str(Path(catalog.__file__).resolve()),
         "--model", "gemini-3.8-flash", "--date", date_arg],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == expected_status
    if expected_status == 0:
        record = json.loads(result.stdout)
        assert record["rates"]["text"]["input"] == 1.5
        assert record["metered"]["cache_million_token_hours"] == 1.0
        assert result.stderr == ""
    else:
        assert result.stdout == ""
        assert "error:" in result.stderr
        assert "Traceback" not in result.stderr
    assert not list(tmp_path.iterdir())  # The CLI must not write in the caller's directory.
