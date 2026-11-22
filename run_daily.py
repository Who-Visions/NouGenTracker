#!/usr/bin/env python
"""NouGenTracker daily fleet job.

RECONSTRUCTED 2026-09-07. The original wrapper lived only on one box's disk,
was never tracked by git (`git log --all -- '*run_daily*'` is empty), and
disappeared between two runs on 2026-09-06. This rebuild follows the behaviour
documented in the scheduled-task spec; it is committed so the job cannot
vanish again.

Two jobs:

  1. Backfill every CLOSED day this machine still owes, oldest first, via
     token_tracker.py --publish (which exports *and* commits).
  2. Build the fleet readout (per-machine tokens + API-equivalent shadow cost,
     plus a week-to-date burn-down against the Saturday reset) and write it to
     readouts/<today>.txt.

Two invariants, deliberate:

  * NEVER today. An open day reports floors, not totals (partial: true).
    A floor inside a summed series is worse than a hole.
  * NEVER regenerate a day that already published. Re-running an aged day
    reads a partially pruned corpus and silently undercounts. Published state
    is decided with `git ls-files`, not a directory listing -- a daily can sit
    on disk untracked and look published.
"""

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from token_tracker import model_bill            # noqa: E402
from fleet_dailies import resolve_machine, known_machines  # noqa: E402

BACKFILL_WINDOW_DAYS = 14


def _run(args):
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True)


def _tracked_dailies(machine):
    """Days already published, per git -- not per directory listing."""
    out = _run(["git", "ls-files", f"dailies/{machine}"]).stdout
    return {Path(line).stem for line in out.splitlines() if line.strip().endswith(".json")}


def _day_bill(day):
    """API-equivalent shadow cost for one daily rollup.

    The daily's per-model buckets key cache-creation as `cache_creation`;
    model_bill() looks for `cache_write` / `cache_creation_input_tokens`.
    Mapping it is not cosmetic -- dropping it under-bills by ~28%.
    """
    total = 0.0
    for name, b in day.get("models", {}).items():
        total += model_bill(name, {
            "input_tokens":  b.get("input_tokens", 0),
            "output_tokens": b.get("output_tokens", 0),
            "cache_write":   b.get("cache_creation", 0),
            "cache_read":    b.get("cache_read", 0),
            "reasoning":     b.get("reasoning", 0),
        }, day.get("date"))[0]
    return total


def _day_tokens(day):
    return sum(v for v in day.get("totals", {}).values() if isinstance(v, (int, float)))


def _load(machine, date):
    p = REPO / "dailies" / machine / f"{date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def backfill(machine, today):
    """Publish every closed day this machine owes, oldest first."""
    tracked = _tracked_dailies(machine)
    owed = []
    for n in range(BACKFILL_WINDOW_DAYS, 0, -1):
        d = (today - _dt.timedelta(days=n)).isoformat()
        if d not in tracked:
            owed.append(d)

    if not owed:
        print("Nothing owed: every closed day in the last "
              f"{BACKFILL_WINDOW_DAYS} days is already published.")
        return

    print(f"Backfilling {len(owed)} unpublished closed day(s), oldest first: "
          + ", ".join(owed))

    published, idle, failed = [], [], []
    for d in owed:
        r = _run([sys.executable, "token_tracker.py", "--start", d, "--end", d, "--publish"])
        if r.returncode != 0:
            failed.append(d)
        elif (REPO / "dailies" / machine / f"{d}.json").exists():
            published.append(d)
        else:
            idle.append(d)

    print(f"Published {len(published)}/{len(owed)}"
          + (f": {', '.join(published)}" if published else "")
          + (f"; {len(idle)} idle (no invocations, correctly no file)" if idle else ""))
    if failed:
        print(f"FAILED {len(failed)} day(s): {', '.join(failed)}")


def readout(today):
    """Per-machine readout for the latest closed day + week-to-date burn-down."""
    latest = today - _dt.timedelta(days=1)

    lines = [f"NouGenTracker - fleet daily readout for {latest.isoformat()}",
             "=" * 52,
             f"{'machine':<12}{'invocations':>12}{'tokens':>10}{'API-equiv':>12}",
             "-" * 52]

    fleet_inv = fleet_tok = 0
    fleet_cost = 0.0
    for m in sorted(known_machines()):
        day = _load(m, latest.isoformat())
        if not day:
            continue
        inv, tok, cost = day.get("invocations", 0), _day_tokens(day), _day_bill(day)
        fleet_inv += inv
        fleet_tok += tok
        fleet_cost += cost
        lines.append(f"{m:<12}{inv:>12,}{tok/1e6:>9.1f}M{'$%.2f' % cost:>12}")

    lines += ["-" * 52,
              f"{'FLEET':<12}{fleet_inv:>12,}{fleet_tok/1e6:>9.1f}M{'$%.2f' % fleet_cost:>12}"]

    # Week-to-date: anchor on the most recent Saturday on or before the latest
    # closed day (weekday(): Mon=0 .. Sat=5), run through that closed day.
    anchor = latest - _dt.timedelta(days=(latest.weekday() - 5) % 7)
    wtd_cost, wtd_tok, days = 0.0, 0, (latest - anchor).days + 1
    for n in range(days):
        d = (anchor + _dt.timedelta(days=n)).isoformat()
        for m in known_machines():
            day = _load(m, d)
            if day:
                wtd_cost += _day_bill(day)
                wtd_tok += _day_tokens(day)

    lines += ["",
              f"Week-to-date (since {anchor.isoformat()}, {days} day(s)): "
              f"${wtd_cost:.2f} API-equivalent, {wtd_tok/1e6:.1f}M tokens",
              f"  pace: ~${wtd_cost/days:.2f}/day  ->  ~${wtd_cost/days*7:.0f} over a full 7-day week",
              "  (plan's authoritative session/weekly gauges live in the Claude app;",
              "   this is the local token+shadow-cost view NouGenTracker can prove from disk)"]

    text = "\n".join(lines)
    out = REPO / "readouts" / f"{today.isoformat()}.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    print(text)


def main():
    today = _dt.date.today()
    machine = resolve_machine()
    backfill(machine, today)
    readout(today)


if __name__ == "__main__":
    main()
