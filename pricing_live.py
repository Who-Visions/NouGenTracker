from __future__ import annotations
"""pricing_live.py - Dynamic vendor pricing resolver for NouGenTracker.

Authority: GM directive (Rule 0.2 / Rule 0.0, dynamic over hardcode).
Hardened Pricing Pipeline Contract:
  1. UNIT NORMALIZATION: USD per 1M tokens. Detects per-1K vs per-1M notation.
     Rejects unidentifiable/ambiguous entries (falls through ladder, never guessed).
  2. INVARIANT GATES:
     a. 0 < rate < NOUGEN_PRICING_CEILING_PER_M (env, default 1000) for every field.
     b. output_rate >= input_rate (catches column transposition).
     c. cache_read_rate < input_rate. For Claude: cache_read ~= 0.10 x input
        (tolerance env NOUGEN_PRICING_RATIO_TOL, default 0.02).
     d. cache_write is DERIVED (1.25 x input, 5-min tier), never parsed.
  3. DELTA GUARD:
     If fresh price differs from last known value by > NOUGEN_PRICING_DELTA_MAX_PCT (default 50%),
     keep old value for billing, store new under "doc-live-quarantined", log loudly.
  4. EXACT MONEY MATH:
     calculate_cost evaluated with decimal.Decimal end-to-end, rounded ROUND_HALF_EVEN to cents.
  5. TEMPORAL INTEGRITY:
     Cache appends with fetched_at. Pricing past days uses rate current that day (at-or-before).
     New live fetch never retroactively re-prices history. PRICE_SCHEDULE extended.
  6. FAIL-PROOF LADDER:
     env override -> valid cache -> live fetch (gates 1-3) -> last-known-good expired cache ->
     MODEL_PRICING const -> unknown-model default.
     Every hop logged with provenance tag. Pipeline NEVER raises out of pricing.
  7. CANONICAL URLS:
     OpenAI: https://developers.openai.com/api/docs/pricing (platform.openai.com fallback)
     Claude: https://platform.claude.com/docs/en/about-claude/pricing
     Gemini: https://ai.google.dev/gemini-api/docs/pricing
"""

import datetime
import decimal
from decimal import Decimal, ROUND_HALF_EVEN
import html
import json
import logging
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

logger = logging.getLogger("pricing_live")

# --- Source Tags -------------------------------------------------------------
class SourceTag(str):
    """String subclass that preserves string representation while supporting
    backward-compatible equality with legacy tags (e.g. 'doc')."""

    def __new__(cls, val, aliases=()):
        obj = str.__new__(cls, val)
        obj._aliases = set(aliases)
        return obj

    def __eq__(self, other):
        return str(self) == other or other in self._aliases

    def __hash__(self):
        return str.__hash__(self)


DOC = "doc"
EST = "est"
LIVE = SourceTag("doc-live", aliases=["doc"])
CACHED = SourceTag("doc-cached", aliases=["doc"])
EXPIRED_CACHE = SourceTag("doc-cache-expired", aliases=["doc", "doc-cached"])
FALLBACK_CONST = SourceTag("fallback-const", aliases=["doc"])
ENV = SourceTag("env", aliases=["doc"])
QUARANTINED = SourceTag("doc-live-quarantined", aliases=["doc"])
DEFAULT_UNPRICED = SourceTag("default-unpriced", aliases=["est"])

# --- Exact Money Math --------------------------------------------------------

def calculate_cost(
    input_tokens: int | Decimal = 0,
    output_tokens: int | Decimal = 0,
    cache_write_tokens: int | Decimal = 0,
    cache_read_tokens: int | Decimal = 0,
    reasoning_tokens: int | Decimal = 0,
    inp_rate: float | str | Decimal = 0.0,
    out_rate: float | str | Decimal = 0.0,
    cache_read_rate: float | str | Decimal = 0.0,
) -> Decimal:
    """Exact token cost evaluated end-to-end in decimal.Decimal.

    Formula:
      (input*in + cache_write*1.25*in + cache_read*cr + (output+reasoning)*out) / 1e6

    Evaluated entirely with decimal.Decimal. Floats are converted via str() to
    prevent binary floating-point representation artifacts.
    """
    inp_dec = Decimal(str(inp_rate if inp_rate is not None else 0.0))
    out_dec = Decimal(str(out_rate if out_rate is not None else 0.0))
    cr_dec = Decimal(str(cache_read_rate if cache_read_rate is not None else 0.0))
    # Invariant Gate 2.d: cache_write is DERIVED (1.25 x input, 5-min tier), never parsed
    cw_rate_dec = Decimal("1.25") * inp_dec

    i_dec = Decimal(str(int(input_tokens or 0)))
    o_dec = Decimal(str(int(output_tokens or 0)))
    cw_tok_dec = Decimal(str(int(cache_write_tokens or 0)))
    cr_tok_dec = Decimal(str(int(cache_read_tokens or 0)))
    r_dec = Decimal(str(int(reasoning_tokens or 0)))

    one_m = Decimal("1000000")
    total = (
        i_dec * inp_dec
        + cw_tok_dec * cw_rate_dec
        + cr_tok_dec * cr_dec
        + (o_dec + r_dec) * out_dec
    ) / one_m
    return total


def round_to_cents(cost: Decimal) -> Decimal:
    """Round Decimal cost to cents using ROUND_HALF_EVEN."""
    if not isinstance(cost, Decimal):
        cost = Decimal(str(cost))
    return cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


# --- Unit Normalization ------------------------------------------------------

def detect_unit_multiplier(text: str) -> float | None:
    """Detect per-1K vs per-1M token unit notation in context text.

    Returns:
      1.0 for per-1M notation (USD per 1,000,000 tokens)
      1000.0 for per-1K notation (converts USD per 1,000 tokens to USD per 1M)
      None if unit cannot be confidently identified or is ambiguous.
    """
    if not text:
        return None
    s = text.lower()

    # Per-1K indicators: e.g. /ktok, per 1k, per thousand, 1,000 tokens (not 1,000,000)
    has_1k = bool(
        re.search(r'/\s*(?:1\s*k|ktok|k\b|thousand)', s)
        or re.search(r'per\s+(?:1\s*k|1000|thousand)', s)
        or re.search(r'\b1k\s*tokens?\b', s)
        or (re.search(r'\b1,?000\b', s) and not re.search(r'\b1,?000,?000\b', s) and "token" in s)
    )

    # Per-1M indicators: e.g. /mtok, per 1m, per million, 1,000,000 tokens
    has_1m = bool(
        re.search(r'/\s*(?:1\s*m|mtok|m\b|million)', s)
        or re.search(r'per\s+(?:1\s*m|1000000|million)', s)
        or re.search(r'\b1m\s*tokens?\b', s)
        or re.search(r'\b1,?000,?000\b', s)
    )

    if has_1k and has_1m:
        # Conflicting or ambiguous notation in the same context
        return None
    if has_1m:
        return 1.0
    if has_1k:
        return 1000.0
    return None


# --- Invariant Gates ---------------------------------------------------------

# Published cache_read/input ratio per Claude family. Anything not listed is 0.10.
CLAUDE_CACHE_RATIO_BY_FAMILY = {
    "fable-5.1": 0.025,
    "mythos-5.1": 0.025,
    "opus-5.5": 0.05,
    "sonnet-5.5": 0.05,
}


_RATIO_CLAUSE = re.compile(r"(\d*\.\d+)\s*x\s+on\s+((?:Claude\s+[A-Za-z]+\s+[\d.]+(?:\s*(?:,|and)\s*)?)+)", re.I)
_RATIO_MODEL = re.compile(r"Claude\s+([A-Za-z]+)\s+([\d.]+)", re.I)


def learn_claude_cache_ratios(text: str) -> dict:
    """Read per-family cache-hit multipliers from the pricing page itself.

    The page states exceptions in prose ("0.05x on Claude Opus 5.5"); a
    hardcoded map goes stale the day a new family ships with its own ratio
    and gate 2.c then rejects that family's genuine live row.
    """
    learned = {}
    for m in _RATIO_CLAUSE.finditer(text or ""):
        ratio = float(m.group(1))
        if not 0 < ratio < 1:
            continue
        for fam, ver in _RATIO_MODEL.findall(m.group(2)):
            learned[f"{fam.lower()}-{ver.rstrip('.')}"] = ratio
    CLAUDE_CACHE_RATIO_BY_FAMILY.update(learned)
    return learned


def expected_claude_cache_ratio(key: str) -> float:
    low = key.lower().replace("-", ".")
    # Longest family first, so "opus-5.5" is not shadowed by a shorter match.
    for family, ratio in sorted(CLAUDE_CACHE_RATIO_BY_FAMILY.items(), key=lambda kv: -len(kv[0])):
        if family.replace("-", ".") in low:
            return ratio
    return 0.10


