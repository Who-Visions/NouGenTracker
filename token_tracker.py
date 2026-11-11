#!/usr/bin/env python3
"""Claude Code & Antigravity token tracker.

Aggregates token usage from:
1. Claude Code JSONL logs under ~/.claude/projects/
2. Antigravity active sessions queried via internal loopback RPC
3. Antigravity JSONL logs under ~/.gemini/antigravity/brain/ and ~/.gemini/antigravity-cli/brain/ (fallback/archived)
"""
import glob
import json
import os
import sys
import subprocess
import re
import urllib.request
import ssl
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import pricing_live
import pricing_catalog
from pricing_live import calculate_cost, round_to_cents


def _force_utf8_stdio():
    """Windows consoles default to cp1252, which cannot encode this report's glyphs.

    The warning markers are the whole point of the lines they sit on — an
    overlap or a mixed-counting block is the loudest thing this tool prints —
    and on a cp1252 console `print("⚠ ...")` does not degrade, it raises
    UnicodeEncodeError and kills the run mid-report. Reconfigure rather than
    strip: `errors="replace"` keeps a legacy console readable.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") == "utf8":
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdio()

DAYS = 2
MONTH = None
BY_PROVIDER = False
RANGE_START = None
RANGE_END = None
# Did the caller actually name a window? DAYS always holds a value (its default,
# and --export/--lanes widen it), so DAYS alone cannot answer that. --fleet needs
# the distinction: unwindowed means all published history, not "the default 30".
DAYS_EXPLICIT = False
COMPARE_N = None
LANES = False
EXPORT = False
PUBLISH = False
VALIDATE = False
FIX = False
FLEET = False
INSTALL_HOOKS = False

# argparse CLI: preserves the legacy positional days arg and --month / --by-provider,
# and adds dynamic ranges (--days/--weeks/--start/--end) + a --compare period diff.
# Skipped for the --demo-tc subcommand, and when IMPORTED (e.g. hi_token_tracker.py
# imports this module for its helpers; we must not consume that process's argv or
# trigger argparse's --help/exit during import).
if "--demo-tc" not in sys.argv and __name__ == "__main__":
    import argparse as _ap

    _parser = _ap.ArgumentParser(
        prog="token_tracker.py",
        formatter_class=_ap.RawDescriptionHelpFormatter,
        description=(
            "Cross-provider token usage monitor. Reports input / output / cache-read / "
            "reasoning tokens by company (Claude Code, Google Antigravity, OpenAI Codex, "
            "Gemini CLI, + Fleet ledger) and by day, over any range you ask for."
        ),
        epilog=(
            "ranges (most specific wins: --start/--end > --month > --compare > --weeks > --days > positional)\n"
            "  token_tracker.py                                    last 2 days (default)\n"
            "  token_tracker.py 7                                  last 7 days (legacy positional)\n"
            "  token_tracker.py --days 7                           last 7 days\n"
            "  token_tracker.py --weeks 2                          last 14 days\n"
            "  token_tracker.py --month 2026-06                    a calendar month\n"
            "  token_tracker.py --start 2026-06-11 --end 2026-06-18   explicit inclusive range\n"
            "  token_tracker.py --compare 7                        last 7d vs prior 7d, per company\n"
            "  token_tracker.py 7 --by-provider                   group the Fleet ledger by provider\n"
            "\nenv: TOKEN_TRACKER_CUTOFF=<iso8601> overrides the lower bound."
        ),
    )
    _parser.add_argument("days_pos", nargs="?", type=int, default=None,
                         help="legacy positional: days back from now (default 2)")
    _parser.add_argument("--days", type=int, default=None, help="days back from now")
    _parser.add_argument("--weeks", type=int, default=None, help="weeks back from now (7*N days)")
    _parser.add_argument("--start", metavar="YYYY-MM-DD", default=None,
                         help="explicit window start, inclusive")
    _parser.add_argument("--end", metavar="YYYY-MM-DD", default=None,
                         help="explicit window end, inclusive (default = now)")
    _parser.add_argument("--month", metavar="YYYY-MM", default=None,
                         help="restrict the window to a calendar month")
    _parser.add_argument("--compare", type=int, metavar="N", default=None,
                         help="compare the last N days vs the prior N days, per company")
    _parser.add_argument("--lanes", action="store_true",
                         help="full analytics dashboard: 24h/WTD/MTD/QTD/HTD/YTD comparisons + records (highest day/week/month/streak)")
    _parser.add_argument("--by-provider", action="store_true",
                         help="group the Fleet usage ledger rows by provider")
    _parser.add_argument("--export", action="store_true",
                         help="write this machine's daily rollups to dailies/<machine>/")
    _parser.add_argument("--publish", action="store_true",
                         help="--export, then commit the dailies with Machine/Agent trailers")
    _parser.add_argument("--fleet", action="store_true",
                         help="totals across every machine that has published dailies")
    _parser.add_argument("--install-hooks", action="store_true",
                         help="point this clone at .githooks (per-clone; cannot be committed)")
    _parser.add_argument("--validate", "--backpedal", dest="validate", action="store_true",
                         help="backpedal: walk this machine's dailies newest-first and validate "
                              "(parse errors, negative totals, stale counting-code cohorts, "
                              "calendar gaps, dirty-tree stamp warning); exit 1 on defects")
    _parser.add_argument("--fix", action="store_true",
                         help="with --validate: archive this machine's stale-cohort days to "
                              "archive-<counter>/, re-export their window, re-validate. "
                              "Refuses to run from a dirty tree (the stamp would be dirty).")
    _a, _ = _parser.parse_known_args()

    MONTH = _a.month
    BY_PROVIDER = _a.by_provider
    RANGE_START = _a.start
    RANGE_END = _a.end
    COMPARE_N = _a.compare
    LANES = _a.lanes
    EXPORT = _a.export or _a.publish
    PUBLISH = _a.publish
    VALIDATE = _a.validate
    FIX = _a.fix
    FLEET = _a.fleet
    INSTALL_HOOKS = _a.install_hooks

    if _a.days is not None:
        DAYS = _a.days
    elif _a.weeks is not None:
        DAYS = _a.weeks * 7
    elif _a.days_pos is not None:
        DAYS = _a.days_pos

    DAYS_EXPLICIT = any(v is not None for v in
                        (_a.days, _a.weeks, _a.days_pos, _a.month,
                         _a.start, _a.end))

    # --compare widens the collection window so both periods are gathered (>= 2N days).
    if COMPARE_N is not None and COMPARE_N > 0:
        DAYS = max(DAYS, COMPARE_N * 2)
    if LANES:
        DAYS = max(DAYS, 760)   # load full available history for the analytics dashboard
    # An export publishes this machine's history, not just the window someone
    # happened to ask for on the command line.
    if EXPORT:
        DAYS = max(DAYS, 760)

PROJECTS = os.path.expanduser(os.path.join("~", ".claude", "projects"))

ANTIGRAVITY_BRAIN_DIRS = [
    os.path.expanduser(os.path.join("~", ".gemini", "antigravity", "brain")),
    os.path.expanduser(os.path.join("~", ".gemini", "antigravity-cli", "brain")),
    os.path.expanduser(os.path.join("~", ".gemini", "antigravity-ide", "brain")),
    os.path.expanduser(os.path.join("~", ".gemini", "antigravity-backup", "brain"))
]

CODEX_STATE = os.path.expanduser(os.path.join("~", ".codex", "state_5.sqlite"))

# Fleet usage ledger: forward token accounting for local Ollama/Gemma, OpenRouter,
# HF and other lanes that otherwise write no token telemetry to disk. Written by
# fleet_usage_proxy.py + the instrumented fleet clients (see Sol-Ai/).
FLEET_USAGE_LEDGER = os.environ.get(
    "FLEET_USAGE_LEDGER",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault", "fleet_usage.jsonl"),
)

KEYS = ("input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens", "reasoning_tokens")

NOW = datetime.now(timezone.utc).astimezone()

if RANGE_START or RANGE_END:
    local_tz = datetime.now().astimezone().tzinfo
    CUTOFF = (
        datetime.fromisoformat(RANGE_START).replace(tzinfo=local_tz)
        if RANGE_START else (NOW - timedelta(days=DAYS))
    )
    if RANGE_END:
        # end is inclusive -> extend to the end of that calendar day
        LIMIT_UPPER = datetime.fromisoformat(RANGE_END).replace(tzinfo=local_tz) + timedelta(days=1)
    else:
        try:
            LIMIT_UPPER = datetime.max.replace(tzinfo=timezone.utc).astimezone()
        except OSError:
            LIMIT_UPPER = datetime(3000, 1, 1, tzinfo=timezone.utc).astimezone()
elif MONTH:
    try:
        parts = MONTH.split("-")
        year = int(parts[0])
        month = int(parts[1])
        start_dt = datetime(year, month, 1)
        if month == 12:
            end_dt = datetime(year + 1, 1, 1)
        else:
            end_dt = datetime(year, month + 1, 1)
        # Convert to local timezone
        local_tz = datetime.now().astimezone().tzinfo
        CUTOFF = start_dt.replace(tzinfo=local_tz)
        LIMIT_UPPER = end_dt.replace(tzinfo=local_tz)
    except Exception as e:
        print(f"Error parsing --month {MONTH}: {e}")
        sys.exit(1)
else:
    cutoff_env = os.environ.get("TOKEN_TRACKER_CUTOFF")
    if cutoff_env:
        CUTOFF = datetime.fromisoformat(cutoff_env)
        # a bare ISO stamp ("2026-08-15T17:00") is naive; every parsed record
        # timestamp is tz-aware, so localize it to this machine's zone rather
        # than blowing up on the first comparison.
        if CUTOFF.tzinfo is None:
            CUTOFF = CUTOFF.astimezone()
    else:
        CUTOFF = NOW - timedelta(days=DAYS)
    try:
        LIMIT_UPPER = datetime.max.replace(tzinfo=timezone.utc).astimezone()
    except OSError:
        LIMIT_UPPER = datetime(3000, 1, 1, tzinfo=timezone.utc).astimezone()

ALL_INVOCATIONS = []

MODEL_MAP = {
    'MODEL_PLACEHOLDER_M132': 'gemini-3.5-flash-high',
    'MODEL_PLACEHOLDER_M131': 'gemini-3.5-flash-medium',
    'MODEL_PLACEHOLDER_M130': 'gemini-3.5-flash-low',
    'MODEL_PLACEHOLDER_M37': 'gemini-3.1-pro-high',
    'MODEL_PLACEHOLDER_M36': 'gemini-3.1-pro-low',
    'MODEL_PLACEHOLDER_M18': 'gemini-3-flash',
    'MODEL_PLACEHOLDER_M8': 'gemini-3-pro-high',
    'MODEL_PLACEHOLDER_M7': 'gemini-3-pro-low',
    'MODEL_PLACEHOLDER_M9': 'gemini-3-pro-image',
    'MODEL_PLACEHOLDER_M26': 'claude-opus-4-6-thinking',
    'MODEL_PLACEHOLDER_M35': 'claude-sonnet-4-6-thinking',
    'MODEL_PLACEHOLDER_M12': 'claude-opus-4-5-thinking',
    'MODEL_OPENAI_GPT_OSS_120B_MEDIUM': 'gpt-oss-120b-medium',
    'MODEL_CLAUDE_4_5_SONNET': 'claude-sonnet-4-5',
    'MODEL_CLAUDE_4_5_SONNET_THINKING': 'claude-sonnet-4-5-thinking',

    # New models from gemini_api_models.json
    'models/gemini-2.5-flash': 'gemini-2.5-flash',
    'models/gemini-2.5-pro': 'gemini-2.5-pro',
    'models/gemini-2.0-flash': 'gemini-2.0-flash',
    'models/gemini-2.0-flash-001': 'gemini-2.0-flash-001',
    'models/gemini-2.0-flash-lite-001': 'gemini-2.0-flash-lite-001',
    'models/gemini-2.0-flash-lite': 'gemini-2.0-flash-lite',
    'models/gemini-2.5-flash-preview-tts': 'gemini-2.5-flash-preview-tts',
    'models/gemini-2.5-pro-preview-tts': 'gemini-2.5-pro-preview-tts',
    'models/gemma-3-1b-it': 'gemma-3-1b-it',
    'models/gemma-3-4b-it': 'gemma-3-4b-it',
    'models/gemma-3-12b-it': 'gemma-3-12b-it',
    'models/gemma-3-27b-it': 'gemma-3-27b-it',
    'models/gemma-3n-e4b-it': 'gemma-3n-e4b-it',
    'models/gemma-3n-e2b-it': 'gemma-3n-e2b-it',
    'models/gemma-4-26b-a4b-it': 'gemma-4-26b-a4b-it',
    'models/gemma-4-31b-it': 'gemma-4-31b-it',
    'models/gemini-flash-latest': 'gemini-flash-latest',
    'models/gemini-flash-lite-latest': 'gemini-flash-lite-latest',
    'models/gemini-pro-latest': 'gemini-pro-latest',
    'models/gemini-2.5-flash-lite': 'gemini-2.5-flash-lite',
    'models/gemini-2.5-flash-image': 'nano-banana',
    'models/gemini-3-pro-preview': 'gemini-3-pro-preview',
    'models/gemini-3-flash-preview': 'gemini-3-flash-preview',
    'models/gemini-3.1-pro-preview': 'gemini-3.1-pro-preview',
    'models/gemini-3.1-pro-preview-customtools': 'gemini-3.1-pro-preview-customtools',
    'models/gemini-3.1-flash-lite-preview': 'gemini-3.1-flash-lite-preview',
    'models/gemini-3-pro-image-preview': 'nano-banana-pro',
    'models/nano-banana-pro-preview': 'nano-banana-pro',
    'models/gemini-3.1-flash-image-preview': 'nano-banana-2',
    'models/lyria-3-clip-preview': 'lyria-3-clip-preview',
    'models/lyria-3-pro-preview': 'lyria-3-pro-preview',
    'models/gemini-robotics-er-1.5-preview': 'gemini-robotics-er-1.5-preview',
    'models/gemini-2.5-computer-use-preview-10-2025': 'gemini-2.5-computer-use-preview-10-2025',
    'models/deep-research-pro-preview-12-2025': 'deep-research-pro-preview-12-2025',
    'models/gemini-embedding-001': 'gemini-embedding-001',
    'models/gemini-embedding-2-preview': 'gemini-embedding-2-preview',
    'models/aqa': 'aqa',
    'models/imagen-4.0-generate-001': 'imagen-4',
    'models/imagen-4.0-ultra-generate-001': 'imagen-4-ultra',
    'models/imagen-4.0-fast-generate-001': 'imagen-4-fast',
    'models/veo-2.0-generate-001': 'veo-2',
    'models/veo-3.0-generate-001': 'veo-3',
    'models/veo-3.0-fast-generate-001': 'veo-3-fast',
    'models/veo-3.1-generate-preview': 'veo-3.1',
    'models/veo-3.1-fast-generate-preview': 'veo-3.1-fast',
    'models/veo-3.1-lite-generate-preview': 'veo-3.1-lite',
    'models/gemini-2.5-flash-native-audio-latest': 'gemini-2.5-flash-native-audio-latest',
    'models/gemini-2.5-flash-native-audio-preview-09-2025': 'gemini-2.5-flash-native-audio-preview-09-2025',
    'models/gemini-2.5-flash-native-audio-preview-12-2025': 'gemini-2.5-flash-native-audio-preview-12-2025',
    'models/gemini-3.1-flash-live-preview': 'gemini-3.1-flash-live-preview',
}

def resolve_model(model_id):
    if not model_id:
        return "unknown"
    if model_id.startswith("models/"):
        model_id = model_id[7:]
    if model_id in MODEL_MAP:
        return MODEL_MAP[model_id]
    if model_id.startswith("MODEL_PLACEHOLDER_"):
        num = model_id.split("_")[-1]
        return f"gemini-{num.lower()}"
    return model_id


# --- Per-model pricing (USD per million tokens) -----------------------------
# Tuple = (input, output, cache_read, source). reasoning bills at the output
# rate; cache_creation bills at 1.25x input (the 5-minute cache-write tier).
#
# Claude rates are first-party list prices from claude.com/pricing.
# Everything tagged EST is an ESTIMATE with no first-party source wired in —
# treat those as tunable knobs, not ground truth, and correct them as real
# invoices arrive. The point of this table is an HONEST reference bill, not an
# impressive one: cache-reads are priced as cache-reads, not as fresh input.
DOC = "doc"   # first-party documented list price (any vendor)
EST = "est"
LIVE = pricing_live.LIVE
CACHED = pricing_live.CACHED
FALLBACK_CONST = pricing_live.FALLBACK_CONST
MODEL_PRICING = {
    # ---- Claude: first-party list prices ----
    "claude-fable-5":             (10.00, 50.00, 1.000, DOC),
    "claude-mythos-5":            (10.00, 50.00, 1.000, DOC),
    # Opus 4.1 is 3x the 4.5+ rate, not the same. Absent, it fell through to the
    # $1/$4 default — a 15x undercount on any log old enough to contain it.
    "claude-opus-4-1":            (15.00, 75.00, 1.500, DOC),
    "claude-haiku-3-5":           (0.80, 4.00, 0.080, DOC),
    # Opus 5 and Sonnet 5 were missing entirely, so 255M opus-5 tokens — the
    # second-largest model on this fleet — were billing at the $1/$4 unknown-model
    # default. Newer families have their own documented cache ratios.
    "claude-opus-5":              (5.00, 25.00, 0.500, DOC),
    "claude-opus-5-thinking":     (5.00, 25.00, 0.500, DOC),
    # The planned September increase was canceled by Anthropic.
    "claude-sonnet-5":            (2.00, 10.00, 0.200, DOC),
    "claude-sonnet-5-thinking":   (2.00, 10.00, 0.200, DOC),
    "claude-opus-4-8":            (5.00, 25.00, 0.500, DOC),
    "claude-opus-4-7":            (5.00, 25.00, 0.500, DOC),
    "claude-opus-4-6":            (5.00, 25.00, 0.500, DOC),
    "claude-opus-4-6-thinking":   (5.00, 25.00, 0.500, DOC),
    "claude-opus-4-5":            (5.00, 25.00, 0.500, DOC),
    "claude-opus-4-5-thinking":   (5.00, 25.00, 0.500, DOC),
    "claude-sonnet-4-6":          (3.00, 15.00, 0.300, DOC),
    "claude-sonnet-4-6-thinking": (3.00, 15.00, 0.300, DOC),
    "claude-sonnet-4-5":          (3.00, 15.00, 0.300, DOC),
    "claude-sonnet-4-5-20250929": (3.00, 15.00, 0.300, DOC),
    "claude-sonnet-4-5-thinking": (3.00, 15.00, 0.300, DOC),
    "claude-haiku-4-5":           (1.00, 5.00, 0.100, DOC),
    # ---- Gemini: first-party list prices (ai.google.dev/gemini-api/docs/pricing) ----
    # Flash thinking tiers (high/medium/low) share one standard price.
    "gemini-3.5-flash-high":      (1.50, 9.00, 0.15, DOC),
    "gemini-3.5-flash-medium":    (1.50, 9.00, 0.15, DOC),
    "gemini-3.5-flash-low":       (1.50, 9.00, 0.15, DOC),
    "gemini-3.5-pro":             (2.00, 12.00, 0.20, DOC),
    "gemini-3.5-pro-preview":     (2.00, 12.00, 0.20, EST),
    "gemini-3.5-flash":           (1.50, 9.00, 0.15, DOC),
    "gemini-3.5-flash-preview":   (1.50, 9.00, 0.15, EST),
    # Gemini 3.1 Pro standard, <=200k-token prompt tier.
    "gemini-3.1-pro-high":        (2.00, 12.00, 0.20, DOC),
    "gemini-3.1-pro-low":         (2.00, 12.00, 0.20, DOC),
    # Gemini 3 Flash Preview standard (the actual heavy Antigravity model).
    "gemini-3-flash-preview":     (0.50, 3.00, 0.05, DOC),
    "gemini-3-flash":             (0.50, 3.00, 0.05, DOC),
    # ---- Gemini CLI models (seen in ~/.gemini/tmp/*/chats logs) ----
    # 3.1 Pro preview shares the documented 3.1-pro standard tier.
    "gemini-3.1-pro-preview":     (2.00, 12.00, 0.20, DOC),
    "gemini-3.1-pro-preview-customtools": (2.00, 12.00, 0.20, DOC),
    # 3 Pro preview: no first-party row wired in yet -> estimate at pro tier.
    "gemini-3-pro-preview":       (2.00, 12.00, 0.20, EST),
    # 2.5 family: first-party list prices (<=200k tier).
    "gemini-2.5-pro":             (1.25, 10.00, 0.125, DOC),
    "gemini-2.5-flash":           (0.30, 2.50, 0.030, DOC),
    # 2.0 family: first-party list prices
    "gemini-2.0-flash":           (0.075, 0.30, 0.01875, DOC),
    "gemini-2.0-flash-lite":      (0.0375, 0.15, 0.009375, DOC),
    "gemini-2.0-pro":             (0.80, 3.20, 0.20, DOC),
    # Flash-lite tiers: estimate, no first-party row confirmed here.
    "gemini-3.1-flash-lite":          (0.25, 1.50, 0.025, DOC),
    "gemini-3.1-flash-lite-preview":  (0.25, 1.50, 0.025, DOC),
    # ---- OpenAI: first-party list prices (cached input -> cache_read) ----
    # Standard API-equivalent rates verified 2026-09-29:
    # https://developers.openai.com/api/docs/models/gpt-6.1-sol
    # Request-level long-context/service-tier premiums are not inferred from
    # daily token aggregates; see README's Sol 6.1 pricing scope.
    "gpt-6.1-sol":                (2.00, 10.00, 0.10, DOC),
    "gpt-5.6-sol-ultra":          (4.00, 20.00, 0.40, EST),
    "gpt-5.6-sol":                (4.00, 20.00, 0.40, DOC),
    "gpt-5.6-terra":              (2.00, 12.00, 0.200, DOC),
    "gpt-5.6-luna":               (0.20, 1.20, 0.020, DOC),
    "gpt-5.5":                    (5.00, 30.00, 0.50, DOC),
    "gpt-5.4":                    (2.50, 15.00, 0.25, DOC),
    "gpt-5.4-mini":               (0.75, 4.50, 0.075, DOC),
    "gpt-5-codex-mini":           (0.75, 4.50, 0.075, EST),
    "gpt-5.1-codex-mini":         (0.25, 2.00, 0.025, DOC),
    # gpt-oss is open-weights; Dave runs it free via OpenRouter/local. Nominal host est.
    "gpt-oss-120b-medium":        (0.10, 0.40, 0.010, EST),
    # --- audited against the official pricing pages, 2026-08-01 -------------
    # Absent rows are not neutral: they fall through to DEFAULT_PRICING at
    # $1/$4, so a missing premium model reads as an order of magnitude cheaper
    # than it is. gpt-5.5-pro and gpt-5.4-pro at $30/$180 were a 30x undercount.
    "gemini-3.6-flash":           (0.75, 3.75, 0.075, DOC),
    "gemini-3.5-flash-lite":      (0.30, 2.50, 0.030, DOC),
    "gemini-2.5-flash-lite":      (0.10, 0.40, 0.010, DOC),
    "gemini-embedding-001":       (0.15, 0.00, 0.000, DOC),
    "gpt-5.5-pro":                (30.00, 180.00, 0.000, DOC),
    "gpt-5.4-pro":                (30.00, 180.00, 0.000, DOC),
    "gpt-5.4-nano":               (0.20, 1.25, 0.020, DOC),
    # The model Codex actually reports; absent, every Codex log billed at $1/$4.
    "gpt-5.3-codex":              (1.75, 14.00, 0.175, DOC),
    "chat-latest":                (5.00, 30.00, 0.500, DOC),
}
# Sourced catalog supersedes older constants and adds every published model.
# Non-text modalities retain their own units in pricing_catalog, not this bill.
MODEL_PRICING.update(pricing_catalog.text_prices())
MODEL_PRICING["gpt-5.6-sol-ultra"] = (*MODEL_PRICING["gpt-5.6-sol"][:3], EST)
MODEL_PRICING.setdefault("gemini-4-carbon", (2.00, 10.00, 0.100, DOC))
MODEL_PRICING.setdefault("gemini-4-argon", (2.00, 10.00, 0.100, DOC))
# Unknown model: conservative estimate so the bill never silently reads $0.
DEFAULT_PRICING = (1.00, 4.00, 0.100, EST)


# Local Ollama/Gemma models and OpenRouter ':free' routes cost $0 — they are
# tracked for VOLUME, not spend (the fleet enforces a hard-free policy).
FREE_LOCAL_MODELS = {
    "dav1d:e2b", "sol-ai:e4b", "kaedra:e4b", "iris-ai:e4b",
    "gemma4-aggressive:e4b", "gemma4-aggressive:e2b", "gemma2:2b", "gemma:2b",
}



# --- dated pricing ---------------------------------------------------------
#
# A price is only true for a stretch of time, and this tool bills history: a day
# in August and a day in September are charged at different rates for the same
# model. A single number per model cannot express that, so it is wrong on one
# side of the boundary no matter which value you pick.
#
# Anthropic canceled Sonnet 5's planned September increase: $2/$10 remained
# standard. Keep a constant schedule so stale cached plans cannot reinstate it.
# Google explicitly publishes January 2027 transitions; use catalog schedules.
#
# Entries are (first_day, last_day_inclusive, (input, output, cache_read, src)).
# last_day of None means "still current".
PRICE_SCHEDULE = {
    "claude-sonnet-5": [
        ("2000-01-01", None, (2.00, 10.00, 0.200, DOC)),
    ],
}
PRICE_SCHEDULE["claude-sonnet-5-thinking"] = PRICE_SCHEDULE["claude-sonnet-5"]
PRICE_SCHEDULE.update(pricing_catalog.text_price_schedules())
# Google Gemini 4 Carbon & Argon double on 2026-12-01 (published per ai.google.dev/gemini-api/docs/pricing)
PRICE_SCHEDULE.setdefault("gemini-4-carbon", [
    ("2026-01-01", "2026-11-30", (2.00, 10.00, 0.100, DOC)),
    ("2026-12-01", None, (4.00, 20.00, 0.200, DOC)),
])
PRICE_SCHEDULE.setdefault("gemini-4-argon", [
    ("2026-01-01", "2026-11-30", (2.00, 10.00, 0.100, DOC)),
    ("2026-12-01", None, (4.00, 20.00, 0.200, DOC)),
])


def _scheduled_price(key, when):
    """Dated price for a model, or None when the flat table should answer.

    `when` is a YYYY-MM-DD string or a date/datetime. Without one there is no
    way to choose, so the flat table wins — callers that do not care about
    history keep the behaviour they had.
    """
    entries = PRICE_SCHEDULE.get(key)
    if not entries or when is None:
        return None
    day = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else str(when)[:10]
    if len(day) != 10:
        return None
    for start, end, price in entries:
        if start <= day and (end is None or day <= end):
            return price
    return None


def price_for(model_name, when=None):
    """Resolve (input, output, cache_read, source) for a model, ignoring the
    ' (estimated)' suffix the Antigravity fallback parser appends.

    Pass `when` (the day the tokens were spent) to get the rate in force then.
    Without it the flat table answers, which is the current rate for every model
    that has never been repriced.
    """
    key = (model_name or "").replace(" (estimated)", "").strip()
    # Free lanes: local Ollama/Gemma + OpenRouter ':free' routes.
    if key.endswith(":free") or key in FREE_LOCAL_MODELS:
        return (0.0, 0.0, 0.0, DOC)
    return pricing_live.resolve_price(
        key,
        when=when,
        fallback_pricing=MODEL_PRICING,
        default_pricing=DEFAULT_PRICING,
        variant_suffixes=_VARIANT_SUFFIXES,
        price_schedule=PRICE_SCHEDULE,
    )


#: Suffixes a provider appends to a model id without changing the model being
#: billed — reasoning effort, routing hints. `gemini-3.5-flash-high` is
#: `gemini-3.5-flash` at a different effort, not a different price list.
_VARIANT_SUFFIXES = tuple(
    s.strip().lower() for s in os.environ.get(
        "MODEL_VARIANT_SUFFIXES",
        "high,xhigh,max,ultra,medium,low,minimal,thinking,latest,preview,customtools").split(",")
    if s.strip())


def _family_price(key):
    """Price an unlisted variant from its FAMILY rather than the generic default.

    A table of exact ids goes stale the moment a provider ships a variant, and
    the failure is silent and cheap-looking: an unlisted id lands on
    DEFAULT_PRICING, which is lower than every current Gemini rate, so the
    report under-bills exactly the newest models. `gemini-3.6-flash-high` — the
    latest stable family — priced at the 1.0/4.0 default against 3.5's
    documented 1.5/9.0.

    So: strip one trailing segment at a time and re-look-up, but ONLY while the
    dropped segment is a known variant suffix. That keeps `gemini-3.6-flash-high`
    resolving to `gemini-3.6-flash` while refusing to let `gemini-3.6-flash`
    collapse into some unrelated `gemini-3.6`.

    The returned source tag is marked inferred, never `doc`. A rate nobody
    published should not claim to be documented.
    """
    parts = key.split("-")
    while len(parts) > 1 and parts[-1].lower() in _VARIANT_SUFFIXES:
        parts = parts[:-1]
        found = pricing_live.resolve_exact_price(
            "-".join(parts),
            fallback_pricing=MODEL_PRICING,
        )
        if found is not None:
            inp, out, cache, _source = found
            return (inp, out, cache, EST)
    return DEFAULT_PRICING


def model_bill(model_name, d, when=None):
    """Honest API-equivalent cost (USD) for one model's token bucket.

    Cache-reads are billed at their discounted rate, cache-creation at 1.25x
    input, and reasoning at the output rate — the way a real invoice prices
    them. Returns (cost_usd, source_tag). Evaluated with decimal.Decimal end-to-end.
    """
    inp, out, cache_read, src = price_for(model_name, when)
    cost = calculate_cost(
        input_tokens=d.get("input_tokens", 0),
        cache_write_tokens=d.get("cache_creation_input_tokens", 0) or d.get("cache_write", 0),
        cache_read_tokens=d.get("cache_read_input_tokens", 0) or d.get("cache_read", 0),
        output_tokens=d.get("output_tokens", 0),
        reasoning_tokens=d.get("reasoning_tokens", 0) or d.get("reasoning", 0),
        inp_rate=inp,
        out_rate=out,
        cache_read_rate=cache_read,
    )
    return float(cost), src



def fold_openai_usage(usage):
    """Fold an OpenAI usage block into Anthropic's disjoint-bucket schema.

    OpenAI nests its subtotals: ``cached_input_tokens`` is part of
    ``input_tokens``, and ``reasoning_output_tokens`` is part of
    ``output_tokens``. The payload proves it with its own arithmetic --
    ``input_tokens + output_tokens == total_tokens`` on every row, with cached
    and reasoning excluded from that sum.

    Anthropic keeps those buckets disjoint, and every total in this file
    (``i + o + cc + cr + rt``) and every :func:`model_bill` call is written to
    Anthropic's rules. Folding here, at the parse boundary, means there is one
    set of rules below this line instead of two.

    Left unfolded, cached input is counted inside ``input_tokens`` and again as
    ``cache_read`` -- billed at the full input rate AND the cache rate -- while
    reasoning is billed at the output rate on top of the output tokens it
    already sits inside. Measured on 2026-08-27: 32.26M phantom tokens, and a
    97.3% cache hit rate mislabelled as a 49.5% "cold context leak".

    Returns a dict whose ``input_tokens``/``cache_read`` sum back to the raw
    input, and whose ``output_tokens``/``reasoning`` sum back to the raw output.
    """
    raw_input = int(usage.get("input_tokens") or 0)
    raw_output = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cached_input_tokens") or 0)
    reasoning = int(usage.get("reasoning_output_tokens") or 0)

    # Clamp rather than trust: a subtotal larger than the bucket it is nested in
    # is a schema change, and silently emitting a negative count would corrupt
    # every downstream sum. Clamping keeps the invariant that the folded parts
    # never exceed the raw whole.
    cache_read = min(cache_read, raw_input)
    reasoning = min(reasoning, raw_output)

    return {
        "input_tokens": raw_input - cache_read,
        "output_tokens": raw_output - reasoning,
        "cache_read": cache_read,
        "reasoning": reasoning,
        "total_tokens": int(usage.get("total_tokens") or (raw_input + raw_output)),
        "raw_input": raw_input,
        "raw_output": raw_output,
    }


def parse_ts(rec):
    if not rec or not isinstance(rec, dict):
        return None
    ts = rec.get("timestamp") or rec.get("created_at") or rec.get("startTime") or rec.get("start_time")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return None

def usage_of(rec):
    msg = rec.get("message")
    u = msg.get("usage") if isinstance(msg, dict) else None
    if u is None:
        u = rec.get("usage")
    return u if isinstance(u, dict) else None

def model_of(rec):
    msg = rec.get("message")
    if isinstance(msg, dict) and msg.get("model"):
        return msg["model"]
    return rec.get("model") or "unknown"

def fmt(n):
    return f"{n:,}"

def cols(d):
    return (d["input_tokens"], d["output_tokens"],
            d["cache_creation_input_tokens"], d["cache_read_input_tokens"], d["reasoning_tokens"])

# --- Parse Claude Code Logs ---
def parse_claude():
    # Sorted, because this parser dedupes by requestId across every file and
    # the FIRST copy of a duplicated id is the one counted — and therefore the
    # one whose timestamp decides which day it lands on. glob returns
    # filesystem order, which differs between machines.
    files = sorted(glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True))
    by_day = defaultdict(lambda: defaultdict(int))
    by_model = defaultdict(lambda: defaultdict(int))
    totals = defaultdict(int)
    records = 0
    seen = set()

    for f in files:
        try:
            fh = open(f, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                u = usage_of(rec)
                if not u:
                    continue
                ts = parse_ts(rec)
                if ts is None or ts < CUTOFF or ts > LIMIT_UPPER:
                    continue
                # One API request writes SEVERAL transcript rows — the assistant
                # message, then one per tool_use block — and every row repeats
                # the same usage object under a fresh uuid. Deduping by uuid
                # therefore dedupes nothing, and the report bills a request once
                # per block it happened to produce.
                #
                # Measured on this box 2026-08-01 over 6 transcripts: 652 of
                # 1,136 requests spanned multiple rows, usage byte-identical in
                # every single one, none differing. Summing rows gave 554M
                # cache-read tokens where summing requests gives 287M — the
                # headline number was inflated 1.9x, and so was the shadow bill.
                #
                # requestId is the unit the provider bills, so it is the unit of
                # dedup. uuid remains the fallback for the rare row without one.
                uid = rec.get("requestId") or rec.get("request_id") or rec.get("uuid")
                if uid is not None:
                    if uid in seen:
                        continue
                    seen.add(uid)
                day = ts.strftime("%Y-%m-%d")
                model = model_of(rec)
                for k in KEYS:
                    v = int(u.get(k) or 0)
                    by_day[day][k] += v
                    by_model[model][k] += v
                    totals[k] += v
                
                it = int(u.get("input_tokens") or 0)
                ot = int(u.get("output_tokens") or 0)
                cc = int(u.get("cache_creation_input_tokens") or 0)
                cr = int(u.get("cache_read_input_tokens") or 0)
                rt = int(u.get("reasoning_tokens") or 0)
                
                ALL_INVOCATIONS.append({
                    "timestamp": ts,
                    "source": "Claude Code",
                    "model": model,
                    "input_tokens": it,
                    "output_tokens": ot,
                    "cache_creation": cc,
                    "cache_read": cr,
                    "reasoning": rt,
                    "exact": True,
                    "session_id": rec.get("project_name") or (f.split(os.sep)[-2] if len(f.split(os.sep)) >= 2 else "unknown_project"),
                    "source_file": os.path.basename(f)
                })
                records += 1
    return by_day, by_model, totals, len(files), records

# --- RPC Locator ---
def locate_antigravity_rpc():
    candidates = []
    # 1. WMIC process detection.
    #
    # wmic.exe was removed in Windows 11 24H2. The call still "worked" here in
    # the sense that the exception was caught — but with shell=True and stderr
    # inherited, cmd.exe printed "'wmic' is not recognized" straight into the
    # middle of the report on every run. stderr is captured now, and the
    # PowerShell path below is the one that actually finds anything on a
    # current box.
    try:
        cmd = 'wmic process get ProcessId,CommandLine /FORMAT:CSV'
        output = subprocess.check_output(
            cmd, shell=True, stderr=subprocess.DEVNULL
        ).decode('utf-8', errors='ignore')
        for line in output.splitlines():
            line = line.strip()
            if not line or "wmic" in line:
                continue
            if "language_server" in line.lower() and "--csrf_token" in line:
                parts = line.split(',')
                if len(parts) >= 3:
                    pid_str = parts[-1].strip()
                    cmd_line = ",".join(parts[1:-1])
                    try:
                        pid = int(pid_str)
                    except ValueError:
                        continue
                    token_match = re.search(r'--csrf_token\s+([a-f0-9-]+)', cmd_line)
                    if token_match:
                        candidates.append({"pid": pid, "token": token_match.group(1)})
    except Exception:
        pass

    # 2. PowerShell fallback process detection
    if not candidates:
        try:
            cmd = 'powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like \'*language_server*\' } | Select-Object ProcessId, CommandLine | ConvertTo-Json"'
            output = subprocess.check_output(
                cmd, shell=True, stderr=subprocess.DEVNULL
            ).decode('utf-8', errors='ignore')
            if output.strip():
                data = json.loads(output)
                if isinstance(data, dict):
                    data = [data]
                for p in data:
                    pid = p.get("ProcessId")
                    cmd_line = p.get("CommandLine") or ""
                    token_match = re.search(r'--csrf_token\s+([a-f0-9-]+)', cmd_line)
                    if pid and token_match:
                        candidates.append({"pid": pid, "token": token_match.group(1)})
        except Exception:
            pass

    verified_conns = []
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for cand in candidates:
        pid = cand["pid"]
        token = cand["token"]
        ports = []
        try:
            output = subprocess.check_output("netstat -ano", shell=True).decode('utf-8', errors='ignore')
            for line in output.splitlines():
                if "LISTENING" in line and str(pid) in line.split()[-1]:
                    parts = line.split()
                    port_match = re.search(r':(\d+)$', parts[1])
                    if port_match:
                        ports.append(int(port_match.group(1)))
        except Exception:
            pass
            
        for port in set(ports):
            url = f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/Heartbeat"
            req = urllib.request.Request(
                url,
                data=json.dumps({"uuid": "00000000-0000-0000-0000-000000000000"}).encode('utf-8'),
                headers={
                    "Content-Type": "application/json",
                    "Connect-Protocol-Version": "1",
                    "X-Codeium-Csrf-Token": token
                },
                method="POST"
            )
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=1.5) as resp:
                    if resp.status == 200:
                        verified_conns.append((port, token))
            except Exception:
                pass
    return verified_conns

# --- Parse Antigravity Logs & RPC ---
def parse_antigravity():
    by_day = defaultdict(lambda: defaultdict(int))
    by_model = defaultdict(lambda: defaultdict(int))
    totals = defaultdict(int)
    records = 0
    active_cascade_ids = set()
    rpc_scanned_invocations = 0

    # 1. Attempt RPC Retrieval
    rpc_conns = locate_antigravity_rpc()
    for port, token in rpc_conns:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        # Get active trajectories
        url_traj = f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories"
        req_traj = urllib.request.Request(
            url_traj,
            data=json.dumps({}).encode('utf-8'),
            headers={
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "X-Codeium-Csrf-Token": token
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_traj, context=ctx, timeout=3) as resp:
                traj_data = json.loads(resp.read().decode('utf-8'))
                summaries = traj_data.get("trajectorySummaries", {})
                for cascade_id in summaries.keys():
                    active_cascade_ids.add(cascade_id)
                    
                    # Query metadata
                    url_meta = f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetCascadeTrajectoryGeneratorMetadata"
                    req_meta = urllib.request.Request(
                        url_meta,
                        data=json.dumps({"cascadeId": cascade_id}).encode('utf-8'),
                        headers={
                            "Content-Type": "application/json",
                            "Connect-Protocol-Version": "1",
                            "X-Codeium-Csrf-Token": token
                        },
                        method="POST"
                    )
                    with urllib.request.urlopen(req_meta, context=ctx, timeout=3) as resp_meta:
                        meta_data = json.loads(resp_meta.read().decode('utf-8'))
                        for item in meta_data.get("generatorMetadata", []):
                            chat_model = item.get("chatModel", {})
                            usage = chat_model.get("usage", {})
                            if not usage:
                                continue
                            
                            it = int(usage.get("inputTokens") or usage.get("input_token_count") or usage.get("prompt_token_count") or usage.get("prompt_eval_count") or 0)
                            ot = int(usage.get("outputTokens") or usage.get("output_token_count") or usage.get("eval_count") or 0)
                            cc = int(usage.get("cacheCreationInputTokens") or usage.get("cacheWriteTokens") or 0)
                            cr = int(usage.get("cachedContentTokenCount") or usage.get("cached_content_token_count") or usage.get("cacheReadTokens") or 0)
                            rt = int(usage.get("reasoning_tokens") or usage.get("thinking_tokens") or usage.get("reasoning_output_tokens") or 0)
                            model_id = chat_model.get("model") or usage.get("model") or "unknown"
                            model_name = resolve_model(model_id)
                            
                            ts_str = chat_model.get("chatStartMetadata", {}).get("createdAt")
                            ts = None
                            if ts_str:
                                try:
                                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone()
                                except Exception:
                                    pass
                                    
                            if ts and CUTOFF <= ts <= LIMIT_UPPER:
                                day = ts.strftime("%Y-%m-%d")
                                by_day[day]["input_tokens"] += it
                                by_day[day]["output_tokens"] += ot
                                by_day[day]["cache_creation_input_tokens"] += cc
                                by_day[day]["cache_read_input_tokens"] += cr
                                by_day[day]["reasoning_tokens"] += rt
                                by_model[model_name]["input_tokens"] += it
                                by_model[model_name]["output_tokens"] += ot
                                by_model[model_name]["cache_creation_input_tokens"] += cc
                                by_model[model_name]["cache_read_input_tokens"] += cr
                                by_model[model_name]["reasoning_tokens"] += rt
                                totals["input_tokens"] += it
                                totals["output_tokens"] += ot
                                totals["cache_creation_input_tokens"] += cc
                                totals["cache_read_input_tokens"] += cr
                                totals["reasoning_tokens"] += rt
                                ALL_INVOCATIONS.append({
                                    "timestamp": ts,
                                    "source": "Antigravity (RPC)",
                                    "model": model_name,
                                    "input_tokens": it,
                                    "output_tokens": ot,
                                    "cache_creation": cc,
                                    "cache_read": cr,
                                    "reasoning": rt,
                                    "exact": True,
                                    "session_id": cascade_id,
                                    "source_file": "RPC"
                                })
                                rpc_scanned_invocations += 1
                                records += 1
        except Exception:
            pass

    # 2. File-based Fallback for closed/archived sessions
    files = []
    for brain_dir in ANTIGRAVITY_BRAIN_DIRS:
        if os.path.exists(brain_dir):
            # Same reason as parse_claude: this parser dedupes too.
            for root, dirs, filenames in os.walk(brain_dir):
                dirs.sort()
                for filename in sorted(filenames):
                    if filename == "transcript.jsonl":
                        files.append(os.path.join(root, filename))

    seen = set()
    fallback_transcripts_scanned = 0
    fallback_invocations_estimated = 0

    for f in files:
        parts = os.path.normpath(f).split(os.sep)
        conv_id = parts[-4] if len(parts) >= 4 else "unknown_conv"
        
        # Skip active sessions that were already queried via RPC
        if conv_id in active_cascade_ids:
            continue
            
        try:
            fh = open(f, encoding="utf-8", errors="ignore")
        except OSError:
            continue
            
        fallback_transcripts_scanned += 1
        with fh:
            accumulated_chars = 0
            last_model_call_accumulated_chars = 0
            current_model = "gemini-3-flash-preview"  # Default fallback (most Antigravity sessions)
            for idx, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                
                # Check for model setting changes (only in user input or system generated blocks)
                content = rec.get("content") or ""
                # More specific regex to ensure we are in a settings change block
                match = re.search(r"<USER_SETTINGS_CHANGE>\s*The user changed setting `Model Selection` from .*? to (.*?)(?:\.\s|\.$|$)", content)
                if match:
                    model_candidate = match.group(1).strip()
                    name_lower = model_candidate.lower()
                    if "gemini 3.5 flash (high)" in name_lower:
                        current_model = "gemini-3.5-flash-high"
                    elif "gemini 3.5 flash (medium)" in name_lower:
                        current_model = "gemini-3.5-flash-medium"
                    elif "gemini 3.5 flash (low)" in name_lower:
                        current_model = "gemini-3.5-flash-low"
                    elif "gemini 3.5 pro" in name_lower:
                        current_model = "gemini-3.5-pro"
                    elif "gemini 3.5 flash" in name_lower:
                        current_model = "gemini-3.5-flash"
                    elif "gemini 3.1 pro (high)" in name_lower:
                        current_model = "gemini-3.1-pro-high"
                    elif "gemini 3.1 pro (low)" in name_lower:
                        current_model = "gemini-3.1-pro-low"
                    elif "claude sonnet 4.6" in name_lower:
                        current_model = "claude-sonnet-4-6-thinking"
                    elif "claude opus 4.6" in name_lower:
                        current_model = "claude-opus-4-6-thinking"
                    elif "gpt-5.6 sol" in name_lower or "gpt 5.6 sol" in name_lower:
                        current_model = "gpt-5.6-sol"
                    elif "gpt-5.6 terra" in name_lower or "gpt 5.6 terra" in name_lower:
                        current_model = "gpt-5.6-terra"
                    elif "gpt-5.6 luna" in name_lower or "gpt 5.6 luna" in name_lower:
                        current_model = "gpt-5.6-luna"
                    elif "gpt-oss 120b" in name_lower or "gpt-oss 128b" in name_lower:
                        current_model = "gpt-oss-120b-medium"
                    elif "gemini 3 flash" in name_lower:
                        current_model = "gemini-3-flash-preview"
                    elif "gemini 3" in name_lower:
                        current_model = "gemini-3-flash-preview"
                    else:
                        current_model = model_candidate

                ts = parse_ts(rec)
                if ts is None or ts < CUTOFF or ts > LIMIT_UPPER:
                    continue
                
                thinking = rec.get("thinking") or ""
                tool_calls = str(rec.get("tool_calls") or "")
                step_chars = len(content) + len(thinking) + len(tool_calls)
                
                source = rec.get("source")
                step_type = rec.get("type")
                
                # Deduplicate step calls
                step_uid = f"{conv_id}_{idx}"
                if step_uid in seen:
                    continue
                seen.add(step_uid)
                
                if source == "MODEL" and step_type == "PLANNER_RESPONSE":
                    # Estimate token usage
                    ot = max(1, step_chars // 4)
                    total_in = (accumulated_chars // 4) + 6000
                    
                    if last_model_call_accumulated_chars > 0:
                        cr = (last_model_call_accumulated_chars // 4) + 6000
                        it = max(0, total_in - cr)
                    else:
                        cr = 0
                        it = total_in
                        
                    last_model_call_accumulated_chars = accumulated_chars + step_chars
                    
                    day = ts.strftime("%Y-%m-%d")
                    model = f"{current_model} (estimated)"
                    
                    by_day[day]["input_tokens"] += it
                    by_day[day]["output_tokens"] += ot
                    by_day[day]["cache_read_input_tokens"] += cr
                    
                    by_model[model]["input_tokens"] += it
                    by_model[model]["output_tokens"] += ot
                    by_model[model]["cache_read_input_tokens"] += cr
                    
                    totals["input_tokens"] += it
                    totals["output_tokens"] += ot
                    totals["cache_read_input_tokens"] += cr
                    
                    ALL_INVOCATIONS.append({
                        "timestamp": ts,
                        "source": "Antigravity (Fallback)",
                        "model": model,
                        "input_tokens": it,
                        "output_tokens": ot,
                        "cache_creation": 0,
                        "cache_read": cr,
                        "reasoning": 0,
                        "exact": False,
                        "session_id": conv_id,
                        "source_file": os.path.basename(f)
                    })
                    fallback_invocations_estimated += 1
                    records += 1
                
                accumulated_chars += step_chars
                
    total_transcripts_scanned = len(active_cascade_ids) + fallback_transcripts_scanned
    return by_day, by_model, totals, total_transcripts_scanned, records, len(active_cascade_ids), rpc_scanned_invocations, fallback_invocations_estimated

# --- Parse Codex Logs ---
def parse_codex():
    by_day = defaultdict(lambda: defaultdict(int))
    by_model = defaultdict(lambda: defaultdict(int))
    totals = defaultdict(int)
    records = 0
    sessions_scanned = 0

    if not os.path.exists(CODEX_STATE):
        return by_day, by_model, totals, 0, 0

    try:
        conn = sqlite3.connect(CODEX_STATE)
        cursor = conn.cursor()
        
        # Query threads updated since cutoff
        cutoff_ts = int(CUTOFF.timestamp())
        cursor.execute("SELECT model, rollout_path FROM threads WHERE updated_at > ? AND rollout_path IS NOT NULL;", (cutoff_ts,))
        rows = cursor.fetchall()
        
        for model_name, rollout_path in rows:
            if not os.path.exists(rollout_path):
                continue
            
            sessions_scanned += 1
            try:
                with open(rollout_path, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line: continue
                        try:
                            rec = json.loads(line)
                        except: continue
                        
                        if rec.get("type") == "event_msg":
                            payload = rec.get("payload") or {}
                            if payload.get("type") == "token_count":
                                info = payload.get("info") or {}
                                usage = info.get("last_token_usage") or {}
                                if not usage: continue
                                
                                folded = fold_openai_usage(usage)
                                it = folded["input_tokens"]
                                ot = folded["output_tokens"]
                                cr = folded["cache_read"]
                                rt = folded["reasoning"]
                                total = folded["total_tokens"]
                                raw_input = folded["raw_input"]
                                context_window = int(info.get("model_context_window") or 0)
                                rate_limits = payload.get("rate_limits") or {}
                                primary_limit = rate_limits.get("primary") or {}
                                secondary_limit = rate_limits.get("secondary") or {}
                                
                                ts_str = rec.get("timestamp")
                                ts = None
                                if ts_str:
                                    try:
                                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone()
                                    except: pass
                                
                                if ts and CUTOFF <= ts <= LIMIT_UPPER:
                                    day = ts.strftime("%Y-%m-%d")
                                    by_day[day]["input_tokens"] += it
                                    by_day[day]["output_tokens"] += ot
                                    by_day[day]["cache_read_input_tokens"] += cr
                                    by_day[day]["reasoning_tokens"] += rt
                                    
                                    by_model[model_name]["input_tokens"] += it
                                    by_model[model_name]["output_tokens"] += ot
                                    by_model[model_name]["cache_read_input_tokens"] += cr
                                    by_model[model_name]["reasoning_tokens"] += rt
                                    
                                    totals["input_tokens"] += it
                                    totals["output_tokens"] += ot
                                    totals["cache_read_input_tokens"] += cr
                                    totals["reasoning_tokens"] += rt
                                    ALL_INVOCATIONS.append({
                                        "timestamp": ts,
                                        "source": "OpenAI Codex",
                                        "model": model_name,
                                        "input_tokens": it,
                                        "output_tokens": ot,
                                        "cache_creation": 0,
                                        "cache_read": cr,
                                        "reasoning": rt,
                                        "exact": True,
                                        "session_id": os.path.splitext(os.path.basename(rollout_path))[0],
                                        "source_file": os.path.basename(rollout_path),
                                        "openai_total_tokens": total,
                                        "openai_uncached_input_tokens": it,
                                        "openai_context_input_tokens": raw_input,
                                        "openai_context_window": context_window,
                                        "openai_plan_type": rate_limits.get("plan_type"),
                                        "openai_primary_used_percent": primary_limit.get("used_percent"),
                                        "openai_primary_window_minutes": primary_limit.get("window_minutes"),
                                        "openai_primary_resets_at": primary_limit.get("resets_at"),
                                        "openai_secondary_used_percent": secondary_limit.get("used_percent"),
                                        "openai_secondary_window_minutes": secondary_limit.get("window_minutes"),
                                        "openai_secondary_resets_at": secondary_limit.get("resets_at"),
                                    })
                                    records += 1
            except:
                continue
        conn.close()
    except:
        pass
        
    return by_day, by_model, totals, sessions_scanned, records


def codex_native_total():
    """Codex's OWN lifetime counter: SUM(threads.tokens_used), windowed to the
    report range by thread updated_at. This is the figure behind Codex's
    `/usage` display -- a cross-check on our granular rollout parse. Thread-level
    granularity (coarser than per-event), so treat as an order-of-magnitude
    validator, not a row-exact match. Returns (thread_count, total_tokens)."""
    if not os.path.exists(CODEX_STATE):
        return 0, 0
    try:
        conn = sqlite3.connect(CODEX_STATE)
        lo = int(CUTOFF.timestamp())
        hi = int(LIMIT_UPPER.timestamp())
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens_used), 0) FROM threads "
            "WHERE updated_at > ? AND updated_at <= ?",
            (lo, hi),
        ).fetchone()
        conn.close()
        return int(row[0] or 0), int(row[1] or 0)
    except Exception:
        return 0, 0


# --- Parse Gemini CLI chat logs (estimated; no exact token telemetry on disk) ---
# The Gemini CLI stores conversations under ~/.gemini/tmp/<project>/chats/ as
# either a single .json file with a messages[] array, or .jsonl one msg/line.
# These hold message TEXT only -- there is no usageMetadata/token telemetry
# recorded -- so tokens are ESTIMATED via the same chars/4 heuristic the
# Antigravity fallback uses. This is the ONLY source for pre-May 2026 Gemini
# activity (the Antigravity brain transcripts only go back to ~2026-05-19).
GEMINI_CLI_CHAT_GLOBS = [
    os.path.expanduser(os.path.join("~", ".gemini", "tmp", "*", "chats", "*.json")),
    os.path.expanduser(os.path.join("~", ".gemini", "tmp", "*", "chats", "*.jsonl")),
    os.path.expanduser(os.path.join("~", ".gemini", "tmp", "*", "chats", "*", "*.jsonl")),
]


def _gemini_text(content):
    """Flatten a CLI message 'content' (str | list[{text}] | dict) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict):
                out.append(str(p.get("text") or p.get("content") or ""))
            else:
                out.append(str(p))
        return "".join(out)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content or "")


