#!/usr/bin/env python3
"""Build the fleet dashboard's summary from the committed dailies.

This is the re-cut of the earlier relay work (PR #1). The relay transport
itself — rollup files pushed to a side branch — is superseded: the dailies
under ``dailies/<machine>/`` travel with the repo and are already the fleet's
shared record. What was worth rescuing is the page (``dashboard.py``, kept
verbatim) and the summary it renders. This module feeds it from the dailies,
so there is exactly one usage transport in this project.

Field note: dailies store token buckets as ``cache_creation`` / ``cache_read``
/ ``reasoning``; ``token_tracker.model_bill`` prices the API's field names
(``cache_creation_input_tokens`` …). ``_bill`` maps between them — the dailies
are the storage format, the invoice names are the pricing format.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pricing_live import calculate_cost, round_to_cents
import token_tracker as tt

logger = logging.getLogger("fleet_summary")

DAILIES = Path(__file__).resolve().parent / "dailies"
BUCKET_FIELDS = ("input_tokens", "output_tokens", "cache_creation",
                 "cache_read", "reasoning")
STALE_HOURS = float(os.environ.get("NOUGEN_FLEET_STALE_HOURS", "48"))


@dataclass
class Overlap:
    machine_a: str
    machine_b: str
    day_a: str
    day_b: str
    similarity: float


def validate_and_clamp_accounting(
    cold: float,
    realistic: float,
    paid: float,
) -> Tuple[float, float, float, float]:
    """Enforce accounting invariants:
    a. cold >= cached_realistic >= 0 and paid >= 0.
    b. absorbed == cold - paid, never displayed negative (clamp to 0 with logged warning).
    c. Evaluated using Decimal arithmetic.

    Returns:
      (cold_clamped, realistic_clamped, paid_clamped, absorbed)
    """
    if paid < 0:
        logger.warning("Negative paid spend (%s); clamping to 0.0", paid)
        paid = 0.0

    if realistic < 0:
        logger.warning("Negative realistic cost (%s); clamping to 0.0", realistic)
        realistic = 0.0

    if cold < realistic:
        logger.warning(
            "Accounting inconsistency: cold cost (%s) < realistic cost (%s); clamping cold to realistic",
            cold, realistic
        )
        cold = realistic

    cold_dec = round_to_cents(Decimal(str(cold)))
    realistic_dec = round_to_cents(Decimal(str(realistic)))
    paid_dec = round_to_cents(Decimal(str(paid)))
    absorbed_dec = cold_dec - paid_dec

    if absorbed_dec < Decimal("0.00"):
        logger.warning(
            "Accounting inconsistency: cold (%s) < paid (%s); clamping absorbed to 0.0",
            cold, paid
        )
        absorbed_dec = Decimal("0.00")

    absorbed = float(round_to_cents(absorbed_dec))
    return (float(cold_dec), float(realistic_dec), float(paid_dec), absorbed)


@dataclass
class FleetSummary:
    machines: Dict[str, Dict[str, Any]]
    days: List[str]
    total_cost: float
    total_tokens: int
    cache_read: int
    confidence: Optional[float]
    inferred: List[str]
    overlaps: List[Overlap]
    freshness: List[Tuple[str, Optional[datetime], float, bool]]
    local: str
    cold_cost: float = 0.0
    paid_cost: float = 0.0
    absorbed_cost: float = 0.0
    exact_tokens: int = 0
    estimated_tokens: int = 0

    @property
    def is_estimated(self) -> bool:
        return (
            self.estimated_tokens > 0
            or (self.confidence is not None and self.confidence < 0.9999)
            or bool(self.inferred)
        )

    @property
    def cache_share(self) -> float:
        return self.cache_read / self.total_tokens if self.total_tokens else 0.0

    @property
    def busiest_day(self) -> Optional[Tuple[str, float]]:
        per_day: Dict[str, float] = defaultdict(float)
        for data in self.machines.values():
            for day, cost in data["days"].items():
                per_day[day] += cost
        if not per_day:
            return None
        return max(per_day.items(), key=lambda kv: kv[1])


def local_machine() -> str:
    """The exporter's naming: NOUGEN_MACHINE, else the slugged hostname."""
    explicit = os.environ.get("NOUGEN_MACHINE", "").strip()
    if explicit:
        return explicit
    try:
        host = socket.gethostname()
    except OSError:
        return "unknown-machine"
    return host.lower().replace(".", "-")