def validate_live_price(key: str, inp: float, out: float, cache_read: float) -> tuple[bool, str]:
    """Validate a parsed live price against invariant gates (a, b, c, d).

    Returns:
      (is_valid_bool, rejection_reason_str)
    """
    # Gate 2.a: 0 < rate < NOUGEN_PRICING_CEILING_PER_M (env, default 1000) for every field
    ceiling_env = os.environ.get("NOUGEN_PRICING_CEILING_PER_M", "1000")
    try:
        ceiling = float(ceiling_env)
    except ValueError:
        ceiling = 1000.0

    for field_name, rate in (("input", inp), ("output", out), ("cache_read", cache_read)):
        if rate <= 0.0:
            return False, f"Gate 2.a violation: {field_name}_rate={rate} must be > 0"
        if rate >= ceiling:
            return False, f"Gate 2.a violation: {field_name}_rate={rate} exceeds ceiling {ceiling}"

    # Gate 2.b: output_rate >= input_rate (catches column transposition)
    if out < inp:
        return False, f"Gate 2.b violation: column transposition (output_rate {out} < input_rate {inp})"

    # Gate 2.c: cache_read_rate < input_rate
    if cache_read >= inp:
        return False, f"Gate 2.c violation: cache_read_rate {cache_read} >= input_rate {inp}"

    # For Claude models: assert cache_read ~= the family's published ratio of
    # input. 0.10 for every family through Opus/Sonnet 5; the Mythos tier
    # (fable-5.1 / mythos-5.1) lists cache_read at 0.025 x input. A gate that
    # hard-coded 0.10 rejected those rows outright, which left every day with
    # Fable 5.1 usage unpriceable and the fleet report at 0 machines
    # (2026-09-21, hyperion follow-ups 3+4). Widening the tolerance would
    # weaken the guard for every model; a per-family expectation does not.
    if "claude" in key.lower():
        tol_env = os.environ.get("NOUGEN_PRICING_RATIO_TOL", "0.02")
        try:
            tol = float(tol_env)
        except ValueError:
            tol = 0.02
        expected = expected_claude_cache_ratio(key)
        ratio = cache_read / inp
        if abs(ratio - expected) > tol:
            return False, (f"Gate 2.c violation: Claude cache_read ratio {ratio:.4f} "
                           f"differs from {expected} by > {tol}")

    # Gate 2.d: cache_write is derived (1.25 x input, 5-min tier), never parsed
    return True, ""


# --- Delta Guard -------------------------------------------------------------

def check_delta_guard(
    key: str,
    fresh_price: tuple[float, float, float],
    last_known_price: tuple[float, float, float] | None,
) -> tuple[bool, str]:
    """Delta guard: check if fresh price differs from last known value by > NOUGEN_PRICING_DELTA_MAX_PCT.

    Returns:
      (passes_guard_bool, details_str)
    """
    if not last_known_price:
        return True, "no prior baseline"

    delta_max_env = os.environ.get("NOUGEN_PRICING_DELTA_MAX_PCT", "50")
    try:
        max_pct = float(delta_max_env)
    except ValueError:
        max_pct = 50.0

    inp_new, out_new, cr_new = fresh_price[:3]
    inp_old, out_old, cr_old = last_known_price[:3]

    diffs = []
    for name, v_new, v_old in (("input", inp_new, inp_old), ("output", out_new, out_old), ("cache_read", cr_new, cr_old)):
        if v_old > 0:
            pct = abs(v_new - v_old) / v_old * 100.0
            if pct > max_pct:
                diffs.append(f"{name}: {v_old} -> {v_new} ({pct:.1f}% > {max_pct}%)")

    if diffs:
        return False, f"Delta guard triggered for {key} (> {max_pct}%): " + ", ".join(diffs)
    return True, "within delta limit"


# --- Vendor Endpoints --------------------------------------------------------
VENDOR_SOURCES = [
    (
        "anthropic",
        [
            ("https://platform.claude.com/docs/en/about-claude/pricing.md", {"Accept": "text/markdown"}),
            ("https://platform.claude.com/docs/en/about-claude/pricing", {"Accept": "text/markdown, text/html"}),
        ],
    ),
    (
        "gemini",
        [
            ("https://ai.google.dev/gemini-api/docs/pricing.md.txt", {"Accept": "text/plain, text/markdown"}),
            ("https://ai.google.dev/gemini-api/docs/pricing", {"Accept": "text/markdown, text/html"}),
        ],
    ),
    (
        "openai",
        [
            # GM-corrected canonical URL (2026-08-28); platform path kept as secondary fallback
            ("https://developers.openai.com/api/docs/pricing?latest-pricing=standard", {"Accept": "text/html"}),
            ("https://developers.openai.com/api/docs/pricing", {"Accept": "text/html"}),
            ("https://platform.openai.com/docs/pricing", {"Accept": "text/html"}),
        ],
    ),
]

# Process-level state
_ATTEMPTED_VENDORS = set()
_MEMORY_CACHE = {}
_MEMORY_QUARANTINED = {}
_MEMORY_SCHEDULES = {}
_LAST_EXTRACTED_SCHEDULES = {}
_MEMORY_HISTORY = []
_MEMORY_FETCHED_AT = None


def get_cache_path() -> Path:
    """Resolve cache file path: NOUGEN_PRICING_CACHE_PATH or %USERPROFILE%\\.nougen\\pricing_cache.json."""
    custom = os.environ.get("NOUGEN_PRICING_CACHE_PATH")
    if custom:
        return Path(custom)
    userprofile = os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home())
    return Path(userprofile) / ".nougen" / "pricing_cache.json"


def get_ttl_hours() -> float:
    """Read TTL from NOUGEN_PRICING_TTL_HOURS (default 24). Logs when default is used."""
    val = os.environ.get("NOUGEN_PRICING_TTL_HOURS")
    if val is not None:
        try:
            return float(val)
        except ValueError:
            logger.warning("Invalid NOUGEN_PRICING_TTL_HOURS=%r; falling back to default 24 hours", val)
            return 24.0
    logger.info("NOUGEN_PRICING_TTL_HOURS not set; using fallback default 24 hours")
    return 24.0


def get_fetch_timeout_s() -> float:
    """Read fetch timeout in seconds from NOUGEN_PRICING_FETCH_TIMEOUT_S (default 8)."""
    val = os.environ.get("NOUGEN_PRICING_FETCH_TIMEOUT_S")
    if val is not None:
        try:
            return float(val)
        except ValueError:
            pass
    return 8.0


def normalize_model_name(name: str) -> str:
    """Normalize model names to tracker key style: lowercase, hyphens, no surrounding punctuation."""
    if not name:
        return ""
    # Strip markdown links e.g. [name](url)
    name = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', name)
    # Strip notes, brackets, parentheses e.g. "(<272K context length)", "(retired...)", "[1]"
    name = re.sub(r'\[.*?\]', '', name)
    name = re.sub(r'\(.*?\)', '', name)
    s = name.lower().strip()
    # Replace spaces, slashes, underscores with hyphens
    s = re.sub(r'[\s/_]+', '-', s)
    # Remove any characters except alphanumeric, hyphen, dot
    s = re.sub(r'[^a-z0-9\.\-]+', '', s)
    s = re.sub(r'-+', '-', s).strip('-')
    return s


# --- Parsers -----------------------------------------------------------------