def parse_gemini_cli():
    by_day = defaultdict(lambda: defaultdict(int))
    by_model = defaultdict(lambda: defaultdict(int))
    totals = defaultdict(int)
    records = 0
    scanned_files = 0
    exact_cnt = 0
    est_cnt = 0

    files = []
    for g in GEMINI_CLI_CHAT_GLOBS:
        files.extend(glob.glob(g))
    files = sorted(set(files))

    cutoff_date = CUTOFF.date() - timedelta(days=1)
    limit_date = (
        date.max if LIMIT_UPPER.date() >= date.max - timedelta(days=1)
        else LIMIT_UPPER.date() + timedelta(days=1)
    )
    local_tz = datetime.now().astimezone().tzinfo

    for f in files:
        basename = os.path.basename(f)
        match = re.search(r"session-(\d{4}-\d{2}-\d{2})", basename)
        if not match:
            continue
        try:
            file_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue

        if not (cutoff_date <= file_date <= limit_date):
            continue

        messages_by_id = {}
        session_start_time = None

        try:
            if f.endswith(".json"):
                with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                    data = json.load(fh)
                    session_start_time = parse_ts(data)
                    for m in data.get("messages", []):
                        if "id" in m:
                            messages_by_id[m["id"]] = m
            else:  # .jsonl, one message per line
                with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        if rec.get("startTime") and not session_start_time:
                            session_start_time = parse_ts(rec)
                        if "id" in rec:
                            messages_by_id[rec["id"]] = rec
        except Exception:
            continue

        if not messages_by_id:
            continue

        if not session_start_time:
            session_start_time = datetime.combine(file_date, datetime.min.time()).replace(tzinfo=local_tz)

        def get_msg_time(item):
            msg = item[1]
            ts = parse_ts(msg)
            return ts.timestamp() if ts else 0

        sorted_messages = [msg for msg_id, msg in sorted(messages_by_id.items(), key=get_msg_time)]

        session_chars = 0
        previous_session_chars = 0
        scanned_files += 1

        # Propagate model selection within session
        current_model = "gemini-3-flash-preview"
        for msg in sorted_messages:
            m_id = msg.get("model")
            if m_id and m_id != "unknown":
                current_model = m_id
                break

        for msg in sorted_messages:
            role = (msg.get("type") or msg.get("role") or "").lower()
            if role in ("user", "human"):
                text = _gemini_text(msg.get("content") if msg.get("content") is not None else msg.get("text"))
                session_chars += len(text)
            elif role in ("gemini", "model", "assistant"):
                ts = parse_ts(msg) or session_start_time
                text = _gemini_text(msg.get("content") if msg.get("content") is not None else msg.get("text"))
                step_chars = len(text)
                
                if not (CUTOFF <= ts <= LIMIT_UPPER):
                    session_chars += step_chars + len(str(msg.get("thoughts") or ""))
                    continue

                model_id = msg.get("model")
                if not model_id or model_id == "unknown":
                    model_name = resolve_model(current_model)
                else:
                    model_name = resolve_model(model_id)
                    current_model = model_id

                tokens = msg.get("tokens")
                has_exact = False
                if tokens and isinstance(tokens, dict):
                    it = int(tokens.get("input") or 0)
                    ot = int(tokens.get("output") or 0)
                    cr = int(tokens.get("cached") or 0)
                    rt = int(tokens.get("thoughts") or 0)
                    if it > 0 or ot > 0:
                        has_exact = True

                if has_exact:
                    exact = True
                    exact_cnt += 1
                else:
                    exact = False
                    est_cnt += 1
                    total_in = (session_chars // 4) + 6000
                    if previous_session_chars > 0:
                        cr = (previous_session_chars // 4) + 6000
                        it = max(0, total_in - cr)
                    else:
                        cr = 0
                        it = total_in
                    ot = max(1, step_chars // 4)
                    rt = len(str(msg.get("thoughts") or "")) // 4

                day = ts.strftime("%Y-%m-%d")
                model_label = model_name if exact else f"{model_name} (estimated)"

                by_day[day]["input_tokens"] += it
                by_day[day]["output_tokens"] += ot
                by_day[day]["cache_read_input_tokens"] += cr
                by_day[day]["reasoning_tokens"] += rt

                by_model[model_label]["input_tokens"] += it
                by_model[model_label]["output_tokens"] += ot
                by_model[model_label]["cache_read_input_tokens"] += cr
                by_model[model_label]["reasoning_tokens"] += rt

                totals["input_tokens"] += it
                totals["output_tokens"] += ot
                totals["cache_read_input_tokens"] += cr
                totals["reasoning_tokens"] += rt

                parts = os.path.normpath(f).split(os.sep)
                proj_name = parts[-3] if len(parts) >= 3 else "unknown_project"

                ALL_INVOCATIONS.append({
                    "timestamp": ts,
                    "source": "Gemini CLI",
                    "model": model_label,
                    "input_tokens": it,
                    "output_tokens": ot,
                    "cache_creation": 0,
                    "cache_read": cr,
                    "reasoning": rt,
                    "exact": exact,
                    "session_id": proj_name,
                    "source_file": basename
                })
                records += 1
                previous_session_chars = session_chars
                session_chars += step_chars + len(str(msg.get("thoughts") or ""))

    return by_day, by_model, totals, scanned_files, records, exact_cnt, est_cnt


# --- Parse Fleet Usage Ledger (forward, EXACT: local Ollama/Gemma, OpenRouter, HF) ---
# Append-only JSONL written by fleet_usage_proxy.py + instrumented fleet clients.
# Each line: {timestamp, provider, lane, model, input_tokens, output_tokens,
# cached_tokens, reasoning_tokens, source}. These are exact counts from each
# lane's own API response (ollama prompt_eval_count/eval_count; OpenRouter usage).
def parse_fleet_usage():
    by_day = defaultdict(lambda: defaultdict(int))
    by_model = defaultdict(lambda: defaultdict(int))
    totals = defaultdict(int)
    records = 0

    if not os.path.exists(FLEET_USAGE_LEDGER):
        return by_day, by_model, totals, records

    try:
        fh = open(FLEET_USAGE_LEDGER, encoding="utf-8", errors="ignore")
    except OSError:
        return by_day, by_model, totals, records

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = parse_ts(rec)
            if ts is None or ts < CUTOFF or ts > LIMIT_UPPER:
                continue

            provider = rec.get("provider") or "Fleet"
            model = rec.get("model") or "unknown"
            it = int(rec.get("input_tokens") or 0)
            ot = int(rec.get("output_tokens") or 0)
            cr = int(rec.get("cached_tokens") or 0)
            rt = int(rec.get("reasoning_tokens") or 0)

            day = ts.strftime("%Y-%m-%d")
            by_day[day]["input_tokens"] += it
            by_day[day]["output_tokens"] += ot
            by_day[day]["cache_read_input_tokens"] += cr
            by_day[day]["reasoning_tokens"] += rt

            by_model[model]["input_tokens"] += it
            by_model[model]["output_tokens"] += ot
            by_model[model]["cache_read_input_tokens"] += cr
            by_model[model]["reasoning_tokens"] += rt

            totals["input_tokens"] += it
            totals["output_tokens"] += ot
            totals["cache_read_input_tokens"] += cr
            totals["reasoning_tokens"] += rt

            ALL_INVOCATIONS.append({
                "timestamp": ts,
                "source": f"Fleet:{provider}",
                "model": model,
                "input_tokens": it,
                "output_tokens": ot,
                "cache_creation": 0,
                "cache_read": cr,
                "reasoning": rt,
                "exact": True,
                "session_id": rec.get("lane") or "fleet",
                "source_file": os.path.basename(FLEET_USAGE_LEDGER),
            })
            records += 1

    return by_day, by_model, totals, records


def compute_and_print_split(invocations, title):
    exact_tok = 0
    est_tok = 0
    exact_cost = 0.0
    est_cost = 0.0
    
    for inv in invocations:
        it = inv.get("input_tokens", 0)
        ot = inv.get("output_tokens", 0)
        cc = inv.get("cache_creation", 0)
        cr = inv.get("cache_read", 0)
        rt = inv.get("reasoning", 0)
        tot = it + ot + cc + cr + rt
        
        cost, _ = model_bill(inv["model"], {
            "input_tokens": it,
            "cache_creation_input_tokens": cc,
            "cache_read_input_tokens": cr,
            "output_tokens": ot,
            "reasoning_tokens": rt
        })
        
        if inv.get("exact", True):
            exact_tok += tot
            exact_cost += cost
        else:
            est_tok += tot
            est_cost += cost
            
    blended_tok = exact_tok + est_tok
    blended_cost = exact_cost + est_cost
    confidence = (exact_tok / blended_tok * 100) if blended_tok > 0 else 100.0
    
    print(f"--- Exact vs Estimated Split ({title}) ---")
    print(f"Exact tokens:                       {fmt(exact_tok)}")
    print(f"Estimated tokens:                   {fmt(est_tok)}")
    print(f"Blended total:                      {fmt(blended_tok)}")
    print(f"Confidence:                         {confidence:.1f}%")
    print()
    print(f"Exact shadow cost:                  ${exact_cost:,.2f}")
    print(f"Estimated shadow cost:              ${est_cost:,.2f}")
    print(f"Blended shadow cost:                ${blended_cost:,.2f}")
    print("----------------------------------------------------------------------\n")


def get_cache_label(share):
    if share >= 0.95:
        return "Excellent"
    elif share >= 0.85:
        return "Good"
    elif share >= 0.60:
        return "Warning"
    else:
        return "Cold context leak"


def print_cache_health_report(invocations):
    print("======================================================================")
    print("CACHE HEALTH SCORE REPORT")
    print("======================================================================")
    
    # 1. By Model
    print("--- By Model Cache Health ---")
    model_stats = defaultdict(lambda: {"input": 0, "total": 0, "cache_read": 0})
    for inv in invocations:
        m = inv["model"]
        total = inv["input_tokens"] + inv["output_tokens"] + inv["cache_creation"] + inv["cache_read"] + inv["reasoning"]
        model_stats[m]["input"] += inv["input_tokens"]
        model_stats[m]["total"] += total
        model_stats[m]["cache_read"] += inv["cache_read"]
        
    for m, stats in sorted(model_stats.items()):
        total = stats["total"]
        if total == 0:
            continue
        share = stats["cache_read"] / total
        label = get_cache_label(share)
        print(f"  Model: {m:<30} | Share: {share*100:5.1f}% | Label: {label:<20}")
        if stats["input"] > 250000 or share < 0.85:
            warn_reasons = []
            if stats["input"] > 250000:
                warn_reasons.append(f"input_tokens > 250,000 ({fmt(stats['input'])})")
            if share < 0.85:
                warn_reasons.append(f"cache_share < 85% ({share*100:.1f}%)")
            print(f"    [WARNING] {', '.join(warn_reasons)}")
            
    # 2. By Session (Top 10)
    print("\n--- Top 10 Sessions Cache Health ---")
    session_stats = defaultdict(lambda: {"input": 0, "total": 0, "cache_read": 0, "source": ""})
    for inv in invocations:
        s = inv["session_id"]
        total = inv["input_tokens"] + inv["output_tokens"] + inv["cache_creation"] + inv["cache_read"] + inv["reasoning"]
        session_stats[s]["input"] += inv["input_tokens"]
        session_stats[s]["total"] += total
        session_stats[s]["cache_read"] += inv["cache_read"]
        session_stats[s]["source"] = inv["source"]
        
    sorted_sessions = sorted(session_stats.items(), key=lambda x: -x[1]["total"])[:10]
    for s, stats in sorted_sessions:
        total = stats["total"]
        if total == 0:
            continue
        share = stats["cache_read"] / total
        label = get_cache_label(share)
        source = stats["source"]
        print(f"  Session: {s:<30} ({source:<15}) | Share: {share*100:5.1f}% | Label: {label:<20}")
        if stats["input"] > 250000 or share < 0.85:
            warn_reasons = []
            if stats["input"] > 250000:
                warn_reasons.append(f"input_tokens > 250,000 ({fmt(stats['input'])})")
            if share < 0.85:
                warn_reasons.append(f"cache_share < 85% ({share*100:.1f}%)")
            print(f"    [WARNING] {', '.join(warn_reasons)}")
    print()


def get_model_bucket(model_name):
    m_clean = model_name.replace(" (estimated)", "").strip().lower()
    
    if "opus" in m_clean:
        if "thinking" in m_clean:
            return "premium_reasoning"
        return "premium_cloud"
    elif "thinking" in m_clean:
        return "premium_reasoning"
    elif "pro" in m_clean:
        return "standard_cloud"
    elif "flash-high" in m_clean:
        return "standard_cloud"
    elif "flash-low" in m_clean:
        return "cheap_cloud"
    elif "flash" in m_clean or "haiku" in m_clean or "lite" in m_clean:
        return "cheap_cloud"
    elif "gpt-5.5" in m_clean or "gpt-5.4" in m_clean:
        if "mini" in m_clean:
            return "cheap_cloud"
        return "premium_cloud"
    elif "gpt-oss" in m_clean:
        return "cheap_or_local"
    elif "gemma" in m_clean or "local" in m_clean or "ollama" in m_clean:
        return "local_free"
    
    inp, out, cr, src = price_for(model_name)
    if inp >= 5.0:
        return "premium_cloud"
    elif inp >= 1.5:
        return "standard_cloud"
    elif inp > 0.0:
        return "cheap_cloud"
    else:
        return "local_free"


def print_model_class_buckets(invocations):
    buckets = {
        "Premium cloud usage": {"tokens": 0, "cost": 0.0},
        "Standard cloud usage": {"tokens": 0, "cost": 0.0},
        "Cheap cloud usage": {"tokens": 0, "cost": 0.0},
        "Local/free usage": {"tokens": 0, "cost": 0.0}
    }
    
    for inv in invocations:
        m = inv["model"]
        total = inv["input_tokens"] + inv["output_tokens"] + inv["cache_creation"] + inv["cache_read"] + inv["reasoning"]
        cost, _ = model_bill(m, {
            "input_tokens": inv["input_tokens"],
            "cache_creation_input_tokens": inv["cache_creation"],
            "cache_read_input_tokens": inv["cache_read"],
            "output_tokens": inv["output_tokens"],
            "reasoning_tokens": inv["reasoning"]
        })
        
        cls = get_model_bucket(m)
        if cls in ("premium_cloud", "premium_reasoning"):
            b = "Premium cloud usage"
        elif cls == "standard_cloud":
            b = "Standard cloud usage"
        elif cls in ("cheap_cloud", "cheap_or_local"):
            b = "Cheap cloud usage"
        else:
            b = "Local/free usage"
            
        buckets[b]["tokens"] += total
        buckets[b]["cost"] += cost
        
    print("======================================================================")
    print("MODEL CLASS BUCKETS REPORT")
    print("======================================================================")
    for name, data in buckets.items():
        print(f"{name:<25} total {fmt(data['tokens']):>16} tokens   ${data['cost']:,.2f}")
    print()


def print_top_hogs(invocations):
    sorted_inv = sorted(invocations, key=lambda x: -x.get("input_tokens", 0))[:20]
    
    print("======================================================================")
    print("Top 20 Token Hogs")
    print("======================================================================")
    for idx, inv in enumerate(sorted_inv, 1):
        ts = inv.get("timestamp")
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "unknown"
        source = inv.get("source", "unknown")
        model = inv.get("model", "unknown")
        exact_str = "exact" if inv.get("exact", True) else "estimated"
        it = inv.get("input_tokens", 0)
        ot = inv.get("output_tokens", 0)
        cr = inv.get("cache_read", 0)
        tot = it + ot + inv.get("cache_creation", 0) + cr + inv.get("reasoning", 0)
        sess = inv.get("session_id", "unknown")
        
        print(f"{idx:2d}. [{ts_str}] {source} | {model} | {exact_str}")
        print(f"    input: {fmt(it):<12} | output: {fmt(ot):<12} | cache-read: {fmt(cr):<12} | total: {fmt(tot)}")
        print(f"    session/transcript path: {sess}")
    print()


def route_recommendations(invocations):
    """Findings derived from the window's own invocations.

    This used to print four fixed strings — including "investigate the
    2026-06-16 Antigravity spike", a date hardcoded into every report forever.
    Advice that does not read its input is decoration: it survives the problem
    being fixed and it fires when there is nothing wrong. Every line below
    carries the number that produced it, so it can be checked and it goes away
    on its own once the condition does.

    Returns a list of strings; empty means nothing crossed a threshold, which
    is a real answer and is printed as one.
    """
    if not invocations:
        return []

    out = []

    # --- spend concentration -------------------------------------------------
    # Worth saying only when one model dominates: that is the lever.
    cost_by_model = defaultdict(float)
    for inv in invocations:
        bucket = {
            "input_tokens": inv.get("input_tokens", 0),
            "output_tokens": inv.get("output_tokens", 0),
            "cache_creation_input_tokens": inv.get("cache_creation", 0),
            "cache_read_input_tokens": inv.get("cache_read", 0),
            "reasoning_tokens": inv.get("reasoning", 0),
        }
        cost, _ = model_bill(inv.get("model", ""), bucket)
        cost_by_model[inv.get("model") or "unknown"] += cost
    total_cost = sum(cost_by_model.values())
    # Two guards, both learned from the first draft flagging a $0.53 window:
    # with only one model in play "100% of spend" is arithmetic, not a finding —
    # there is no alternative lane to name. And below a dollar, no routing
    # change is worth the reader's attention.
    if total_cost >= 1.00 and len(cost_by_model) > 1:
        model, cost = max(cost_by_model.items(), key=lambda kv: kv[1])
        share = cost / total_cost
        if share >= 0.60:
            out.append(f"{model} is {share:.0%} of the window's API-equivalent cost "
                       f"(${cost:,.2f} of ${total_cost:,.2f}) — the only routing "
                       f"change that moves the number is moving work off it.")

    # --- cache health, per lane ---------------------------------------------
    # Cache-reads bill at ~10% of input, so a lane paying full input rate on
    # context it already sent is the most expensive habit available.
    per_source = defaultdict(lambda: defaultdict(int))
    for inv in invocations:
        s = per_source[inv.get("source", "unknown")]
        s["input"] += inv.get("input_tokens", 0)
        s["cache_read"] += inv.get("cache_read", 0)
        s["cache_creation"] += inv.get("cache_creation", 0)
        s["calls"] += 1
        if not inv.get("exact", True):
            s["estimated_calls"] += 1

    for source, s in sorted(per_source.items()):
        context = s["input"] + s["cache_read"] + s["cache_creation"]
        if context < 100_000:
            continue  # too little traffic for the ratio to mean anything
        hit = s["cache_read"] / context
        if hit < 0.50:
            out.append(f"{source}: only {hit:.0%} of context tokens were cache "
                       f"reads ({fmt(s['cache_read'])} of {fmt(context)}) — that "
                       f"lane is re-sending context at full input price.")
        if s["cache_read"] and s["cache_creation"] > s["cache_read"]:
            out.append(f"{source}: wrote more cache than it read "
                       f"({fmt(s['cache_creation'])} created vs "
                       f"{fmt(s['cache_read'])} read) — caches are expiring "
                       f"before they pay for themselves.")

    # --- confidence ----------------------------------------------------------
    for source, s in sorted(per_source.items()):
        if s["calls"] and s["estimated_calls"] / s["calls"] > 0.50:
            out.append(f"{source}: {s['estimated_calls']} of {s['calls']} calls "
                       f"are ESTIMATED, not counted — treat its share of this "
                       f"report as an order of magnitude, not a figure.")

    # --- day spikes ----------------------------------------------------------
    # The replacement for the hardcoded date: same idea, computed.
    by_day_source = defaultdict(int)
    for inv in invocations:
        ts = inv.get("timestamp")
        if not ts:
            continue
        key = (inv.get("source", "unknown"), ts.strftime("%Y-%m-%d"))
        by_day_source[key] += (inv.get("input_tokens", 0)
                               + inv.get("cache_creation", 0))
    per_lane = defaultdict(list)
    for (source, day), total in by_day_source.items():
        per_lane[source].append((day, total))
    for source, days in sorted(per_lane.items()):
        if len(days) < 3:
            continue  # a median of two points flags noise as a spike
        totals = sorted(t for _, t in days)
        median = totals[len(totals) // 2]
        if median <= 0:
            continue
        for day, total in sorted(days):
            if total >= 3 * median and total > 50_000:
                out.append(f"{source} {day}: {fmt(total)} fresh context tokens "
                           f"(input + cache writes, the part cache reads do not "
                           f"cover), {total / median:.1f}x the lane's median day "
                           f"— worth knowing what ran.")
    return out


def print_route_recommendations(invocations):
    findings = route_recommendations(invocations)
    print("======================================================================")
    print("Findings from this window:")
    print("======================================================================")
    if not findings:
        print("- Nothing crossed a threshold. No concentrated spend, no cold-context")
        print("  lane, no day out of line with its own median.")
    for line in findings:
        print(f"- {line}")
    print("======================================================================")


# --- Main Reporting ---
def run_cli_report():
    print("\n======================================================================")
    if RANGE_START or RANGE_END:
        print(f"Token usage monitor — range {CUTOFF:%Y-%m-%d} -> {LIMIT_UPPER - timedelta(days=1):%Y-%m-%d}")
        print(f"window: {CUTOFF:%Y-%m-%d %H:%M} -> {LIMIT_UPPER:%Y-%m-%d %H:%M} {NOW:%Z}")
    elif MONTH:
        print(f"Token usage monitor — Month: {MONTH}")
        print(f"window: {CUTOFF:%Y-%m-%d %H:%M} -> {LIMIT_UPPER:%Y-%m-%d %H:%M} {NOW:%Z}")
    elif COMPARE_N:
        print(f"Token usage monitor — compare last {COMPARE_N}d vs prior {COMPARE_N}d (collecting {DAYS} days)")
        print(f"window: {CUTOFF:%Y-%m-%d %H:%M} -> {NOW:%Y-%m-%d %H:%M} {NOW:%Z}")
    else:
        print(f"Token usage monitor — last {DAYS} day(s)")
        print(f"window: {CUTOFF:%Y-%m-%d %H:%M} -> {NOW:%Y-%m-%d %H:%M} {NOW:%Z}")
    print("======================================================================\n")

    # 1. Claude Code report
    c_day, c_model, c_totals, c_files, c_records = parse_claude()
    print("--- Claude Code ---")
    print(f"Transcripts scanned: {c_files}   Usage records: {c_records}\n")
    hdr = f"{'Day':<12}{'input':>14}{'output':>14}{'cache-read':>16}{'reasoning':>14}"
    print(hdr)
    print("-" * len(hdr))
    for day in sorted(c_day):
        i, o, cc, cr, rt = cols(c_day[day])
        print(f"{day:<12}{fmt(i):>14}{fmt(o):>14}{fmt(cr):>16}{fmt(rt):>14}")
    print("-" * len(hdr))
    ti, to, tcc, tcr, trt = cols(c_totals)
    print(f"{'TOTAL':<12}{fmt(ti):>14}{fmt(to):>14}{fmt(tcr):>16}{fmt(trt):>14}\n")

    # 2. Antigravity report
    a_day, a_model, a_totals, a_files, a_records, active_cnt, rpc_cnt, est_cnt = parse_antigravity()
    print("--- Google Antigravity (Hybrid RPC & Estimation) [! partial - Antigravity retention starts ~2026-05-19; pre-May encrypted] ---")
    print(f"Sessions scanned: {a_files} ({active_cnt} active via RPC + {a_files - active_cnt} fallback from disk)")
    print(f"Invocations tracked: {a_records} ({rpc_cnt} exact via RPC + {est_cnt} estimated fallback)\n")
    hdr_ag = f"{'Day':<12}{'input':>14}{'output':>14}{'cache-read':>16}{'reasoning':>14}"
    print(hdr_ag)
    print("-" * len(hdr_ag))
    for day in sorted(a_day):
        i, o, cc, cr, rt = cols(a_day[day])
        print(f"{day:<12}{fmt(i):>14}{fmt(o):>14}{fmt(cr):>16}{fmt(rt):>14}")
    print("-" * len(hdr_ag))
    tai, tao, tacc, tacr, tart = cols(a_totals)
    print(f"{'TOTAL':<12}{fmt(tai):>14}{fmt(tao):>14}{fmt(tacr):>16}{fmt(tart):>14}\n")
    compute_and_print_split([inv for inv in ALL_INVOCATIONS if inv["source"].startswith("Antigravity")], "Antigravity")

    # 3. Codex report
    cx_day, cx_model, cx_totals, cx_files, cx_records = parse_codex()
    print("--- OpenAI Codex (Granular Rollout Parsing) ---")
    print(f"Sessions scanned: {cx_files}   Usage events: {cx_records}\n")
    hdr_cx = f"{'Day':<12}{'input':>14}{'output':>14}{'cache-read':>16}{'reasoning':>14}"
    print(hdr_cx)
    print("-" * len(hdr_cx))
    for day in sorted(cx_day):
        i, o, cc, cr, rt = cols(cx_day[day])
        print(f"{day:<12}{fmt(i):>14}{fmt(o):>14}{fmt(cr):>16}{fmt(rt):>14}")
    print("-" * len(hdr_cx))
    txi, txo, txcc, txcr, txrt = cols(cx_totals)
    print(f"{'TOTAL':<12}{fmt(txi):>14}{fmt(txo):>14}{fmt(txcr):>16}{fmt(txrt):>14}")
    # Cross-check our rollout parse against Codex's OWN native counter. Compare like
    # with like: Codex's tokens_used excludes cache-reads, so validate against our
    # non-cache billable tokens (input w/CC + output + reasoning), and note cache
    # separately rather than inflating the delta.
    cx_nthreads, cx_native = codex_native_total()
    cx_billable = txi + txo + txrt
    if cx_native:
        delta = (cx_billable - cx_native) / cx_native * 100
        print(f"  cross-check: Codex native (threads.tokens_used) {fmt(cx_native)} across {cx_nthreads} threads "
              f"| our billable (in+out+reasoning, no cache) {fmt(cx_billable)} | delta {delta:+.0f}% "
              f"(+{fmt(txcr)} cache-read tracked separately)")
    print()

    # 3b. Gemini CLI report (Hybrid Exact & Estimation; pre-May fills here)
    gc_day, gc_model, gc_totals, gc_files, gc_records, gc_exact, gc_est = parse_gemini_cli()
    print("--- Gemini CLI (Hybrid Exact & Estimation) ---")
    print(f"Sessions scanned: {gc_files}   Invocations tracked: {gc_records} ({gc_exact} exact + {gc_est} estimated)\n")
    hdr_gc = f"{'Day':<12}{'input':>14}{'output':>14}{'cache-read':>16}{'reasoning':>14}"
    print(hdr_gc)
    print("-" * len(hdr_gc))
    for day in sorted(gc_day):
        i, o, cc, cr, rt = cols(gc_day[day])
        print(f"{day:<12}{fmt(i):>14}{fmt(o):>14}{fmt(cr):>16}{fmt(rt):>14}")
    print("-" * len(hdr_gc))
    tgi, tgo, tgcc, tgcr, tgrt = cols(gc_totals)
    print(f"{'TOTAL':<12}{fmt(tgi):>14}{fmt(tgo):>14}{fmt(tgcr):>16}{fmt(tgrt):>14}\n")
    compute_and_print_split([inv for inv in ALL_INVOCATIONS if inv["source"] == "Gemini CLI"], "Gemini CLI")

    # 3d. Fleet Usage Ledger (forward, exact: local Ollama/Gemma, OpenRouter, HF)
    fl_day, fl_model, fl_totals, fl_records = parse_fleet_usage()
    print("--- Fleet Usage Ledger (Local Ollama/Gemma + OpenRouter + HF) ---")
    print(f"Invocations tracked: {fl_records} (exact, from each lane's API response)\n")
    hdr_fl = f"{'Day':<12}{'input':>14}{'output':>14}{'cache-read':>16}{'reasoning':>14}"
    print(hdr_fl)
    print("-" * len(hdr_fl))
    for day in sorted(fl_day):
        i, o, cc, cr, rt = cols(fl_day[day])
        print(f"{day:<12}{fmt(i):>14}{fmt(o):>14}{fmt(cr):>16}{fmt(rt):>14}")
    print("-" * len(hdr_fl))
    tfi, tfo, tfcc, tfcr, tfrt = cols(fl_totals)
    print(f"{'TOTAL':<12}{fmt(tfi):>14}{fmt(tfo):>14}{fmt(tfcr):>16}{fmt(tfrt):>14}\n")

    # 4. Model Breakdown
    all_models = defaultdict(lambda: defaultdict(int))
    for m, d in c_model.items():
        for k, v in d.items():
            all_models[m][k] += v
    for m, d in a_model.items():
        for k, v in d.items():
            all_models[m][k] += v
    for m, d in cx_model.items():
        for k, v in d.items():
            all_models[m][k] += v
    for m, d in gc_model.items():
        for k, v in d.items():
            all_models[m][k] += v
    for m, d in fl_model.items():
        for k, v in d.items():
            all_models[m][k] += v

    # Robustness: normalize any None/empty model name across every record so the
    # cross-source aggregates (model breakdown, blended split, cache health, top
    # hogs, route recs) can sort/price model keys without crashing on None.
    for _inv in ALL_INVOCATIONS:
        if not _inv.get("model"):
            _inv["model"] = "(unknown)"

    # Same guard for the pre-aggregated model breakdown dict.
    if None in all_models:
        _none_d = all_models.pop(None)
        for _k, _v in _none_d.items():
            all_models["(unknown)"][_k] += _v

    if all_models:
        print("--- By Model Breakdown ---")
        mw = max(len(str(m)) for m in all_models)
        grand_total_tokens = 0
        grand_total_cost = 0.0
        grand_total_cold = 0.0
        total_cache_reads = 0
        total_fresh_input = 0
        total_output = 0
        used_estimate = False
        for model in sorted(all_models, key=lambda m: -sum(all_models[m].values())):
            i, o, cc, cr, rt = cols(all_models[model])
            total = i + o + cc + cr + rt
            grand_total_tokens += total
            total_cache_reads += cr
            total_fresh_input += i + cc
            total_output += o + rt
            cost, src = model_bill(model, all_models[model])
            grand_total_cost += cost
            # Cold-boot: every input-side token (input + cache-creation + cache-read)
            # charged at full fresh-input rate; reasoning billed as output.
            inp_rate, out_rate, _crr, _s = price_for(model)
            cold_cost = calculate_cost(
                input_tokens=(i + cc + cr),
                output_tokens=(o + rt),
                inp_rate=inp_rate,
                out_rate=out_rate,
            )
            grand_total_cold += float(cold_cost)
            if src == EST:
                used_estimate = True
            tag = "~" if src == EST else " "
            print(f"  {model:<{mw}}  total {fmt(total):>16}   "
                  f"(in {fmt(i + cc)}, out {fmt(o)}, cache-read {fmt(cr)}, reasoning {fmt(rt)})  {tag}${cost:,.2f}")

        # --- Honest API-equivalent shadow bill --------------------------------------
        # What these tokens WOULD have cost at first-party API list prices, with
        # cache-reads priced as cache-reads (not as fresh input) and reasoning as
        # output. This is a hypothetical reference point, NOT money saved: on flat-
        # rate subscriptions this volume was never going to be bought at API rates,
        # so there is no "arbitrage" sum being pocketed. Naive (all-tokens x flat-
        # rate) math overstates this several-fold because cache-reads dominate the
        # token count but bill at ~10% of input.
        #
        # Set AI_MONTHLY_SUBSCRIPTION_USD to print your real spend alongside it.
        import subscriptions as _subs
        sub_cost, _sub_source = _subs.monthly_total()
        _sub_breakdown = _subs.monthly_breakdown()

        # AI_MONTHLY_SUBSCRIPTION_USD is a MONTHLY figure. Every other number in
        # this block is scoped to the window that was asked for, so printing the
        # month's total next to them read as the window's spend: a 20-hour
        # window and a six-day window both printed the same $108.88. It also
        # made "Absorbed" (cold - paid) subtract a full month of subscription
        # from a fraction of a day of cold-boot, which is not an accounting
        # invariant, it is a category error.
        #
        # Pro-rate to the window actually reported. The window ends at NOW for
        # an open-ended run and at LIMIT_UPPER for an explicit --end, whichever
        # comes first.
        _win_end = min(NOW, LIMIT_UPPER)
        _win_days = max(0.0, (_win_end - CUTOFF).total_seconds() / 86400.0)
        sub_window_cost = _subs.prorate(sub_cost, _win_days)

        cache_share = (total_cache_reads / grand_total_tokens * 100) if grand_total_tokens else 0

        # Exact vs estimated accounting from ALL_INVOCATIONS
        exact_tok = sum(
            int(inv.get("input_tokens", 0)) + int(inv.get("output_tokens", 0)) +
            int(inv.get("cache_creation", 0)) + int(inv.get("cache_read", 0)) + int(inv.get("reasoning", 0))
            for inv in ALL_INVOCATIONS if inv.get("exact", True)
        )
        est_tok = sum(
            int(inv.get("input_tokens", 0)) + int(inv.get("output_tokens", 0)) +
            int(inv.get("cache_creation", 0)) + int(inv.get("cache_read", 0)) + int(inv.get("reasoning", 0))
            for inv in ALL_INVOCATIONS if not inv.get("exact", True)
        )
        blended_tokens = grand_total_tokens or (exact_tok + est_tok)
        exact_pct = (exact_tok / blended_tokens * 100) if blended_tokens else 100.0
        est_pct = (est_tok / blended_tokens * 100) if blended_tokens else 0.0
        is_estimated_run = used_estimate or (est_tok > 0)
        est_tag = " ~" if is_estimated_run else ""

        # Enforce accounting invariants via Decimal math
        paid_val = max(0.0, sub_window_cost)
        cached_realistic_val = max(0.0, grand_total_cost)
        cold_val = max(cached_realistic_val, grand_total_cold)
        cold_dec = round_to_cents(Decimal(str(cold_val)))
        paid_dec = round_to_cents(Decimal(str(paid_val)))
        absorbed_dec = cold_dec - paid_dec
        if absorbed_dec < Decimal("0.00"):
            import logging
            logging.getLogger("token_tracker").warning(
                "Accounting inconsistency: cold (%s) < paid (%s); clamping absorbed to 0.0",
                cold_val, paid_val
            )
            absorbed_dec = Decimal("0.00")
        absorbed_val = float(round_to_cents(absorbed_dec))

        print("\n======================================================================")
        print("WORK MOVED THROUGH THIS WINDOW")
        print("======================================================================")
        # The two numbers that state the scale lead, at full precision. Everything
        # that shrinks the headline (the cached price, the subscription) is a
        # footnote below the rule. This report exists to show how much work ran,
        # not how little it cost; leading with the small number inverts that into
        # a frugality brag, and stacking disclaimers around the big one reads as
        # an apology for the scale.
        print(f"  TOTAL TOKENS        {fmt(grand_total_tokens):>22}{est_tag}")
        print(f"  COLD API COST       {chr(36) + format(cold_val, ',.2f'):>22}{est_tag}")
        print("                       every token billed fresh at list price: input +")
        print("                       cache-write + cache-read at the full input rate,")
        print("                       output + reasoning at the output rate.")
        print("")
        print(f"  cache reads         {fmt(total_cache_reads):>22}   {cache_share:.1f}% of all tokens")
        print(f"  fresh input         {fmt(total_fresh_input):>22}")
        print(f"  output              {fmt(total_output):>22}")
        print(f"  measured / inferred {exact_pct:>13.1f}% / {est_pct:.1f}%")
        print("----------------------------------------------------------------------")
        print(f"  cached API price    ${cached_realistic_val:,.2f}      absorbed ${absorbed_val:,.2f}{est_tag}")
        if sub_cost:
            _basis = f"${sub_cost:,.2f}/mo over {_win_days:,.1f}d"
            if _sub_breakdown:
                _basis += " = " + ", ".join(f"{k} ${v:,.2f}" for k, v in sorted(_sub_breakdown.items()))
            print(f"  subscription        ${paid_val:,.2f}      ({_basis})")
        if is_estimated_run:
            print("  ~ = includes estimated tokens or models priced from estimates")
        print("  COLD is a scale gauge at list prices, not an invoice.")
        print("======================================================================\n")
    
        compute_and_print_split(ALL_INVOCATIONS, "Blended Report")
        print_cache_health_report(ALL_INVOCATIONS)
        print_model_class_buckets(ALL_INVOCATIONS)
        print_top_hogs(ALL_INVOCATIONS)
        print_route_recommendations(ALL_INVOCATIONS)
    
        if BY_PROVIDER:
            # Group invocations by provider/company
            provider_stats = defaultdict(lambda: {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation": 0,
                "cache_read": 0,
                "reasoning": 0,
                "cost": 0.0,
                "count": 0
            })
            for inv in ALL_INVOCATIONS:
                source = inv["source"]
                if "Claude Code" in source:
                    company = "Anthropic (Claude Code)"
                elif source.startswith("Fleet:"):
                    prov = source.split(":", 1)[1]
                    if "Ollama" in prov:
                        company = "Local (Ollama/Gemma)"
                    elif "OpenRouter" in prov:
                        company = "OpenRouter (free)"
                    elif "HF" in prov or "HuggingFace" in prov:
                        company = "HuggingFace (free)"
                    else:
                        company = f"Fleet ({prov})"
                elif "Gemini CLI" in source:
                    company = "Google (Gemini CLI)"
                elif "Antigravity" in source:
                    company = "Google (Antigravity)"
                elif "Codex" in source or "OpenAI" in source:
                    company = "OpenAI (Codex)"
                else:
                    company = "Unknown"
                
                it = inv.get("input_tokens", 0)
                ot = inv.get("output_tokens", 0)
                cc = inv.get("cache_creation", 0)
                cr = inv.get("cache_read", 0)
                rt = inv.get("reasoning", 0)
            
                d = {
                    "input_tokens": it,
                    "cache_creation_input_tokens": cc,
                    "cache_read_input_tokens": cr,
                    "output_tokens": ot,
                    "reasoning_tokens": rt
                }
                cost, _ = model_bill(inv["model"], d)
            
                c_stats = provider_stats[company]
                c_stats["input_tokens"] += it
                c_stats["output_tokens"] += ot
                c_stats["cache_creation"] += cc
                c_stats["cache_read"] += cr
                c_stats["reasoning"] += rt
                c_stats["cost"] += cost
                c_stats["count"] += 1
            
            print("======================================================================")
            print("BY-PROVIDER SUMMARY REPORT")
            print("======================================================================")
            hdr_prov = f"{'Provider/Company':<30}{'Invocations':>12}{'Input (w/CC)':>16}{'Output':>14}{'Cache Read':>16}{'Reasoning':>14}{'Cost':>12}"
            print(hdr_prov)
            print("-" * len(hdr_prov))
        
            total_inv = 0
            total_in = 0
            total_out = 0
            total_cr = 0
            total_rt = 0
            total_cost = 0.0
        
            _ordered = ["Anthropic (Claude Code)", "Google (Antigravity)", "Google (Gemini CLI)",
                        "OpenAI (Codex)", "Local (Ollama/Gemma)", "OpenRouter (free)", "HuggingFace (free)"]
            _provider_order = _ordered + [c for c in provider_stats if c not in _ordered]
            for company in _provider_order:
                if company not in provider_stats:
                    continue
                c_stats = provider_stats[company]
                inv_cnt = c_stats["count"]
                it = c_stats["input_tokens"] + c_stats["cache_creation"]
                ot = c_stats["output_tokens"]
                cr = c_stats["cache_read"]
                rt = c_stats["reasoning"]
                cost = c_stats["cost"]
            
                total_inv += inv_cnt
                total_in += it
                total_out += ot
                total_cr += cr
                total_rt += rt
                total_cost += cost
            
                print(f"{company:<30}{fmt(inv_cnt):>12}{fmt(it):>16}{fmt(ot):>14}{fmt(cr):>16}{fmt(rt):>14}  ${cost:10.2f}")
            
            print("-" * len(hdr_prov))
            print(f"{'TOTAL':<30}{fmt(total_inv):>12}{fmt(total_in):>16}{fmt(total_out):>14}{fmt(total_cr):>16}{fmt(total_rt):>14}  ${total_cost:10.2f}")
            print("======================================================================\n")

    print()

if __name__ == "__main__":
    run_cli_report()



# =============================================================================
# C4AI Token Counter Integration (vendored minimal core)
# Source: https://github.com/C4AI/token-counter
# Attribution: C4AI token-counter (MIT). Folded here so token_tracker.py
# stays a single-file import with no external package required for basic use.
# The C4AI token-counter counts tokenizer tokens in local files or HuggingFace
# datasets, producing distribution stats (total, mean, median, IQR, P95, P99,
# stddev) and optional Markdown/JSON reports. It uses HuggingFace tokenizers
# (default: Qwen/Qwen3-1.7B-Base) and supports Parquet/JSONL inputs.
# =============================================================================

import bisect
import math
import time as _time
from dataclasses import dataclass, field as _field
from typing import Any, Dict, Iterable, Iterator, List, Optional


# ---------------------------------------------------------------------------
# TokenCountStats â€” ported minimal subset from token_counter.reporting
# ---------------------------------------------------------------------------

@dataclass
class TokenCountStats:
    """Accumulates per-document token counts and derives distribution stats.

    Mirrors the public contract of C4AI token_counter.reporting.TokenCountStats
    so that callers can swap in the full library when available.
    """
    total_tokens: int = 0
    documents_processed: int = 0
    rows_seen: int = 0
    null_field_rows: int = 0
    empty_text_rows: int = 0
    non_string_rows_coerced: int = 0
    # Distribution tracking (sorted list for percentile computation)
    _lengths: List[int] = _field(default_factory=list, repr=False)
    started_at_epoch: Optional[float] = None
    completed_at_epoch: Optional[float] = None
    wall_time: float = 0.0

    def observe_document(self, *, text: str, token_length: int) -> None:
        self.total_tokens += token_length
        self.documents_processed += 1
        bisect.insort(self._lengths, token_length)

    @property
    def mean_tokens(self) -> Optional[float]:
        if not self._lengths:
            return None
        return self.total_tokens / len(self._lengths)

    def _percentile(self, p: float) -> Optional[float]:
        n = len(self._lengths)
        if n == 0:
            return None
        idx = p * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        frac = idx - lo
        return self._lengths[lo] + frac * (self._lengths[hi] - self._lengths[lo])

    @property
    def median_tokens(self) -> Optional[float]:
        return self._percentile(0.50)

    @property
    def p95_tokens(self) -> Optional[float]:
        return self._percentile(0.95)

    @property
    def p99_tokens(self) -> Optional[float]:
        return self._percentile(0.99)

    @property
    def stddev_tokens(self) -> Optional[float]:
        n = len(self._lengths)
        if n < 2:
            return None
        mu = self.total_tokens / n
        return math.sqrt(sum((x - mu) ** 2 for x in self._lengths) / (n - 1))

    @property
    def iqr_tokens(self) -> Optional[float]:
        p25 = self._percentile(0.25)
        p75 = self._percentile(0.75)
        if p25 is None or p75 is None:
            return None
        return p75 - p25

    def distribution_summary(self) -> Dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "documents_processed": self.documents_processed,
            "mean": self.mean_tokens,
            "median": self.median_tokens,
            "iqr": self.iqr_tokens,
            "p95": self.p95_tokens,
            "p99": self.p99_tokens,
            "stddev": self.stddev_tokens,
        }

    def to_checkpoint_state(self) -> Dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "documents_processed": self.documents_processed,
            "rows_seen": self.rows_seen,
            "null_field_rows": self.null_field_rows,
            "empty_text_rows": self.empty_text_rows,
            "non_string_rows_coerced": self.non_string_rows_coerced,
            "lengths": self._lengths,
            "started_at_epoch": self.started_at_epoch,
            "completed_at_epoch": self.completed_at_epoch,
            "wall_time": self.wall_time,
        }

    @classmethod
    def from_checkpoint_state(cls, state: Dict[str, Any]) -> "TokenCountStats":
        obj = cls()
        obj.total_tokens = state.get("total_tokens", 0)
        obj.documents_processed = state.get("documents_processed", 0)
        obj.rows_seen = state.get("rows_seen", 0)
        obj.null_field_rows = state.get("null_field_rows", 0)
        obj.empty_text_rows = state.get("empty_text_rows", 0)
        obj.non_string_rows_coerced = state.get("non_string_rows_coerced", 0)
        obj._lengths = list(state.get("lengths", []))
        obj.started_at_epoch = state.get("started_at_epoch")
        obj.completed_at_epoch = state.get("completed_at_epoch")
        obj.wall_time = state.get("wall_time", 0.0)
        return obj


# ---------------------------------------------------------------------------
# Core counting helpers â€” ported from token_counter.cli
# ---------------------------------------------------------------------------

DEFAULT_TC_MODEL = "Qwen/Qwen3-1.7B-Base"
DEFAULT_TC_BATCH_SIZE = 256


def _tc_token_lengths(tokenizer: Any, texts: List[str]) -> List[int]:
    """Return per-text token lengths using the given HuggingFace tokenizer."""
    if not texts:
        return []
    try:
        encoded = tokenizer(texts, add_special_tokens=False, return_attention_mask=False)
        input_ids = encoded["input_ids"] if isinstance(encoded, dict) else encoded.input_ids
        return [len(ids) for ids in input_ids]
    except Exception:
        return [len(tokenizer.encode(t, add_special_tokens=False)) for t in texts]


def _tc_load_tokenizer(model: str, *, trust_remote_code: bool = False) -> Any:
    """Load a HuggingFace AutoTokenizer. Raises ImportError if transformers absent."""
    from transformers import AutoTokenizer  # type: ignore
    return AutoTokenizer.from_pretrained(model, trust_remote_code=trust_remote_code)


def count_tokens_in_texts(
    texts: Iterable[str],
    *,
    model: str = DEFAULT_TC_MODEL,
    batch_size: int = DEFAULT_TC_BATCH_SIZE,
    trust_remote_code: bool = False,
    tokenizer: Any = None,
) -> TokenCountStats:
    """Count tokenizer tokens across an iterable of strings.

    This is the primary programmatic entry-point folded in from C4AI
    token-counter. It accepts any iterable of strings â€” files, dataset rows,
    in-memory lists â€” and returns a TokenCountStats with full distribution stats.

    Parameters
    ----------
    texts:
        Iterable of raw text strings to tokenize.
    model:
        HuggingFace tokenizer model. Default: Qwen/Qwen3-1.7B-Base.
    batch_size:
        Documents per tokenizer batch for throughput.
    trust_remote_code:
        Pass to AutoTokenizer.from_pretrained.
    tokenizer:
        Pre-loaded tokenizer object (skips loading if provided).

    Returns
    -------
    TokenCountStats
        Distribution stats including total_tokens, mean, median, p95, p99.
    """
    if tokenizer is None:
        tokenizer = _tc_load_tokenizer(model, trust_remote_code=trust_remote_code)

    stats = TokenCountStats()
    stats.started_at_epoch = _time.time()
    batch: List[str] = []

    def _flush() -> None:
        if not batch:
            return
        lengths = _tc_token_lengths(tokenizer, batch)
        for text, length in zip(batch, lengths):
            stats.observe_document(text=text, token_length=length)
        batch.clear()

    for raw in texts:
        stats.rows_seen += 1
        if raw is None:
            stats.null_field_rows += 1
            continue
        if not isinstance(raw, str):
            stats.non_string_rows_coerced += 1
            raw = str(raw)
        if raw == "":
            stats.empty_text_rows += 1
        batch.append(raw)
        if len(batch) >= batch_size:
            _flush()

    _flush()
    stats.completed_at_epoch = _time.time()
    stats.wall_time = max(0.0, stats.completed_at_epoch - stats.started_at_epoch)
    return stats


def count_tokens_in_file(
    path: str,
    *,
    field: str = "text",
    file_fmt: str = "jsonl",
    model: str = DEFAULT_TC_MODEL,
    batch_size: int = DEFAULT_TC_BATCH_SIZE,
    max_docs: Optional[int] = None,
    trust_remote_code: bool = False,
    tokenizer: Any = None,
) -> TokenCountStats:
    """Count tokens in a local JSONL, plain-text, or Parquet file.

    For JSONL: each line is parsed as JSON and the ``field`` key is extracted.
    For plain-text (file_fmt='text'): each line is treated as one document.
    For Parquet (file_fmt='parquet'): requires pandas + pyarrow.

    Parameters
    ----------
    path:
        Absolute or relative path to the input file.
    field:
        Key to extract from each JSON object (JSONL/Parquet only).
    file_fmt:
        'jsonl', 'text', or 'parquet'.
    model:
        HuggingFace tokenizer model.
    batch_size:
        Documents per tokenizer batch.
    max_docs:
        Stop after processing this many documents.
    trust_remote_code:
        Passed to the tokenizer loader.
    tokenizer:
        Pre-loaded tokenizer to reuse across calls.

    Returns
    -------
    TokenCountStats
        Populated distribution stats object.
    """
    import pathlib as _pathlib
    p = _pathlib.Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    def _iter() -> Iterator[str]:
        count = 0
        if file_fmt == "jsonl":
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        val = row.get(field, "")
                    except json.JSONDecodeError:
                        val = line
                    yield val if isinstance(val, str) else str(val)
                    count += 1
                    if max_docs is not None and count >= max_docs:
                        return
        elif file_fmt == "text":
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    yield line.rstrip("\n")
                    count += 1
                    if max_docs is not None and count >= max_docs:
                        return
        elif file_fmt == "parquet":
            try:
                import pandas as pd  # type: ignore
            except ImportError:
                raise ImportError("pandas required for parquet support: pip install pandas pyarrow")
            df = pd.read_parquet(path, columns=[field])
            for val in df[field]:
                yield val if isinstance(val, str) else str(val)
                count += 1
                if max_docs is not None and count >= max_docs:
                    return
        else:
            raise ValueError(f"Unsupported format: {file_fmt}. Use 'jsonl', 'text', or 'parquet'.")

    return count_tokens_in_texts(
        _iter(),
        model=model,
        batch_size=batch_size,
        trust_remote_code=trust_remote_code,
        tokenizer=tokenizer,
    )


def print_token_distribution_report(
    stats: TokenCountStats, *, title: str = "Token Distribution"
) -> None:
    """Print a compact distribution summary to stdout.

    Mirrors the summary block from C4AI token-counter Markdown reports.
    """
    d = stats.distribution_summary()
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  {title}")
    print(sep)
    print(f"  Total tokens      : {d['total_tokens']:,}")
    print(f"  Documents         : {d['documents_processed']:,}")
    if d["mean"] is not None:
        print(f"  Mean              : {d['mean']:,.1f}")
    if d["median"] is not None:
        print(f"  Median            : {d['median']:,.1f}")
    if d["iqr"] is not None:
        print(f"  IQR               : {d['iqr']:,.1f}")
    if d["p95"] is not None:
        print(f"  P95               : {d['p95']:,.1f}")
    if d["p99"] is not None:
        print(f"  P99               : {d['p99']:,.1f}")
    if d["stddev"] is not None:
        print(f"  Std Dev           : {d['stddev']:,.1f}")
    print(sep)


# ---------------------------------------------------------------------------
# __main__ demo â€” runs when invoked as: python token_tracker.py --demo-tc <path>
# Existing default invocation (DAYS arg) is unaffected.
# ---------------------------------------------------------------------------

if __name__ == "__main__" and "--demo-tc" in sys.argv:
    import argparse as _argparse

    _p = _argparse.ArgumentParser(
        description="C4AI token-counter demo (vendored into token_tracker.py)"
    )
    _p.add_argument("--demo-tc", metavar="PATH", help="File to count tokens in")
    _p.add_argument("--field", default="text", help="JSON field to extract (JSONL/Parquet)")
    _p.add_argument(
        "--format", default="jsonl", choices=["jsonl", "text", "parquet"],
        dest="file_fmt", help="Input format"
    )
    _p.add_argument("--model", default=DEFAULT_TC_MODEL, help="HuggingFace tokenizer model")
    _p.add_argument("--max-docs", type=int, default=None, help="Max documents to process")
    _args = _p.parse_args()

    print(f"[token-counter demo] Loading tokenizer: {_args.model}")
    print(f"[token-counter demo] Input: {_args.demo_tc}  format={_args.file_fmt}  field={_args.field}")
    _tc_stats = count_tokens_in_file(
        _args.demo_tc,
        field=_args.field,
        file_fmt=_args.file_fmt,
        model=_args.model,
        max_docs=_args.max_docs,
    )
    print_token_distribution_report(_tc_stats, title=f"Token Distribution -- {_args.demo_tc}")
    print(f"\n[token-counter demo] Wall time: {_tc_stats.wall_time:.2f}s")


# ---------------------------------------------------------------------------
# --compare N : last N days vs the prior N days, per company (additive summary).
# Reads the already-collected ALL_INVOCATIONS; the window was widened to >= 2N days
# at parse time so both periods are present. Runs after the normal report.
# ---------------------------------------------------------------------------
if __name__ == "__main__" and COMPARE_N:
    _n = COMPARE_N
    _mid = NOW - timedelta(days=_n)
    _lo = NOW - timedelta(days=_n * 2)

    def _company(src):
        return (src or "unknown").split(" (")[0].split(":")[0].strip()

    def _toks(inv):
        return (inv.get("input_tokens", 0) + inv.get("output_tokens", 0)
                + inv.get("cache_creation", 0) + inv.get("cache_read", 0)
                + inv.get("reasoning", 0))

    _cur = defaultdict(int)
    _prev = defaultdict(int)
    for _inv in ALL_INVOCATIONS:
        _ts = _inv.get("timestamp")
        if _ts is None:
            continue
        if _ts >= _mid:
            _cur[_company(_inv.get("source"))] += _toks(_inv)
        elif _ts >= _lo:
            _prev[_company(_inv.get("source"))] += _toks(_inv)

    _companies = sorted(set(_cur) | set(_prev), key=lambda c: -(_cur[c] + _prev[c]))
    _sep = "-" * 70
    print()
    print("=" * 70)
    print(f"Period comparison — last {_n}d vs prior {_n}d (total tokens, by company)")
    print(f"  last {_n}d : {_mid:%Y-%m-%d} -> {NOW:%Y-%m-%d}")
    print(f"  prior {_n}d: {_lo:%Y-%m-%d} -> {_mid:%Y-%m-%d}")
    print("=" * 70)
    print(f"{'Company':<22}{'prior ' + str(_n) + 'd':>16}{'last ' + str(_n) + 'd':>16}{'delta':>12}")
    print(_sep)
    _tc = _tp = 0
    for _c in _companies:
        _p = _prev[_c]
        _cu = _cur[_c]
        _tc += _cu
        _tp += _p
        _d = f"{(_cu - _p) / _p * 100:+.0f}%" if _p else ("new" if _cu else "-")
        print(f"{_c:<22}{_p:>16,}{_cu:>16,}{_d:>12}")
    print(_sep)
    _dt = f"{(_tc - _tp) / _tp * 100:+.0f}%" if _tp else "-"
    print(f"{'TOTAL':<22}{_tp:>16,}{_tc:>16,}{_dt:>12}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# --lanes : full analytics dashboard. Period comparisons (24h / week / month /
# quarter / half / year, each "to-date" vs the same elapsed offset into the
# prior period) plus records (highest day/week/month + longest & current
# streak). One pass builds a per-day series; every lane is date math on it.
# Drafted on gemma4:31b-cloud (free lane); boundary math reviewed by Coach.
# ---------------------------------------------------------------------------
def print_analytics_dashboard(invocations, now):
    import datetime as _dtm

    def humanize(n):
        n = int(n)
        if n == 0:
            return "0"
        for unit, val in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
            if abs(n) >= val:
                return f"{n / val:.2f}{unit}"
        return str(n)

    def delta(this, last):
        if last == 0:
            return "new" if this > 0 else "-"
        return f"{(this - last) / last:+.0%}"

    today = now.date()
    day_sums = defaultdict(int)
    records = []
    for inv in invocations:
        ts = inv.get("timestamp")
        if ts is None:
            continue
        toks = sum(inv.get(k, 0) for k in
                   ("input_tokens", "output_tokens", "cache_creation", "cache_read", "reasoning"))
        day_sums[ts.date()] += toks
        records.append((ts, toks))

    if not records:
        print("no data")
        return None

    def dsum(lo, hi):  # inclusive day-range sum over the per-day series
        total = 0
        cur = lo
        while cur <= hi:
            total += day_sums.get(cur, 0)
            cur += _dtm.timedelta(days=1)
        return total

    lanes = []
    # 24h rolling (timestamp precision)
    lanes.append(("this 24h / prior 24h",
                  sum(t for ts, t in records if now - _dtm.timedelta(hours=24) <= ts < now),
                  sum(t for ts, t in records if now - _dtm.timedelta(hours=48) <= ts < now - _dtm.timedelta(hours=24))))
    # week-to-date (Mon start)
    ws = today - _dtm.timedelta(days=today.weekday())
    off = (today - ws).days
    lanes.append(("this week / last week", dsum(ws, today),
                  dsum(ws - _dtm.timedelta(days=7), ws - _dtm.timedelta(days=7 - off))))
    # month-to-date
    ms = today.replace(day=1)
    off = (today - ms).days
    lme = ms - _dtm.timedelta(days=1)
    lms = lme.replace(day=1)
    lanes.append(("this month / last month", dsum(ms, today),
                  dsum(lms, min(lms + _dtm.timedelta(days=off), lme))))
    # quarter-to-date (3mo)
    qm = ((today.month - 1) // 3) * 3 + 1
    qs = _dtm.date(today.year, qm, 1)
    off = (today - qs).days
    pqs = _dtm.date(today.year if qm > 1 else today.year - 1, qm - 3 if qm > 1 else 10, 1)
    lanes.append(("this quarter / last (3mo)", dsum(qs, today),
                  dsum(pqs, min(pqs + _dtm.timedelta(days=off), qs - _dtm.timedelta(days=1)))))
    # half-to-date (6mo)
    hm = 1 if today.month <= 6 else 7
    hs = _dtm.date(today.year, hm, 1)
    off = (today - hs).days
    phs = _dtm.date(today.year if hm > 1 else today.year - 1, hm - 6 if hm > 1 else 7, 1)
    lanes.append(("this half / last (6mo)", dsum(hs, today),
                  dsum(phs, min(phs + _dtm.timedelta(days=off), hs - _dtm.timedelta(days=1)))))
    # year-to-date
    ys = _dtm.date(today.year, 1, 1)
    off = (today - ys).days
    lys = _dtm.date(today.year - 1, 1, 1)
    lanes.append(("this year / last year", dsum(ys, today),
                  dsum(lys, min(lys + _dtm.timedelta(days=off), _dtm.date(today.year - 1, 12, 31)))))

    print(f"{'Lane':<28}{'this':>14}{'prior':>14}{'delta':>9}")
    print("-" * 65)
    for name, t, l in lanes:
        print(f"{name:<28}{humanize(t):>14}{humanize(l):>14}{delta(t, l):>9}")

    print()
    print("Records (full history)")
    print("-" * 65)
    md = max(day_sums, key=day_sums.get)
    print(f"  Highest Day:    {md} ({humanize(day_sums[md])})")
    weeks, months = defaultdict(int), defaultdict(int)
    for d, v in day_sums.items():
        weeks[d.isocalendar()[:2]] += v
        months[(d.year, d.month)] += v
    mw = max(weeks, key=weeks.get)
    print(f"  Highest Week:   {mw[0]}-W{mw[1]:02d} ({humanize(weeks[mw])})")
    mm = max(months, key=months.get)
    print(f"  Highest Month:  {mm[0]}-{mm[1]:02d} ({humanize(months[mm])})")

    dates = sorted(day_sums)
    best = cur = 0
    bstart = bend = tstart = dates[0]
    cd = dates[0]
    while cd <= dates[-1]:
        if day_sums.get(cd, 0) > 0:
            if cur == 0:
                tstart = cd
            cur += 1
            if cur > best:
                best, bstart, bend = cur, tstart, cd
        else:
            cur = 0
        cd += _dtm.timedelta(days=1)
    cstreak = 0
    cd = today if day_sums.get(today, 0) > 0 else today - _dtm.timedelta(days=1)
    while day_sums.get(cd, 0) > 0:
        cstreak += 1
        cd -= _dtm.timedelta(days=1)
    print(f"  Longest Streak: {best} days ({bstart}..{bend})")
    print(f"  Current Streak: {cstreak} days")
    return None


if __name__ == "__main__" and LANES:
    print()
    print("=" * 70)
    print(f"Token usage analytics  (history through {NOW:%Y-%m-%d})")
    print("=" * 70)
    print_analytics_dashboard(ALL_INVOCATIONS, NOW)



# --- Fleet dailies: publish this machine, sum every machine ----------------
#
# Everything above answers "what did this box spend?". These three flags carry
# that answer to the other boxes over git, and fold theirs back in.

def backpedal_validate(_fd, machine=None):
    """Walk this machine's dailies newest-first and validate them.

    Returns a dict of findings. Defects are parse errors, negative totals and
    stale-cohort days; calendar gaps are reported but are NOT defects (an idle
    day exports nothing). The dirty flag warns that any export made now would
    carry a +dirty stamp that can never rank as the current cohort.
    """
    from pathlib import Path as _Path
    import re as _re
    import datetime as _dt

    machine = machine or _fd.resolve_machine()
    base = _Path("dailies") / machine
    files = sorted((p for p in base.glob("*.json")), reverse=True)
    parse_bad, negative, days = [], [], set()

    def _nums(o):
        if isinstance(o, dict):
            for v in o.values():
                yield from _nums(v)
        elif isinstance(o, list):
            for v in o:
                yield from _nums(v)
        elif isinstance(o, (int, float)) and not isinstance(o, bool):
            yield o

    for p in files:
        m = _re.search(r"(\d{4}-\d{2}-\d{2})", p.stem)
        if m:
            days.add(m.group(1))
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            parse_bad.append((p.name, str(e)[:80]))
            continue
        if any(v < 0 for v in _nums(data)):
            negative.append(p.name)

    gaps = []
    if days:
        d = _dt.date.fromisoformat(min(days))
        end = _dt.date.fromisoformat(max(days))
        while d <= end:
            if d.isoformat() not in days:
                gaps.append(d.isoformat())
            d += _dt.timedelta(days=1)

    counters = _fd.aggregate(_fd.load_fleet())["counters"]
    stale = [(m2, day, cnt) for m2, day, cnt in counters["stale"] if m2 == machine]
    dirty = counters["local"].endswith(_fd.DIRTY_SUFFIX)
    return {
        "machine": machine, "base": base, "files": len(files),
        "range": (min(days), max(days)) if days else (None, None),
        "parse_bad": parse_bad, "negative": negative, "gaps": gaps,
        "stale": stale, "dirty": dirty,
    }


def backpedal_report(r):
    lo, hi = r["range"]
    print("=" * 70)
    print(f"BACKPEDAL — {r['machine']}: {r['files']} day file(s) {lo}..{hi}")
    print("=" * 70)
    print(f"  unparseable files:     {len(r['parse_bad'])}"
          + (f"  {r['parse_bad'][:3]}" if r["parse_bad"] else ""))
    print(f"  negative totals:       {len(r['negative'])}"
          + (f"  {r['negative'][:3]}" if r["negative"] else ""))
    print(f"  stale-cohort days:     {len(r['stale'])}"
          + (f"  {[d for _, d, _ in r['stale'][:6]]}" if r["stale"] else ""))
    print(f"  calendar days absent:  {len(r['gaps'])}  (idle or unexported — not a defect)")
    if r["dirty"]:
        print("  ⚠ working tree is dirty: exports made now get a +dirty stamp that can")
        print("    never rank as the current cohort. Commit before exporting.")
    defects = bool(r["parse_bad"] or r["negative"] or r["stale"])
    print(f"  verdict: {'DEFECTS FOUND' if defects else 'clean'}")
    return defects


if __name__ == "__main__" and (INSTALL_HOOKS or EXPORT or FLEET or VALIDATE):
    import fleet_dailies as _fd

    if INSTALL_HOOKS:
        _ok, _msg = _fd.install_hooks()
        print(f"{'✓' if _ok else '✗'} {_msg}")

    if VALIDATE:
        _r = backpedal_validate(_fd)
        _defects = backpedal_report(_r)
        if FIX and _r["stale"]:
            if _r["dirty"]:
                print("  --fix refused: commit the working tree first (dirty stamp).")
                sys.exit(1)
            _stale_days = sorted(d for _, d, _ in _r["stale"])
            _counter = _r["stale"][0][2][:12]
            _arch = _r["base"] / f"archive-{_counter}"
            _arch.mkdir(exist_ok=True)
            _moved = 0
            for _day in _stale_days:
                for _f in _r["base"].glob(f"*{_day}*"):
                    if _f.is_file():
                        _f.rename(_arch / _f.name)
                        _moved += 1
            print(f"  archived {_moved} stale file(s) -> {_arch}")
            _cmd = [sys.executable, os.path.abspath(__file__),
                    "--start", _stale_days[0], "--end", _stale_days[-1], "--export"]
            print(f"  re-exporting {_stale_days[0]}..{_stale_days[-1]} with current code...")
            subprocess.run(_cmd, check=False)
            _defects = backpedal_report(backpedal_validate(_fd))
        sys.exit(1 if _defects else 0)

    if EXPORT:
        # Sampled BEFORE the export: export_days() creates dailies/<machine>/,
        # so reading the known set afterwards can never see this machine as new.
        _known_before = _fd.known_machines()
        _paths = _fd.export_days(ALL_INVOCATIONS)
        _machine = _fd.resolve_machine()
        print()
        print("=" * 70)
        print(f"Exported {len(_paths)} day(s) for {_machine} -> dailies/{_machine}/")
        print("=" * 70)
        if _paths:
            print(f"  {_paths[0].stem} .. {_paths[-1].stem}")
        _warn = _fd.unintroduced_machine_warning(_machine, known=_known_before)
        if _warn:
            print(f"  ⚠ {_warn}")
        if not _fd.hooks_installed():
            # Say it once, here, rather than letting a fresh clone discover it
            # from unstamped commits weeks later.
            print("  note: hooks not installed in this clone — run --install-hooks")

        # This export used the current counting code. Any day this machine
        # published under a different one is now incomparable to it, and only
        # this machine can fix that — nobody else has its logs. So name the
        # range and the command rather than leaving it to be noticed.
        _cohorts = _fd.aggregate(_fd.load_fleet())["counters"]
        _mine = _fd.stale_range(_cohorts["stale"], _machine)
        if _mine:
            _lo, _hi = _mine
            print(f"  ⚠ {_lo}..{_hi} were exported by different counting code "
                  f"and no longer agree with the days just written.")
            print(f"    re-export them:  python token_tracker.py "
                  f"--start {_lo} --end {_hi} --export")

        if PUBLISH:
            _ok, _msg = _fd.stamped_commit(
                f"dailies({_machine}): publish {len(_paths)} day(s)", _paths
            )
            print(f"  {'✓' if _ok else '·'} {_msg}")
            print("  push with: git push -u origin dailies/" + _machine)

    if FLEET:
        # model_bill injected rather than imported, so fleet_dailies stays free
        # of this module's import cost.
        # The window the caller actually asked for. --fleet used to load every
        # published day regardless of --days/--start/--end, so a 3-day and a
        # 31-day request returned byte-identical totals while echoing the
        # requested period back - a silent wrong answer, not an error.
        _f_since = RANGE_START or (CUTOFF.date().isoformat() if DAYS_EXPLICIT else None)
        _f_until = RANGE_END
        _agg = _fd.aggregate(
            _fd.load_fleet(since=_f_since, until=_f_until), price_fn=model_bill
        )
        _f_window = (f"{_f_since or 'first published'} -> {_f_until or 'today'}"
                     if (_f_since or _f_until) else "all published history")
        print()
        print("=" * 70)
        print(f"Fleet totals — {_agg['machine_count']} machine(s), {_agg['day_count']} day(s)")
        print(f"window: {_f_window}")
        print("=" * 70)
        if not _agg["machines"]:
            print("  no dailies published yet — run --export, then push")
        else:
            print(f"  {'machine':<28}{'input':>12}{'output':>12}{'cache read':>14}")
            for _name, _t in sorted(_agg["machines"].items()):
                print(f"  {_name:<28}{fmt(_t['input_tokens']):>12}"
                      f"{fmt(_t['output_tokens']):>12}{fmt(_t['cache_read']):>14}")
            _tot = _agg["totals"]
            _cnt = _agg["counters"]
            print("  " + "-" * 64)
            if _cnt["mixed"]:
                # These numbers were produced by different counting code, so
                # adding them is arithmetic on incomparable units. Printing one
                # FLEET line anyway would hide that behind a number that looks
                # exactly like a correct one — the failure this whole mechanism
                # exists to prevent. Split it instead.
                for _c, _ct in sorted(_agg["by_counter"].items(),
                                      key=lambda kv: kv[0] != _cnt["current"]):
                    _tag = "current" if _c == _cnt["current"] else "STALE"
                    _label = _c if _c == _fd.UNSTAMPED else _c[:8]
                    print(f"  {'counter ' + _label + ' (' + _tag + ')':<28}"
                          f"{fmt(_ct['input_tokens']):>12}"
                          f"{fmt(_ct['output_tokens']):>12}{fmt(_ct['cache_read']):>14}")
                print(f"  {'FLEET':<28}{'— not summed: see below':>38}")
            else:
                print(f"  {'FLEET':<28}{fmt(_tot['input_tokens']):>12}"
                      f"{fmt(_tot['output_tokens']):>12}{fmt(_tot['cache_read']):>14}")
            # How much of that total is measured rather than inferred. A number
            # without this is not a number you can act on.
            _conf = _agg["confidence"]
            print(f"  {'confidence':<28}{_conf:>11.1%} measured"
                  f"   ({fmt(sum(_agg['estimated'].values()))} estimated)")

            if _agg["partial"]:
                print()
                print(f"  {len(_agg['partial'])} partial day(s) included "
                      "(today is not over on those machines)")

            # Double counting is the one error that inflates a fleet total while
            # every individual file still looks correct — so it is loud.
            if _agg["overlaps"]:
                print()
                print("  ⚠ OVERLAP — these machines appear to have counted the same calls:")
                for _o in _agg["overlaps"][:5]:
                    print(f"      {_o['date']}  {' + '.join(_o['machines'])}  "
                          f"(Jaccard {_o['jaccard']})")
                print("      totals above are inflated by the shared portion")

            # Same class of failure as OVERLAP — the total looks right while
            # every file in it looks right too — so it gets the same volume.
            if _cnt["mixed"]:
                print()
                print("  ⚠ MIXED COUNTING — these machine-days were produced by "
                      "code that counts differently:")
                _by_machine_stale = defaultdict(list)
                for _m, _d, _c in _cnt["stale"]:
                    _by_machine_stale[_m].append(_d)
                for _m, _days in sorted(_by_machine_stale.items()):
                    print(f"      {_m:<16}{len(_days):>4} day(s)  "
                          f"{min(_days)}..{max(_days)}")
                print("      They cannot be added to the current cohort. Each box")
                print("      re-exports its own:  python token_tracker.py "
                      "--start <first> --end <last> --export")
                if not _cnt["local_is_current"]:
                    print(f"      NOTE: this box counts as {_cnt['local'][:8]}, and the "
                          f"corpus is mostly {_cnt['current'][:8]} —")
                    print("      publishing from here would add a third cohort. Pull first.")

            if _agg["anomalies"]:
                print()
                print(f"  outlier days (modified z ≥ {_fd.OUTLIER_Z}, robust to the spike itself):")
                for _an in _agg["anomalies"][:5]:
                    print(f"      {_an['date']}  {fmt(_an['tokens']):>14}  "
                          f"z={_an['z']:+.1f}  {_an['direction']}")