def _bill(model: str, bucket: Dict[str, int], when: str) -> float:
    """Price a dailies bucket with the tracker's invoice-shaped field names."""
    cost, _src = tt.model_bill(model, {
        "input_tokens": bucket.get("input_tokens", 0),
        "output_tokens": bucket.get("output_tokens", 0),
        "cache_creation_input_tokens": bucket.get("cache_creation", 0),
        "cache_read_input_tokens": bucket.get("cache_read", 0),
        "reasoning_tokens": bucket.get("reasoning", 0),
    }, when)
    return cost


def _bill_cold(model: str, bucket: Dict[str, int], when: str) -> float:
    """Price a dailies bucket cold: all input-side tokens fresh, reasoning as output, no cache discount."""
    inp, out, _cr, _src = tt.price_for(model, when)
    i = int(bucket.get("input_tokens", 0))
    cc = int(bucket.get("cache_creation", 0))
    cr = int(bucket.get("cache_read", 0))
    o = int(bucket.get("output_tokens", 0))
    rt = int(bucket.get("reasoning", 0))
    cost = calculate_cost(
        input_tokens=(i + cc + cr),
        output_tokens=(o + rt),
        inp_rate=inp,
        out_rate=out,
    )
    return float(cost)


def _tokens(bucket: Dict[str, int]) -> int:
    return sum(int(bucket.get(f, 0)) for f in BUCKET_FIELDS)