def parse_anthropic_pricing(text: str) -> dict:
    """Tolerant parser for Anthropic pricing pages with unit normalization & invariant gates."""
    results = {}
    if not text:
        return results

    try:
        page_unit = detect_unit_multiplier(text)
        learn_claude_cache_ratios(text)

        # 1. Try Markdown table
        if "|" in text and "---" in text:
            lines = text.splitlines()
            in_table = False
            headers = []
            for line in lines:
                line_str = line.strip()
                if not line_str.startswith("|"):
                    in_table = False
                    continue
                parts = [c.strip() for c in line_str.strip("|").split("|")]
                if any("base input" in p.lower() or "input tokens" in p.lower() for p in parts):
                    headers = [p.lower() for p in parts]
                    in_table = True
                    continue
                if in_table and any("---" in p for p in parts):
                    continue
                if in_table and headers and len(parts) >= len(headers):
                    row = dict(zip(headers, parts))
                    model_raw = parts[0]
                    inp_col = next((v for k, v in row.items() if "base input" in k or ("input" in k and "batch" not in k and "fast" not in k)), None)
                    out_col = next((v for k, v in row.items() if "output" in k and "batch" not in k and "fast" not in k), None)
                    cache_col = next((v for k, v in row.items() if "cache hit" in k or "cache read" in k), None)

                    if inp_col and out_col:
                        m_in = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', inp_col)
                        m_out = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', out_col)
                        m_cache = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', cache_col) if cache_col else []
                        if m_in and m_out:
                            # Detect unit multiplier: cell -> row -> page
                            unit_mult = (
                                detect_unit_multiplier(inp_col + " " + out_col + " " + (cache_col or ""))
                                or detect_unit_multiplier(line_str)
                                or page_unit
                            )
                            if unit_mult is None:
                                logger.warning("Rejecting %s: could not confidently normalize unit notation", model_raw)
                                continue

                            inp_val = float(m_in[0]) * unit_mult
                            out_val = float(m_out[0]) * unit_mult
                            cache_val = (float(m_cache[0]) * unit_mult) if m_cache else round(inp_val * 0.1, 4)

                            key = normalize_model_name(model_raw)
                            if key:
                                ok, reason = validate_live_price(key, inp_val, out_val, cache_val)
                                if not ok:
                                    logger.warning("Rejecting %s: %s", key, reason)
                                    continue
                                val = (inp_val, out_val, cache_val)
                                results[key] = val
                                if "." in key:
                                    results[key.replace(".", "-")] = val

        # 2. Try HTML tables if markdown did not produce results
        if not results:
            tables = re.findall(r'<table[\s\S]*?</table>', text, re.IGNORECASE)
            for t in tables:
                table_unit = detect_unit_multiplier(t) or page_unit
                rows = re.findall(r'<tr[\s\S]*?</tr>', t, re.IGNORECASE)
                headers = []
                for r in rows:
                    cells = [re.sub(r'<[^>]+>', ' ', c).strip() for c in re.findall(r'<t[dh][\s\S]*?</t[dh]>', r, re.IGNORECASE)]
                    if not cells:
                        continue
                    if any('base input' in c.lower() or 'output token' in c.lower() for c in cells):
                        headers = [c.lower() for c in cells]
                        continue
                    if headers and len(cells) >= len(headers):
                        row = dict(zip(headers, cells))
                        model_raw = cells[0]
                        inp_col = next((v for k, v in row.items() if "base input" in k), None)
                        out_col = next((v for k, v in row.items() if "output token" in k), None)
                        cache_col = next((v for k, v in row.items() if "cache hit" in k or "cache read" in k), None)
                        if inp_col and out_col:
                            m_in = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', inp_col)
                            m_out = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', out_col)
                            m_cache = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', cache_col) if cache_col else []
                            if m_in and m_out:
                                unit_mult = (
                                    detect_unit_multiplier(inp_col + " " + out_col + " " + (cache_col or ""))
                                    or table_unit
                                )
                                if unit_mult is None:
                                    logger.warning("Rejecting %s: could not confidently normalize unit notation", model_raw)
                                    continue

                                inp_val = float(m_in[0]) * unit_mult
                                out_val = float(m_out[0]) * unit_mult
                                cache_val = (float(m_cache[0]) * unit_mult) if m_cache else round(inp_val * 0.1, 4)

                                key = normalize_model_name(model_raw)
                                if key:
                                    ok, reason = validate_live_price(key, inp_val, out_val, cache_val)
                                    if not ok:
                                        logger.warning("Rejecting %s: %s", key, reason)
                                        continue
                                    val = (inp_val, out_val, cache_val)
                                    results[key] = val
                                    if "." in key:
                                        results[key.replace(".", "-")] = val
    except Exception as e:
        logger.warning("Anthropic pricing parse error: %s; degraded to no-data", e)

    return results


_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_DATE_CLAUSE_RE = re.compile(
    r'\b(through|starting|effective|after|from)\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})',
    re.IGNORECASE,
)


def _parse_english_date(date_str: str) -> str | None:
    """Parse 'November 30, 2026' or 'Dec 1, 2026' -> 'YYYY-MM-DD', or None."""
    if not date_str:
        return None
    cleaned = re.sub(r'\s+', ' ', date_str.replace(",", " ")).strip()
    m = re.fullmatch(r'([A-Za-z]+)\s+(\d{1,2})\s+(\d{4})', cleaned)
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if not month:
        return None
    try:
        d = datetime.date(int(m.group(3)), month, int(m.group(2)))
        return d.isoformat()
    except ValueError:
        return None


def _clean_gemini_cell_text(raw: str) -> str:
    """Normalize Markdoc/HTML markup in a table cell while preserving line breaks."""
    if not raw:
        return ""
    s = html.unescape(raw)
    s = re.sub(r'<br\s*/?>|</p>|</div>|</li>', '\n', s, flags=re.IGNORECASE)
    s = re.sub(r'{%\s*/?\s*sup\s*%}.*?{%\s*/\s*sup\s*%}', ' ', s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r'{%[^%]*%}', ' ', s)
    s = re.sub(r'<sup[^>]*>[\s\S]*?</sup>', ' ', s, flags=re.IGNORECASE)
    s = re.sub(r'<[^>]+>', ' ', s)
    lines = []
    for line in s.splitlines():
        compact = re.sub(r'[ \t]+', ' ', line).strip()
        if compact:
            lines.append(compact)
    return "\n".join(lines)


def _is_non_token_rate_segment(seg_low: str) -> bool:
    """Return True if a pricing segment describes storage, duration, per-image, or tool request fees."""
    if any(tok in seg_low for tok in (
        "storage price", "/ hour", "per hour", "equivalent to",
        "per image", "/image", "per frame", "per second", "per 10s",
        "/min", "/ min", "per min", "search request", "search quer",
        "grounded prompt", "requests per",
    )):
        return True
    if ("request" in seg_low or "quer" in seg_low) and "token" not in seg_low:
        return True
    return False


def _detect_token_unit_strict(text: str) -> float | None:
    """Detect per-1K vs per-1M token unit ONLY when context describes tokens, not requests/queries/images."""
    if not text:
        return None
    low = text.lower()
    if any(non_tok in low for non_tok in ("request", "query", "queries", "image", "frame", "second", "min", "hour")):
        # Strip non-token phrases unless "tokens" is explicitly tied to this header/segment.
        if "token" not in low:
            return None
    return detect_unit_multiplier(low)


def _find_gemini_column_indices(headers: list[str], *, require_feature_name: bool = True) -> tuple[int, int] | None:
    """Resolve (feature_col_idx, paid_col_idx) from explicit column headers; fail closed on unknown/ambiguous shapes."""
    if not headers or len(headers) < 2:
        return None
    norm = [re.sub(r'\s+', ' ', _clean_gemini_cell_text(h)).strip().lower() for h in headers]
    paid_matches = [i for i, h in enumerate(norm) if "paid tier" in h]
    if len(paid_matches) != 1:
        return None
    paid_idx = paid_matches[0]

    feature_matches = [i for i, h in enumerate(norm) if h == "feature" or h.startswith("feature ")]
    if len(feature_matches) == 1:
        feature_idx = feature_matches[0]
    elif not require_feature_name and not feature_matches and paid_idx != 0 and norm[0] == "":
        feature_idx = 0
    else:
        return None

    if feature_idx == paid_idx:
        return None
    return (feature_idx, paid_idx)


def _parse_gemini_paid_cell_entries(cell_raw: str, header_unit: float | None) -> list[tuple[float, str | None, str | None]]:
    """Extract text-compatible token rate entries [(usd_per_1m, start_iso, end_iso), ...] from a Paid Tier cell.

    Returns [] if the cell is unavailable, lacks unambiguous token units, or only covers
    non-text modalities (e.g. audio-only TTS output, audio-only STT input, or per-image).
    """
    cleaned = _clean_gemini_cell_text(cell_raw)
    if not cleaned:
        return []

    # Strip secondary metered equivalents like "or $0.005/min" while preserving trailing modality tags.
    normalized = re.sub(
        r'\bor\s+\$[0-9]+(?:\.[0-9]+)?\s*/\s*(?:min|10s|sec|second|image|frame|hour)\b',
        '',
        cleaned,
        flags=re.IGNORECASE,
    )
    # Strip "Equivalent to $... per 10s ..." clauses
    normalized = re.sub(r'\bEquivalent to\b[^\n.]*\.?', '', normalized, flags=re.IGNORECASE)

    # Split into lines and into separate "$..." segments so multi-rate cells cannot cross-contaminate
    raw_segments = []
    for line in normalized.splitlines():
        parts = re.split(r'(?=\$[0-9])', line)
        raw_segments.extend(p.strip() for p in parts if p.strip())

    baseline_entries = []
    long_context_entries = []

    for seg in raw_segments:
        if "$" not in seg:
            continue
        seg_low = seg.lower()
        if _is_non_token_rate_segment(seg_low):
            continue

        # Modality gate: if non-text modality keywords appear on this price segment without "text", skip it.
        has_non_text_mod = bool(re.search(r'\b(audio|video|image)\b', seg_low))
        has_text_mod = bool(re.search(r'\btext\b', seg_low))
        if has_non_text_mod and not has_text_mod:
            continue

        m_dollar = re.search(r'\$([0-9]+(?:\.[0-9]+)?)', seg)
        if not m_dollar:
            continue

        seg_unit = _detect_token_unit_strict(seg)
        unit = seg_unit if seg_unit is not None else header_unit
        if unit is None:
            # Fail closed when neither cell segment nor Paid Tier header yields an unambiguous token unit
            continue

        val = float(m_dollar.group(1)) * unit

        start_iso, end_iso = None, None
        m_date = _DATE_CLAUSE_RE.search(seg)
        if m_date:
            kw = m_date.group(1).lower()
            parsed_dt = _parse_english_date(m_date.group(2))
            if parsed_dt:
                if kw == "through":
                    end_iso = parsed_dt
                else:
                    start_iso = parsed_dt

        entry = (val, start_iso, end_iso)
        if re.search(r'>\s*200k|>\s*128k', seg_low):
            long_context_entries.append(entry)
        else:
            baseline_entries.append(entry)

    return baseline_entries


def _entry_matches_day(entry: tuple[float, str | None, str | None], day: str) -> bool:
    _, start_iso, end_iso = entry
    if start_iso is not None and day < start_iso:
        return False
    if end_iso is not None and day > end_iso:
        return False
    return True


def _pick_rate_for_day(entries: list[tuple[float, str | None, str | None]], day: str) -> float | None:
    if not entries:
        return None
    dated_hits = [e for e in entries if (e[1] is not None or e[2] is not None) and _entry_matches_day(e, day)]
    if dated_hits:
        return dated_hits[0][0]
    undated = [e for e in entries if e[1] is None and e[2] is None]
    if undated:
        return undated[0][0]
    return None


def _build_gemini_model_resolution(
    keys: list[str],
    inp_entries: list[tuple[float, str | None, str | None]],
    out_entries: list[tuple[float, str | None, str | None]],
    cache_entries: list[tuple[float, str | None, str | None]],
    as_of: str,
) -> tuple[dict, dict]:
    """Resolve current (as_of) pricing AND dated schedule bands across input, output, and cache entries."""
    resolved_models = {}
    resolved_schedules = {}
    if not (inp_entries and out_entries and cache_entries):
        return resolved_models, resolved_schedules

    all_entries = inp_entries + out_entries + cache_entries
    has_dates = any(e[1] is not None or e[2] is not None for e in all_entries)

    if not has_dates:
        inp, out, cache = inp_entries[0][0], out_entries[0][0], cache_entries[0][0]
        for key in keys:
            ok, reason = validate_live_price(key, inp, out, cache)
            if not ok:
                logger.warning("Rejecting %s: %s", key, reason)
                continue
            val = (inp, out, cache)
            resolved_models[key] = val
            if "." in key:
                resolved_models[key.replace(".", "-")] = val
        return resolved_models, resolved_schedules

    # Collect transition start dates from all dated bands
    starts = set()
    ends = set()
    for _, s_dt, e_dt in all_entries:
        if s_dt is not None:
            starts.add(s_dt)
        if e_dt is not None:
            ends.add(e_dt)
            next_day = (datetime.date.fromisoformat(e_dt) + datetime.timedelta(days=1)).isoformat()
            starts.add(next_day)

    if any(e[1] is None for e in all_entries):
        starts.add("0000-01-01")

    sorted_starts = sorted(starts)
    bands_raw = []
    for idx, b_start in enumerate(sorted_starts):
        if idx + 1 < len(sorted_starts):
            b_end = (datetime.date.fromisoformat(sorted_starts[idx + 1]) - datetime.timedelta(days=1)).isoformat()
        else:
            b_end = None
        probe_day = b_end if (b_start == "0000-01-01" and b_end is not None) else b_start
        if b_start == "0000-01-01" and b_end is None:
            probe_day = as_of
        inp = _pick_rate_for_day(inp_entries, probe_day)
        out = _pick_rate_for_day(out_entries, probe_day)
        cache = _pick_rate_for_day(cache_entries, probe_day)
        if inp is not None and out is not None and cache is not None:
            bands_raw.append((b_start, b_end, (inp, out, cache)))

    active_inp = _pick_rate_for_day(inp_entries, as_of)
    active_out = _pick_rate_for_day(out_entries, as_of)
    active_cache = _pick_rate_for_day(cache_entries, as_of)

    for key in keys:
        valid_bands = []
        for b_start, b_end, trio in bands_raw:
            ok, reason = validate_live_price(key, trio[0], trio[1], trio[2])
            if ok:
                valid_bands.append((b_start, b_end, trio))
            else:
                logger.warning("Rejecting %s band %s..%s: %s", key, b_start, b_end, reason)

        if valid_bands:
            resolved_schedules[key] = valid_bands
            if "." in key:
                resolved_schedules[key.replace(".", "-")] = valid_bands

        if active_inp is not None and active_out is not None and active_cache is not None:
            ok, reason = validate_live_price(key, active_inp, active_out, active_cache)
            if ok:
                val = (active_inp, active_out, active_cache)
                resolved_models[key] = val
                if "." in key:
                    resolved_models[key.replace(".", "-")] = val
            else:
                logger.warning("Rejecting %s: %s", key, reason)

    return resolved_models, resolved_schedules


def _split_markdoc_table_rows(table_body: str) -> list[list[str]]:
    """Split a Markdoc {% table %} ... {% /table %} block into trimmed rows of cell strings."""
    raw_rows = re.split(r'\n[ \t]*---+[ \t]*\n', table_body.strip())
    parsed_rows = []
    for raw_r in raw_rows:
        r_str = raw_r.strip("\n")
        if not r_str.strip():
            continue
        # Each cell begins at line-start optional spaces + "* " (or bare "*" at EOL for empty cells)
        parts = re.split(r'(?:^|\n)[ \t]*\*(?:[ \t]+|$)', r_str)
        # re.split produces an empty leading element before the first cell
        if parts and parts[0].strip() == "":
            parts = parts[1:]
        if parts:
            parsed_rows.append([p.strip() for p in parts])
    return parsed_rows


def _extract_gemini_section_keys(heading_raw: str, pre_tab_body: str) -> list[str]:
    """Extract normalized model IDs from backticked Markdoc links or headings; fail closed on invalid/non-model headings."""
    heading_clean = re.sub(r'{%[^%]*%}', '', heading_raw).strip()
    heading_ascii = heading_clean.encode('ascii', 'ignore').decode('ascii').strip()
    low_head = heading_ascii.lower()
    if not heading_ascii or any(skip in low_head for skip in (
        "pricing", "notes", "ai agent context", "gemma ", "lyria", "embedding", "robotics",
    )):
        return []

    keys = []
    # 1. Explicit canonical model slugs like [`gemini-4-carbon`](/docs/models/gemini-4-carbon)
    for m in re.finditer(r'\[\s*`([^`]*)`\s*\]\([^)]*/docs/models/[^)]*\)', pre_tab_body, re.IGNORECASE):
        slug = m.group(1).strip()
        if not slug:
            # Explicit empty/malformed slug fails closed rather than guessing from prose
            return []
        norm = normalize_model_name(slug)
        if norm and norm not in keys:
            keys.append(norm)

    if keys:
        return keys

    # 2. Fallback to normalized heading if no explicit model link was present
    norm_head = normalize_model_name(heading_ascii)
    if norm_head and norm_head.startswith("gemini-"):
        keys.append(norm_head)
    return keys


def parse_gemini_markdown_pricing_with_schedules(
    text: str,
    as_of: str = None,
) -> tuple[dict, dict]:
    """Parse Google Gemini Markdoc pricing pages into (current_prices, schedules).

    Enforces:
      - Standard tab only when {% tabs %} is present (fail closed if missing)
      - Explicit column-header resolution for Feature and Paid Tier (no cells[-1] assumption)
      - Exact row column-count matching against headers
      - Header-only token unit extraction (grounding rows cannot poison units)
      - Full modality validation on both Input and Output (excludes TTS, STT/audio-only, image-only)
      - Temporal date-band schedule extraction (through/starting dates)
    """
    results = {}
    schedules = {}
    if not text or "{% table %}" not in text:
        return results, schedules
    text = html.unescape(text)

    target_day = as_of or datetime.date.today().isoformat()

    try:
        sections = re.split(r'^##[ \t]+', text, flags=re.MULTILINE)[1:]
        for sec in sections:
            lines = sec.splitlines()
            heading_raw = lines[0] if lines else ""
            body = "\n".join(lines[1:])

            # Extract text before the first {% tabs %} / {% table %} for canonical slug lookup
            pre_tab_end = body.find("{% tabs %}")
            if pre_tab_end == -1:
                pre_tab_end = body.find("{% table %}")
            pre_tab_text = body[:pre_tab_end] if pre_tab_end != -1 else body

            keys = _extract_gemini_section_keys(heading_raw, pre_tab_text)
            if not keys:
                continue

            if "{% tabs %}" in body:
                std_m = re.search(
                    r'{%\s*tab\s+title=["\']Standard["\']\s*%}([\s\S]*?){%\s*/tab\s*%}',
                    body,
                    re.IGNORECASE,
                )
                if not std_m:
                    # Tabs exist without Standard -> fail closed (never parse Batch/Priority as Standard)
                    continue
                sec_to_search = std_m.group(1)
            else:
                sec_to_search = body

            table_m = re.search(r'{%\s*table\s*%}([\s\S]*?){%\s*/table\s*%}', sec_to_search, re.IGNORECASE)
            if not table_m:
                continue

            rows = _split_markdoc_table_rows(table_m.group(1))
            if len(rows) < 2:
                continue

            headers = rows[0]
            col_idxs = _find_gemini_column_indices(headers, require_feature_name=True)
            if col_idxs is None:
                continue
            feature_idx, paid_idx = col_idxs

            header_unit = _detect_token_unit_strict(headers[paid_idx])

            inp_entries = []
            out_entries = []
            cache_entries = []

            malformed_row = False
            for cells in rows[1:]:
                if len(cells) != len(headers):
                    malformed_row = True
                    break
                label = _clean_gemini_cell_text(cells[feature_idx]).lower()
                paid_raw = cells[paid_idx]
                if "input price" in label and not inp_entries:
                    inp_entries = _parse_gemini_paid_cell_entries(paid_raw, header_unit)
                elif "output price" in label and not out_entries:
                    out_entries = _parse_gemini_paid_cell_entries(paid_raw, header_unit)
                elif "context caching" in label and not cache_entries:
                    cache_entries = _parse_gemini_paid_cell_entries(paid_raw, header_unit)

            if malformed_row:
                continue

            sec_models, sec_scheds = _build_gemini_model_resolution(
                keys, inp_entries, out_entries, cache_entries, target_day
            )
            results.update(sec_models)
            schedules.update(sec_scheds)
    except Exception as e:
        logger.warning("Gemini Markdoc pricing parse error: %s; degraded to no-data", e)

    return results, schedules