def _read_dailies(root: Path) -> List[Dict[str, Any]]:
    records = []
    for path in sorted(root.glob("*/[0-9]*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("date") and data.get("machine"):
            records.append(data)
    return records


def fleet_summary(days: Optional[int] = None,
                  root: Path = DAILIES,
                  now: Optional[datetime] = None,
                  paid: Optional[float] = None) -> FleetSummary:
    """Everything the fleet has published, folded into one view.

    Includes THIS machine: a fleet report that omits the box running it is
    the same mistake as a machine that cannot see its peers.
    """
    now = now or datetime.now(timezone.utc)
    records = _read_dailies(root)
    if days:
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        records = [r for r in records if r["date"] >= cutoff]

    machines: Dict[str, Dict[str, Any]] = {}
    days_seen: set = set()
    latest: Dict[str, datetime] = {}
    total_cost = total_cold = total_tokens = cache_read = 0
    exact_tokens = estimated_tokens = 0
    estimated_by: set = set()
    # (day, model, exact-bucket tuple) -> machines reporting it, for the
    # double-counting flag: two boxes publishing identical nonzero buckets
    # for the same model-day almost certainly counted the same calls.
    fingerprints: Dict[Tuple[str, str, Tuple[int, ...]], List[str]] = defaultdict(list)

    for rec in records:
        machine, day = rec["machine"], rec["date"]
        entry = machines.setdefault(machine, {
            "cost": 0.0, "cold_cost": 0.0, "tokens": 0, "calls": 0, "cache_read": 0,
            "days": defaultdict(float), "models": defaultdict(float),
        })
        days_seen.add(day)
        entry["calls"] += int(rec.get("invocations", 0))

        exact_tokens += _tokens(rec.get("exact", {}))
        est = _tokens(rec.get("estimated", {}))
        estimated_tokens += est
        if est:
            estimated_by.add(machine)

        for model, bucket in (rec.get("models") or {}).items():
            cost = _bill(model, bucket, day)
            cold = _bill_cold(model, bucket, day)
            tokens = _tokens(bucket)
            entry["cost"] += cost
            entry["cold_cost"] = entry.get("cold_cost", 0.0) + cold
            entry["tokens"] += tokens
            entry["cache_read"] += int(bucket.get("cache_read", 0))
            entry["days"][day] += cost
            entry["models"][model] += cost
            total_cost += cost
            total_cold += cold
            total_tokens += tokens
            cache_read += int(bucket.get("cache_read", 0))
            print_ = tuple(int(bucket.get(f, 0)) for f in BUCKET_FIELDS)
            if any(print_):
                fingerprints[(day, model, print_)].append(machine)

        stamp = rec.get("generated_at")
        if stamp:
            try:
                dt = datetime.fromisoformat(stamp)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if machine not in latest or dt > latest[machine]:
                    latest[machine] = dt
            except ValueError:
                pass

    overlaps = [
        Overlap(machine_a=who[0], machine_b=who[1], day_a=day, day_b=day,
                similarity=1.0)
        for (day, _model, _fp), who in sorted(fingerprints.items())
        if len(set(who)) > 1
    ]

    me = local_machine()
    freshness = []
    for machine in sorted(machines):
        if machine == me:
            continue
        ts = latest.get(machine)
        age = (now - ts).total_seconds() / 3600 if ts else float("inf")
        freshness.append((machine, ts, age, age > STALE_HOURS))

    if paid is None:
        # The configured subscription figure is MONTHLY, but this summary covers
        # however many distinct machine-days the dailies happen to span. Feeding
        # the raw monthly number into validate_and_clamp_accounting() made
        # `absorbed = cold - paid` subtract a full month of spend from a window
        # that might be two days, and the clamp above only checks the sign, not
        # the units, so the result looked verified while being incomparable.
        # Pro-rate to the days actually summarised. days_seen is the honest
        # window here: it counts days that really carry data, not calendar span,
        # so gaps in publication do not inflate the bill.
        import subscriptions as _subs
        _monthly, _ = _subs.monthly_total()
        paid = _subs.prorate(_monthly, len(days_seen))

    cold_clamped, realistic_clamped, paid_clamped, absorbed = validate_and_clamp_accounting(
        total_cold, total_cost, paid
    )

    measured = exact_tokens + estimated_tokens
    return FleetSummary(
        machines=machines,
        days=sorted(days_seen),
        total_cost=realistic_clamped,
        total_tokens=total_tokens,
        cache_read=cache_read,
        confidence=(exact_tokens / measured) if measured else None,
        inferred=sorted(estimated_by),
        overlaps=overlaps,
        freshness=freshness,
        local=me,
        cold_cost=cold_clamped,
        paid_cost=paid_clamped,
        absorbed_cost=absorbed,
        exact_tokens=exact_tokens,
        estimated_tokens=estimated_tokens,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the fleet dailies as one self-contained HTML page.")
    parser.add_argument("--days", type=int, default=None,
                        help="only include the last N days")
    parser.add_argument("--out", default="dashboard.html",
                        help="where to write the page")
    parser.add_argument("--title", default="NouGen fleet usage")
    parser.add_argument("--threshold", type=float,
                        default=float(os.environ.get("NOUGEN_ALERT_USD", "0")),
                        help="spend signal line in USD (0 = off)")
    args = parser.parse_args(argv)

    import dashboard as dash
    summary = fleet_summary(args.days)
    target = Path(os.path.expanduser(args.out))
    dash.write(summary, target, threshold=args.threshold, title=args.title)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --- canonical fleet counter -------------------------------------------------
#
# One number, one definition, every surface. The MCP connector's
# `total_activity/v1` summed four fields off the `exact` bucket only:
#
#     input_tokens + output_tokens + cache_read + cache_creation
#
# while this module's BUCKET_FIELDS has always counted five (reasoning too)
# across BOTH the exact and estimated buckets. Same window, same dailies, two
# irreconcilable headline totals -- for 2026-08-22..2026-08-29 that was
# 994,889,354 against a true blended 2,158,468,269. v1 was not slightly off,
# it was missing 53.9% of real throughput: all estimated usage (Antigravity
# lanes are almost entirely estimated) plus every reasoning token.
#
# v2 fixes the contract rather than the arithmetic: it reports the exact and
# estimated halves separately AND blended, so a consumer can never silently
# pick the narrow one. Zero must mean measured zero -- a lane with no telemetry
# is reported as stale, not as a lane that did no work.

CANONICAL_VERSION = "total_activity/v2"


def _bucket_split(rec: Dict[str, Any]) -> Tuple[Dict[str, int], Dict[str, int]]:
    """(exact, estimated) field maps for one daily, zero-filled."""
    out = []
    for key in ("exact", "estimated"):
        src = rec.get(key) or {}
        out.append({f: int(src.get(f, 0) or 0) for f in BUCKET_FIELDS})
    return out[0], out[1]


def canonical_summary(since: Optional[str] = None,
                      until: Optional[str] = None,
                      root: Path = DAILIES,
                      now: Optional[datetime] = None) -> Dict[str, Any]:
    """The one fleet throughput object every surface must render.

    `since`/`until` are inclusive YYYY-MM-DD. Omitting both sweeps everything
    published. Returns a versioned dict; see CANONICAL_VERSION.
    """
    now = now or datetime.now(timezone.utc)
    records = _read_dailies(root)
    if since:
        records = [r for r in records if r["date"] >= since]
    if until:
        records = [r for r in records if r["date"] <= until]

    lanes: Dict[str, Dict[str, Any]] = {}
    days_seen: set = set()

    for rec in records:
        machine = rec["machine"]
        lane = lanes.setdefault(machine, {
            "lane": machine,
            "days": 0,
            "invocations": 0,
            "exact": {f: 0 for f in BUCKET_FIELDS},
            "estimated": {f: 0 for f in BUCKET_FIELDS},
            "counters": set(),
            "generated_by": set(),
            "partial_days": [],
            "last_export": None,
        })
        exact, estimated = _bucket_split(rec)
        for f in BUCKET_FIELDS:
            lane["exact"][f] += exact[f]
            lane["estimated"][f] += estimated[f]
        lane["days"] += 1
        lane["invocations"] += int(rec.get("invocations", 0) or 0)
        days_seen.add(rec["date"])
        if rec.get("counter"):
            lane["counters"].add(rec["counter"])
        if rec.get("generated_by"):
            lane["generated_by"].add(rec["generated_by"])
        if rec.get("partial"):
            lane["partial_days"].append(rec["date"])
        stamp = rec.get("generated_at")
        if stamp:
            try:
                dt = datetime.fromisoformat(stamp)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if lane["last_export"] is None or dt > lane["last_export"]:
                    lane["last_export"] = dt
            except ValueError:
                pass

    warnings: List[str] = []
    lane_out: List[Dict[str, Any]] = []
    tot_exact = {f: 0 for f in BUCKET_FIELDS}
    tot_est = {f: 0 for f in BUCKET_FIELDS}
    tot_inv = 0

    # A lane can only be judged stale against the cohort its peers are on;
    # a lone lane has nothing to be stale against.
    all_counters = {c for l in lanes.values() for c in l["counters"]}

    for machine in sorted(lanes):
        lane = lanes[machine]
        ex_total = sum(lane["exact"].values())
        es_total = sum(lane["estimated"].values())
        age_h = None
        stale = False
        if lane["last_export"] is not None:
            age_h = (now - lane["last_export"]).total_seconds() / 3600.0
            stale = age_h > STALE_HOURS
        if stale:
            warnings.append(
                f"lane {machine} last exported {age_h:.0f}h ago "
                f"(> {STALE_HOURS:.0f}h): its totals may be incomplete"
            )
        if len(all_counters) > 1 and lane["counters"] and lane["counters"] != all_counters:
            warnings.append(
                f"lane {machine} is on counting cohort "
                f"{sorted(lane['counters'])} while the fleet spans "
                f"{sorted(all_counters)}: MIXED COUNTING, totals are not comparable"
            )
        if "unknown-agent" in lane["generated_by"]:
            warnings.append(
                f"lane {machine} has dailies with generated_by=unknown-agent: "
                f"ledger provenance is incomplete"
            )
        for f in BUCKET_FIELDS:
            tot_exact[f] += lane["exact"][f]
            tot_est[f] += lane["estimated"][f]
        tot_inv += lane["invocations"]
        lane_out.append({
            "lane": machine,
            "days": lane["days"],
            "invocations": lane["invocations"],
            "exact_tokens": ex_total,
            "estimated_tokens": es_total,
            "blended_total": ex_total + es_total,
            "fresh_input": lane["exact"]["input_tokens"] + lane["estimated"]["input_tokens"],
            "output": lane["exact"]["output_tokens"] + lane["estimated"]["output_tokens"],
            "cache_read": lane["exact"]["cache_read"] + lane["estimated"]["cache_read"],
            "cache_creation": lane["exact"]["cache_creation"] + lane["estimated"]["cache_creation"],
            "reasoning": lane["exact"]["reasoning"] + lane["estimated"]["reasoning"],
            "counters": sorted(lane["counters"]),
            "generated_by": sorted(lane["generated_by"]),
            "partial_days": sorted(lane["partial_days"]),
            "last_export": lane["last_export"].isoformat() if lane["last_export"] else None,
            "age_hours": round(age_h, 1) if age_h is not None else None,
            "stale": stale,
        })

    exact_total = sum(tot_exact.values())
    est_total = sum(tot_est.values())
    if not lane_out:
        warnings.append("no dailies matched this window: zero here means no "
                        "telemetry, not measured zero")

    return {
        "version": CANONICAL_VERSION,
        "definition": " + ".join(BUCKET_FIELDS) + ", over exact AND estimated buckets",
        "window": {"since": since, "until": until,
                   "days_covered": len(days_seen),
                   "first_day": min(days_seen) if days_seen else None,
                   "last_day": max(days_seen) if days_seen else None},
        "exact_tokens": exact_total,
        "estimated_tokens": est_total,
        "blended_total": exact_total + est_total,
        "fresh_input": tot_exact["input_tokens"] + tot_est["input_tokens"],
        "output": tot_exact["output_tokens"] + tot_est["output_tokens"],
        "cache_read": tot_exact["cache_read"] + tot_est["cache_read"],
        "cache_creation": tot_exact["cache_creation"] + tot_est["cache_creation"],
        "reasoning": tot_exact["reasoning"] + tot_est["reasoning"],
        "invocations": tot_inv,
        "confidence": (exact_total / (exact_total + est_total))
                      if (exact_total + est_total) else None,
        "lanes": lane_out,
        "warnings": warnings,
        "generated_at": now.isoformat(),
    }


def legacy_total_activity_v1(since: Optional[str] = None,
                             until: Optional[str] = None,
                             root: Path = DAILIES) -> int:
    """Reproduce the connector's old number, for regression comparison only.

    Kept so a test can prove what v1 dropped. Do not render this to a user.
    """
    v1_fields = ("input_tokens", "output_tokens", "cache_read", "cache_creation")
    records = _read_dailies(root)
    if since:
        records = [r for r in records if r["date"] >= since]
    if until:
        records = [r for r in records if r["date"] <= until]
    return sum(int((r.get("exact") or {}).get(f, 0) or 0)
               for r in records for f in v1_fields)