def parse_gemini_markdown_pricing(text: str, as_of: str = None) -> dict:
    """Parse Google Gemini Markdoc pricing pages into {model_key: (in, out, cache_read)}."""
    models, _ = parse_gemini_markdown_pricing_with_schedules(text, as_of=as_of)
    return models


def _parse_gemini_html_pricing_with_schedules(text: str, as_of: str = None) -> tuple[dict, dict]:
    """Hardened HTML table parser for Google Gemini API pricing pages."""
    results = {}
    schedules = {}
    if not text or "<table" not in text.lower():
        return results, schedules

    target_day = as_of or datetime.date.today().isoformat()

    try:
        sections = re.findall(r'<h2[^>]*>([\s\S]*?)</h2>([\s\S]*?)(?=<h2|$)', text, re.IGNORECASE)
        for h2_raw, content in sections:
            model_name = re.sub(r'<[^>]+>', '', h2_raw).strip()
            model_name = model_name.encode('ascii', 'ignore').decode('ascii').strip()
            if not model_name or 'pricing' in model_name.lower():
                continue

            std_match = re.search(r'<h3[^>]*>\s*Standard\s*</h3>([\s\S]*?)<(?:h[23]|/section)', content, re.IGNORECASE)
            sec_to_search = std_match.group(1) if std_match else content
            table_match = re.search(r'<table[\s\S]*?</table>', sec_to_search, re.IGNORECASE)
            if not table_match:
                continue

            table_str = table_match.group(0)
            rows = re.findall(r'<tr[\s\S]*?</tr>', table_str, re.IGNORECASE)
            if len(rows) < 2:
                continue

            parsed_rows = []
            for r in rows:
                cells = [c for c in re.findall(r'<t[dh][^>]*>([\s\S]*?)</t[dh]>', r, re.IGNORECASE)]
                if cells:
                    parsed_rows.append(cells)

            if len(parsed_rows) < 2:
                continue

            headers = parsed_rows[0]
            col_idxs = _find_gemini_column_indices(headers, require_feature_name=False)
            if col_idxs is None:
                continue
            feature_idx, paid_idx = col_idxs
            header_unit = _detect_token_unit_strict(_clean_gemini_cell_text(headers[paid_idx]))

            inp_entries = []
            out_entries = []
            cache_entries = []

            for cells in parsed_rows[1:]:
                if len(cells) != len(headers):
                    continue
                label = _clean_gemini_cell_text(cells[feature_idx]).lower()
                paid_raw = cells[paid_idx]
                if "input price" in label and not inp_entries:
                    inp_entries = _parse_gemini_paid_cell_entries(paid_raw, header_unit)
                elif "output price" in label and not out_entries:
                    out_entries = _parse_gemini_paid_cell_entries(paid_raw, header_unit)
                elif "context caching" in label and not cache_entries:
                    cache_entries = _parse_gemini_paid_cell_entries(paid_raw, header_unit)

            key = normalize_model_name(model_name)
            if key:
                sec_models, sec_scheds = _build_gemini_model_resolution(
                    [key], inp_entries, out_entries, cache_entries, target_day
                )
                results.update(sec_models)
                schedules.update(sec_scheds)
    except Exception as e:
        logger.warning("Gemini HTML pricing parse error: %s; degraded to no-data", e)

    return results, schedules


def parse_gemini_pricing_with_schedules(text: str, as_of: str = None) -> tuple[dict, dict]:
    """Dispatch Gemini Markdoc or HTML pricing pages and return (models_dict, schedules_dict)."""
    if not text:
        return {}, {}
    if "{% table %}" in text:
        return parse_gemini_markdown_pricing_with_schedules(text, as_of=as_of)
    if "<table" in text.lower():
        return _parse_gemini_html_pricing_with_schedules(text, as_of=as_of)
    return {}, {}


def parse_gemini_pricing(text: str, as_of: str = None) -> dict:
    """Tolerant parser for Google Gemini API pricing pages (Markdoc & HTML) with fail-closed gates."""
    models, _ = parse_gemini_pricing_with_schedules(text, as_of=as_of)
    return models


def parse_openai_pricing(text: str) -> dict:
    """Tolerant parser for OpenAI pricing pages with unit normalization & invariant gates."""
    results = {}
    if not text:
        return results

    try:
        unescaped = html.unescape(text)
        page_unit = detect_unit_multiplier(unescaped)

        # Official content negotiation can return Markdown, including all
        # service tiers. Read Standard by header names; positional parsing
        # confuses cache writes with output and fine-tuning with base inference.
        mode = "standard"
        headers = None
        fine_tuning = False
        for line in unescaped.splitlines():
            clean = line.strip()
            if clean.lower() == "finetuning":
                fine_tuning = True
            if clean in ("Cyber models", "Multimodal models", "Specialized models"):
                mode = "standard"
            tier = re.fullmatch(r"(?:### )?(Standard|Batch|Flex|Fast|Ultrafast)(?: pricing data)?", clean)
            if tier:
                mode = tier[1].lower()
            if not clean.startswith("|"):
                headers = None
                continue
            cells = [c.strip() for c in clean.strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", c) for c in cells):
                continue
            if headers is None:
                headers = cells
                continue
            if mode != "standard" or fine_tuning or len(cells) != len(headers):
                continue
            row = dict(zip(headers, cells))
            if "Modality" in row or row.get("Category") in ("Embedding", "Moderation"):
                continue  # These cannot be flattened into a text-only bill.
            name = row.get("Model", "")
            input_cell = row.get("Short context input", row.get("Input", ""))
            output_cell = row.get("Short context output", row.get("Output", ""))
            cached_cell = row.get("Short context cached input", row.get("Cached input", "-"))
            parsed_cells = [re.fullmatch(r"\$([0-9]+(?:\.[0-9]+)?)", c) for c in (input_cell, output_cell, cached_cell)]
            if not name or not parsed_cells[0] or not parsed_cells[1]:
                continue
            inp, out = float(parsed_cells[0][1]), float(parsed_cells[1][1])
            cache = float(parsed_cells[2][1]) if parsed_cells[2] else 0.0
            if cached_cell != "-" and not parsed_cells[2]:
                continue
            key = normalize_model_name(name)
            # A published '-' is unavailable caching, not an inferred 10% rate.
            if cache == 0.0:
                valid = 0 < inp <= out < 1000 and cached_cell == "-"
            else:
                valid = validate_live_price(key, inp, out, cache)[0]
            if valid and key not in results:
                results[key] = (inp, out, cache)
                if "." in key:
                    results[key.replace(".", "-")] = results[key]

        # 1. Parse React / Next.js hydration props
        pattern = re.compile(
            r'\[0,\s*"([^"]+?)"\]\s*,\s*'
            r'\[0,\s*([0-9]+(?:\.[0-9]+)?|-|"-")\]\s*,\s*'
            r'\[0,\s*([0-9]+(?:\.[0-9]+)?|-|"-")\]\s*,\s*'
            r'\[0,\s*([0-9]+(?:\.[0-9]+)?|-|"-")\]\s*,\s*'
            r'\[0,\s*([0-9]+(?:\.[0-9]+)?)\]'
        )
        for m in pattern.finditer(unescaped):
            m_name, m_inp, m_cached, m_write, m_out = m.groups()
            if m_inp in ('-', '"-"') or m_out in ('-', '"-"'):
                continue
            try:
                # OpenAI hydration tables are specified per 1M tokens by default
                unit_mult = page_unit if page_unit is not None else 1.0
                inp = float(m_inp) * unit_mult
                out = float(m_out) * unit_mult
                cache = (float(m_cached) * unit_mult) if m_cached not in ('-', '"-"') else 0.0
                key = normalize_model_name(m_name)
                if key and key not in results:
                    ok, reason = validate_live_price(key, inp, out, cache)
                    if not ok:
                        logger.warning("Rejecting %s: %s", key, reason)
                        continue
                    val = (inp, out, cache)
                    results[key] = val
                    if "." in key:
                        results[key.replace(".", "-")] = val
            except ValueError:
                continue

        # 2. Parse HTML tables
        tables = re.findall(r'<table[\s\S]*?</table>', unescaped, re.IGNORECASE)
        for t in tables:
            # Multimodal and fine-tuning tables do not describe one text SKU.
            if re.search(r'<th\b[^>]*>\s*(?:Modality|Training)\s*</th>', t, re.I):
                continue
            table_unit = detect_unit_multiplier(t) or page_unit
            rows = re.findall(r'<tr[\s\S]*?</tr>', t, re.IGNORECASE)
            for r in rows:
                cells = [re.sub(r'<[^>]+>', ' ', c).strip() for c in re.findall(r'<t[dh][\s\S]*?</t[dh]>', r, re.IGNORECASE)]
                if len(cells) < 3:
                    continue
                for i, c in enumerate(cells):
                    clean_c = c.strip()
                    if re.match(r'^(?:gpt-|chat-|o[0-9]|sora-|dall-)', clean_c, re.I):
                        tail = cells[i + 1:]
                        row_unit = detect_unit_multiplier(" ".join(tail)) or table_unit or 1.0
                        dollars = []
                        for tc in tail:
                            m_d = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', tc)
                            if m_d:
                                dollars.append(float(m_d[0]) * row_unit)
                            elif tc in ('-', ''):
                                dollars.append(None)
                        if len(dollars) >= 2:
                            inp = dollars[0]
                            if inp is None:
                                break
                            cache = dollars[1] if (len(dollars) >= 3 and dollars[1] is not None) else round(inp * 0.1, 4)
                            out = dollars[3] if (len(dollars) >= 4 and dollars[3] is not None) else (
                                dollars[2] if (len(dollars) >= 3 and dollars[2] is not None) else dollars[1]
                            )
                            if out is None:
                                break
                            key = normalize_model_name(clean_c)
                            if key and key not in results:
                                ok, reason = validate_live_price(key, inp, out, cache)
                                if not ok:
                                    logger.warning("Rejecting %s: %s", key, reason)
                                    continue
                                val = (inp, out, cache)
                                results[key] = val
                                if "." in key:
                                    results[key.replace(".", "-")] = val
                        break
    except Exception as e:
        logger.warning("OpenAI pricing parse error: %s; degraded to no-data", e)

    return results


# --- Cache Management & Temporal Integrity -----------------------------------

def load_cache(cache_path: Path = None) -> tuple[dict, bool]:
    """Load cached pricing data. Returns (models_dict, is_valid_bool)."""
    global _MEMORY_CACHE, _MEMORY_QUARANTINED, _MEMORY_SCHEDULES, _MEMORY_HISTORY, _MEMORY_FETCHED_AT

    path = cache_path or get_cache_path()
    if not path.is_file():
        return ({}, False)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        fetched_at_str = data.get("fetched_at")
        if not fetched_at_str:
            return ({}, False)

        try:
            fetched_dt = datetime.datetime.fromisoformat(fetched_at_str)
            if fetched_dt.tzinfo is None:
                fetched_dt = fetched_dt.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            age_hours = (now - fetched_dt).total_seconds() / 3600.0
        except Exception:
            age_hours = 999999.0

        ttl_hours = get_ttl_hours()
        is_valid = age_hours <= ttl_hours

        models_raw = data.get("models")
        if models_raw is None:
            models_raw = {
                k: v for k, v in data.items()
                if k not in ("fetched_at", "source_url", "sources", "history", "quarantined", "schedules")
            }

        models = {}
        for k, v in models_raw.items():
            if isinstance(v, (list, tuple)) and len(v) >= 3:
                models[k] = (float(v[0]), float(v[1]), float(v[2]))
            elif isinstance(v, dict):
                inp = float(v.get("input", 0.0))
                out = float(v.get("output", 0.0))
                cache = float(v.get("cache_read", round(inp * 0.1, 4)))
                models[k] = (inp, out, cache)

        quarantined_raw = data.get("quarantined") or {}
        quarantined = {}
        for k, v in quarantined_raw.items():
            if isinstance(v, (list, tuple)) and len(v) >= 3:
                quarantined[k] = (float(v[0]), float(v[1]), float(v[2]), QUARANTINED)

        schedules_raw = data.get("schedules") or {}
        schedules = {}
        for k, bands in schedules_raw.items():
            if isinstance(bands, list):
                parsed_bands = []
                for band in bands:
                    if isinstance(band, (list, tuple)) and len(band) == 3:
                        b_start, b_end, p = band
                        if isinstance(p, (list, tuple)) and len(p) >= 3:
                            parsed_bands.append((b_start, b_end, (float(p[0]), float(p[1]), float(p[2]), CACHED)))
                if parsed_bands:
                    schedules[k] = parsed_bands

        history = data.get("history") or []

        _MEMORY_CACHE = models
        _MEMORY_QUARANTINED = quarantined
        _MEMORY_SCHEDULES = schedules
        _MEMORY_HISTORY = history
        _MEMORY_FETCHED_AT = fetched_at_str
        return (models, is_valid)
    except json.JSONDecodeError as e:
        # Corrupt cache: move it aside once so every later load does not re-parse
        # the same bad bytes, and so the next fetch regenerates a clean cache.
        quarantine = path.with_name(path.name + ".corrupt-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        try:
            os.replace(path, quarantine)
            logger.warning("Pricing cache at %s was corrupt (%s); moved to %s", path, e, quarantine)
        except OSError as move_err:
            logger.warning("Pricing cache at %s is corrupt (%s) and could not be moved aside: %s", path, e, move_err)
        return ({}, False)
    except Exception as e:
        logger.warning("Failed to load pricing cache at %s: %s", path, e)
        return ({}, False)


def save_cache(
    models: dict,
    source_urls: dict = None,
    quarantined: dict = None,
    cache_path: Path = None,
    schedules: dict = None,
) -> None:
    """Save pricing models, quarantine records, dated schedules, and append to history atomically."""
    global _MEMORY_CACHE, _MEMORY_QUARANTINED, _MEMORY_SCHEDULES, _MEMORY_HISTORY, _MEMORY_FETCHED_AT
    path = cache_path or get_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Load existing history if available
        existing_history = list(_MEMORY_HISTORY)
        existing_quarantined = dict(_MEMORY_QUARANTINED)
        existing_schedules = dict(_MEMORY_SCHEDULES)
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                    if "history" in old_data and isinstance(old_data["history"], list):
                        existing_history = old_data["history"]
                    elif old_data.get("fetched_at") and old_data.get("models"):
                        existing_history = [{
                            "fetched_at": old_data["fetched_at"],
                            "models": old_data["models"],
                        }]
                    if "quarantined" in old_data and isinstance(old_data["quarantined"], dict):
                        existing_quarantined.update(old_data["quarantined"])
                    if "schedules" in old_data and isinstance(old_data["schedules"], dict):
                        for sk, sbands in old_data["schedules"].items():
                            if isinstance(sbands, list) and sk not in existing_schedules:
                                existing_schedules[sk] = sbands
            except Exception:
                pass

        if quarantined:
            existing_quarantined.update({k: list(v[:3]) for k, v in quarantined.items()})

        if schedules:
            for sk, sbands in schedules.items():
                existing_schedules[sk] = [[b[0], b[1], list(b[2][:3])] for b in sbands]

        serialized_schedules = {}
        for sk, sbands in existing_schedules.items():
            serialized_schedules[sk] = [[b[0], b[1], list(b[2][:3])] for b in sbands]

        # Append new snapshot to temporal history
        new_snapshot = {
            "fetched_at": now_iso,
            "source_url": source_urls or {},
            "models": {k: list(v[:3]) for k, v in models.items()},
        }
        existing_history.append(new_snapshot)

        payload = {
            "fetched_at": now_iso,
            "source_url": source_urls or {},
            "models": {k: list(v[:3]) for k, v in models.items()},
            "quarantined": existing_quarantined,
            "schedules": serialized_schedules,
            "history": existing_history,
        }

        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        # Atomic replace with fallback for Windows file locks
        try:
            os.replace(tmp_path, path)
        except OSError:
            import time
            time.sleep(0.05)
            try:
                os.replace(tmp_path, path)
            except OSError as replace_err:
                # Never write the live cache in place: a truncated write here is
                # what produced the corrupt cache. Keep the last good file and the
                # tmp copy for inspection; the next save retries the atomic replace.
                logger.warning("Pricing cache replace failed, keeping previous cache: %s", replace_err)
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

        _MEMORY_CACHE = models
        _MEMORY_QUARANTINED = existing_quarantined
        _MEMORY_SCHEDULES = {
            sk: [(b[0], b[1], (float(b[2][0]), float(b[2][1]), float(b[2][2]), CACHED)) for b in sbands]
            for sk, sbands in serialized_schedules.items()
        }
        _MEMORY_HISTORY = existing_history
        _MEMORY_FETCHED_AT = now_iso
        logger.info("Saved pricing cache to %s (%d active, %d quarantined, %d schedules, %d history snapshots)",
                    path, len(models), len(existing_quarantined), len(serialized_schedules), len(existing_history))
    except Exception as e:
        logger.warning("Failed to save pricing cache to %s: %s", path, e)


def resolve_historical_cache_price(
    key: str,
    when: str | datetime.date | datetime.datetime,
    cache_path: Path = None,
) -> tuple | None:
    """Find the latest valid cached price fetched at or before `when`.

    Returns (inp, out, cache_read, CACHED) or None.
    A new live price fetched today will never match a past day, preserving
    temporal integrity.
    """
    if when is None:
        return None
    day = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else str(when)[:10]
    if len(day) != 10:
        return None

    path = cache_path or get_cache_path()
    if not path.is_file():
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        history = data.get("history") or []
        if not history and data.get("fetched_at") and data.get("models"):
            history = [{
                "fetched_at": data["fetched_at"],
                "models": data["models"],
            }]

        eligible = []
        for snap in history:
            fa = snap.get("fetched_at")
            if fa and fa[:10] <= day:
                eligible.append((fa, snap.get("models", {})))

        if not eligible:
            return None

        # Pick latest snapshot at or before `day`
        eligible.sort(key=lambda x: x[0])
        latest_models = eligible[-1][1]

        if key in latest_models:
            m = latest_models[key]
            return (float(m[0]), float(m[1]), float(m[2]), CACHED)

        alt_key = key.replace("-", ".") if "-" in key else key.replace(".", "-")
        if alt_key in latest_models:
            m = latest_models[alt_key]
            return (float(m[0]), float(m[1]), float(m[2]), CACHED)

        return None
    except Exception as e:
        logger.warning("Error resolving historical cache price at %s: %s", path, e)
        return None


def get_last_known_good_price(key: str, cache_path: Path = None) -> tuple | None:
    """Retrieve last-known-good rate from expired cache (Hop 4 in ladder)."""
    path = cache_path or get_cache_path()
    if not path.is_file():
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        models = data.get("models") or {}
        if key in models:
            m = models[key]
            return (float(m[0]), float(m[1]), float(m[2]), EXPIRED_CACHE)

        alt_key = key.replace("-", ".") if "-" in key else key.replace(".", "-")
        if alt_key in models:
            m = models[alt_key]
            return (float(m[0]), float(m[1]), float(m[2]), EXPIRED_CACHE)

        for snap in reversed(data.get("history") or []):
            sm = snap.get("models") or {}
            if key in sm:
                m = sm[key]
                return (float(m[0]), float(m[1]), float(m[2]), EXPIRED_CACHE)

        return None
    except Exception:
        return None


# --- Fetch Execution ---------------------------------------------------------

def fetch_vendor_pricing(vendor: str, timeout_s: float = None) -> tuple[dict, str]:
    """Fetch and parse pricing from a vendor's official documentation URL."""
    if vendor in _ATTEMPTED_VENDORS:
        return ({}, "")
    _ATTEMPTED_VENDORS.add(vendor)

    timeout = timeout_s if timeout_s is not None else get_fetch_timeout_s()
    headers_base = {
        "User-Agent": "NouGenTracker/1.0 (pricing resolver; +https://github.com/WhoVisions/NouGenTracker)",
    }

    vendor_entry = next((v for v in VENDOR_SOURCES if v[0] == vendor), None)
    if not vendor_entry:
        return ({}, "")

    for url, extra_headers in vendor_entry[1]:
        hdrs = dict(headers_base)
        hdrs.update(extra_headers)
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_bytes = resp.read()
                text = raw_bytes.decode("utf-8", errors="replace")

            parsed = {}
            if vendor == "anthropic":
                parsed = parse_anthropic_pricing(text)
            elif vendor == "gemini":
                parsed, parsed_scheds = parse_gemini_pricing_with_schedules(text)
                if parsed_scheds:
                    _LAST_EXTRACTED_SCHEDULES.update(parsed_scheds)
            elif vendor == "openai":
                parsed = parse_openai_pricing(text)

            if parsed:
                logger.info("Successfully fetched and parsed %s pricing from %s (%d models)", vendor, url, len(parsed))
                return (parsed, url)
        except Exception as e:
            logger.info("Pricing fetch offline/failed for %s (%s): %s; silent fallback", vendor, url, e)

    return ({}, "")


def fetch_all_vendors(
    timeout_s: float = None,
    cache_path: Path = None,
    baseline_pricing: dict = None,
) -> dict:
    """Fetch all vendors in authority order, apply Delta Guard, and persist to cache."""
    existing_models, _ = load_cache(cache_path)
    combined = dict(existing_models)
    quarantined = dict(_MEMORY_QUARANTINED)
    source_urls = {}
    _LAST_EXTRACTED_SCHEDULES.clear()

    # Build baseline for delta guard: existing cache > fallback constants
    baseline = dict(baseline_pricing or {})
    baseline.update(existing_models)

    for vendor, _ in VENDOR_SOURCES:
        parsed, url = fetch_vendor_pricing(vendor, timeout_s=timeout_s)
        if parsed:
            source_urls[vendor] = url
            for k, fresh in parsed.items():
                last_known = baseline.get(k)
                passes_delta, reason = check_delta_guard(k, fresh, last_known)
                if passes_delta:
                    combined[k] = fresh
                else:
                    logger.warning(
                        "DELTA GUARD TRIGGERED for %s: %s. Quarantining new price %s under 'doc-live-quarantined'; keeping old rate for billing.",
                        k, reason, fresh
                    )
                    quarantined[k] = (fresh[0], fresh[1], fresh[2], QUARANTINED)
                    if last_known:
                        combined[k] = (last_known[0], last_known[1], last_known[2])

    schedules = dict(_LAST_EXTRACTED_SCHEDULES)
    if combined or quarantined or schedules:
        save_cache(combined, source_urls=source_urls, quarantined=quarantined, cache_path=cache_path, schedules=schedules)

    global _MEMORY_CACHE
    _MEMORY_CACHE = combined
    return combined


# --- Resolution Logic (Fail-Proof Ladder) -----------------------------------

def _check_env_override(key: str) -> tuple | None:
    """Tier (a) / Hop 1: Env override NOUGEN_PRICE_<MODELKEY>="in,out,cacheread"."""
    norm_upper = key.upper().replace("-", "_").replace(".", "_")
    norm_clean = re.sub(r'[^A-Z0-9_]+', '_', norm_upper).strip('_')
    env_keys = [
        f"NOUGEN_PRICE_{norm_clean}",
        f"NOUGEN_PRICE_{key.replace('.', '_')}",
        f"NOUGEN_PRICE_{key}",
    ]
    for ek in env_keys:
        env_val = os.environ.get(ek)
        if env_val:
            try:
                parts = [float(p.strip()) for p in env_val.split(",") if p.strip()]
                if len(parts) >= 2:
                    inp = parts[0]
                    out = parts[1]
                    cache_read = parts[2] if len(parts) >= 3 else round(inp * 0.1, 4)
                    res = (inp, out, cache_read, ENV)
                    logger.info("[tier a / hop 1] Env override for %s (%s=%r) -> %s", key, ek, env_val, res)
                    return res
            except ValueError:
                logger.warning("Invalid pricing format in %s=%r", ek, env_val)
    return None


def _lookup_price_schedule(price_schedule: dict, key: str, when) -> tuple | None:
    """Lookup dated price schedule entries."""
    if not price_schedule or when is None:
        return None
    entries = price_schedule.get(key)
    if not entries:
        alt_key = key.replace("-", ".") if "-" in key else key.replace(".", "-")
        entries = price_schedule.get(alt_key)
    if not entries:
        return None
    day = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else str(when)[:10]
    if len(day) != 10:
        return None
    for start, end, price in entries:
        if start <= day and (end is None or day <= end):
            return price
    return None


def resolve_exact_price(
    key: str,
    fallback_pricing: dict = None,
    cache_path: Path = None,
    when=None,
    price_schedule: dict = None,
) -> tuple | None:
    """Resolve exact model price through ladder hops (1) -> (2) -> (3) -> (4) -> (5).
    Returns (inp, out, cache_read, source_tag) or None."""
    if not key:
        return None

    # Hop 1: Env override
    env_hit = _check_env_override(key)
    if env_hit is not None:
        return env_hit

    # Hop 2: Cache lookup (temporal or valid within TTL)
    if when is not None:
        # PRICE_SCHEDULE first: first-party dated transitions ALWAYS outrank cached
        # snapshots for models they cover on that date. A snapshot taken inside one
        # price band must never answer for a date in a different band (e.g. today's
        # intro-price snapshot answering for a post-intro date).
        if price_schedule:
            sched = _lookup_price_schedule(price_schedule, key, when)
            if sched is not None:
                logger.info("[ladder hop 2] PRICE_SCHEDULE hit for %s at %s -> %s", key, when, sched)
                return sched
        # Live/cached extracted schedules outrank flat snapshots when a date crosses a published band
        _, is_valid = load_cache(cache_path)
        if _MEMORY_SCHEDULES:
            cached_sched = _lookup_price_schedule(_MEMORY_SCHEDULES, key, when)
            if cached_sched is not None:
                logger.info("[ladder hop 2] Cached schedule hit for %s at %s -> %s", key, when, cached_sched)
                return cached_sched
        day_str = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else str(when)[:10]
        today_str = datetime.date.today().isoformat()
        if not is_valid and len(day_str) == 10 and day_str >= today_str:
            unattempted = [v for v, _ in VENDOR_SOURCES if v not in _ATTEMPTED_VENDORS]
            if unattempted:
                fetched = fetch_all_vendors(cache_path=cache_path, baseline_pricing=fallback_pricing)
                if _MEMORY_SCHEDULES:
                    live_sched = _lookup_price_schedule(_MEMORY_SCHEDULES, key, when)
                    if live_sched is not None:
                        inp, out, cache_read = live_sched[:3]
                        return (inp, out, cache_read, LIVE)
                if key in fetched:
                    inp, out, cache_read = fetched[key][:3]
                    return (inp, out, cache_read, LIVE)
        hist_hit = resolve_historical_cache_price(key, when, cache_path=cache_path)
        if hist_hit is not None:
            logger.info("[ladder hop 2] Historical cache hit for %s at %s -> %s", key, when, hist_hit)
            return hist_hit
        # Dated call fallback constant
        if fallback_pricing and key in fallback_pricing:
            entry = fallback_pricing[key]
            source = entry[3] if len(entry) > 3 and entry[3] in (EST, "doc-text-only") else FALLBACK_CONST
            res = (entry[0], entry[1], entry[2], source)
            logger.info("[ladder hop 5] Fallback constant for %s -> %s", key, res)
            return res
        return None

    # Undated, schedule-covered model: the flat table IS the undated contract
    # ("callers that do not care about history keep the behaviour they had" -
    # post-intro list rates). Live/cached snapshots reflect whatever band is
    # current today and would silently swing the undated answer across a
    # scheduled transition, so they are skipped for these models.
    if price_schedule and key in price_schedule and fallback_pricing and key in fallback_pricing:
        entry = fallback_pricing[key]
        res = (entry[0], entry[1], entry[2], entry[3] if len(entry) > 3 else FALLBACK_CONST)
        logger.info("[ladder hop 2] Undated schedule-covered model %s -> flat table %s", key, res)
        return res

    # Undated: Valid cache within TTL (with active schedule precedence across transitions)
    cached_models, is_valid = load_cache(cache_path)
    if is_valid:
        if _MEMORY_SCHEDULES:
            today_str = datetime.date.today().isoformat()
            sched_now = _lookup_price_schedule(_MEMORY_SCHEDULES, key, today_str)
            if sched_now is not None:
                logger.info("[ladder hop 2] Active schedule hit for %s (%s) -> %s", key, today_str, sched_now)
                return sched_now
        if key in cached_models:
            inp, out, cache_read = cached_models[key][:3]
            res = (inp, out, cache_read, CACHED)
            logger.info("[ladder hop 2] Valid cache hit for %s -> %s", key, res)
            return res
        alt_key = key.replace("-", ".") if "-" in key else key.replace(".", "-")
        if alt_key in cached_models:
            inp, out, cache_read = cached_models[alt_key][:3]
            res = (inp, out, cache_read, CACHED)
            logger.info("[ladder hop 2] Valid cache hit for %s (via %s) -> %s", key, alt_key, res)
            return res

    # Hop 3: Live fetch (through gates 1-3: normalization, invariants, delta guard)
    unattempted = [v for v, _ in VENDOR_SOURCES if v not in _ATTEMPTED_VENDORS]
    if unattempted:
        fetched = fetch_all_vendors(cache_path=cache_path, baseline_pricing=fallback_pricing)
        if key in fetched:
            inp, out, cache_read = fetched[key][:3]
            res = (inp, out, cache_read, LIVE)
            logger.info("[ladder hop 3] Live fetch resolved %s -> %s", key, res)
            return res
        alt_key = key.replace("-", ".") if "-" in key else key.replace(".", "-")
        if alt_key in fetched:
            inp, out, cache_read = fetched[alt_key][:3]
            res = (inp, out, cache_read, LIVE)
            logger.info("[ladder hop 3] Live fetch resolved %s (via %s) -> %s", key, alt_key, res)
            return res

    # Hop 4: LAST-KNOWN-GOOD expired cache (validated entries only)
    expired_hit = get_last_known_good_price(key, cache_path=cache_path)
    if expired_hit is not None:
        logger.info("[ladder hop 4] Last-known-good expired cache for %s -> %s", key, expired_hit)
        return expired_hit

    # Hop 5: MODEL_PRICING fallback constant
    if fallback_pricing and key in fallback_pricing:
        entry = fallback_pricing[key]
        source = entry[3] if len(entry) > 3 and entry[3] in (EST, "doc-text-only") else FALLBACK_CONST
        res = (entry[0], entry[1], entry[2], source)
        logger.info("[ladder hop 5] Fallback constant for %s -> %s", key, res)
        return res

    return None


def resolve_price(
    model_name: str,
    when=None,
    fallback_pricing: dict = None,
    default_pricing: tuple = None,
    variant_suffixes: tuple = None,
    cache_path: Path = None,
    price_schedule: dict = None,
) -> tuple:
    """Full fail-proof pricing resolution function for NouGenTracker.

    Pipeline:
      0. Free lanes check
      1. Env override NOUGEN_PRICE_<MODELKEY>
      2. Valid cache (or dated cache if `when` is given)
      3. Live fetch (through gates 1-3)
      4. LAST-KNOWN-GOOD expired cache
      5. MODEL_PRICING const & PRICE_SCHEDULE
      6. Variant family inference
      7. Unknown-model default (DEFAULT_UNPRICED)

    The pipeline NEVER raises out of pricing — a total pricing failure returns
    default pricing tagged 'default-unpriced'.
    """
    try:
        key = normalize_model_name(model_name)
        if not key:
            default = default_pricing or (1.00, 4.00, 0.100, DEFAULT_UNPRICED)
            return (default[0], default[1], default[2], DEFAULT_UNPRICED)

        # Free lanes: local Ollama/Gemma + OpenRouter ':free' routes
        if key.endswith(":free") or key in (
            "sol-ai:e4b", "dav1d:e2b", "kaedra:e4b", "iris-ai:e4b",
            "gemma4-aggressive:e4b", "gemma4-aggressive:e2b", "gemma2:2b", "gemma:2b"
        ):
            return (0.0, 0.0, 0.0, DOC)

        # Claude Code writes "<synthetic>" for harness-generated turns that no
        # API call produced; normalization turns it into "synthetic". Never billed.
        if key == "synthetic":
            return (0.0, 0.0, 0.0, DOC)

        # "-thinking" is a mode label, not a SKU: price it as the base model.
        if key.startswith("claude-") and key.endswith("-thinking"):
            key = key[: -len("-thinking")]

        exact = resolve_exact_price(
            key,
            fallback_pricing=fallback_pricing,
            cache_path=cache_path,
            when=when,
            price_schedule=price_schedule,
        )
        if exact is not None:
            return exact

        # Family inference for variant suffixes (e.g. gemini-3.6-flash-high -> gemini-3.6-flash)
        # and point releases (e.g. claude-fable-5-1 -> claude-fable-5): a static
        # table entry for the parent model existed, but ".1"/"-1" never matched
        # it, so claude-fable-5-1, claude-mythos-5-1 and claude-opus-5-5 fell
        # all the way to the $1/$4/$0.1 unknown-model default instead of the
        # parent's real rate. Point releases are Anthropic's own convention
        # for "same tier, same price" unless a dated PRICE_SCHEDULE entry (hop
        # 2, checked before this ever runs) says otherwise, so this is a safe
        # last resort, not a guess at a new number.
        suffixes = variant_suffixes or (
            "high", "xhigh", "max", "ultra", "medium", "low", "minimal", "thinking", "latest", "preview", "customtools"
        )
        parts = key.split("-")
        while len(parts) > 1 and (parts[-1].lower() in suffixes or parts[-1].isdigit()):
            parts = parts[:-1]
            base_key = "-".join(parts)
            found = resolve_exact_price(
                base_key,
                fallback_pricing=fallback_pricing,
                cache_path=cache_path,
                when=when,
                price_schedule=price_schedule,
            )
            if found is not None:
                inp, out, cache_read, _ = found
                res = (inp, out, cache_read, EST)
                logger.info("[family inference] Variant %s inferred from %s -> %s", key, base_key, res)
                return res

        # Unknown model: fall through to default with DEFAULT_UNPRICED tag
        default = default_pricing if default_pricing is not None else (1.00, 4.00, 0.100, DEFAULT_UNPRICED)
        res = (default[0], default[1], default[2], DEFAULT_UNPRICED)
        logger.info("[default-unpriced] Unknown model %s; default -> %s", key, res)
        return res
    except Exception as e:
        logger.error("Pricing resolution failure for %s: %s; falling back to default-unpriced", model_name, e)
        default = default_pricing or (1.00, 4.00, 0.100, DEFAULT_UNPRICED)
        return (default[0], default[1], default[2], DEFAULT_UNPRICED)


def reset_process_state():
    """Reset process-level state (for unit testing)."""
    global _ATTEMPTED_VENDORS, _MEMORY_CACHE, _MEMORY_QUARANTINED, _MEMORY_SCHEDULES, _LAST_EXTRACTED_SCHEDULES, _MEMORY_HISTORY, _MEMORY_FETCHED_AT
    _ATTEMPTED_VENDORS.clear()
    _MEMORY_CACHE.clear()
    _MEMORY_QUARANTINED.clear()
    _MEMORY_SCHEDULES.clear()
    _LAST_EXTRACTED_SCHEDULES.clear()
    _MEMORY_HISTORY.clear()
    _MEMORY_FETCHED_AT = None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    print("--- NouGen Pricing Resolver Diagnostics ---")
    cache_f = get_cache_path()
    ttl = get_ttl_hours()
    print(f"Cache path: {cache_f}")
    print(f"TTL hours: {ttl}")
    models_cached, valid = load_cache()
    print(f"Cache status: {'VALID' if valid else 'EXPIRED/MISSING'} ({len(models_cached)} models cached)")
    if len(sys.argv) > 1:
        target = sys.argv[1]
        resolved = resolve_price(target)
        print(f"Resolved {target}: {resolved}")
