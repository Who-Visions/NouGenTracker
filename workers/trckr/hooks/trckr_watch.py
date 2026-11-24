#!/usr/bin/env python3
"""NouGen//Trckr local budget watcher. Plain Python, no model calls, works with no cloud.

Run it every minute from launchd or cron. Each run:
  1. reads Claude Code transcripts under ~/.claude/projects (every request deduped by requestId),
  2. prices each request at list price from the cached Trckr price board (~/.nougen/trckr/prices.json,
     refreshed from TRCKR_URL/v1/prices when online; a model the cache lacks stays unpriced, never guessed),
  3. compares each capped session with its cap and writes ~/.nougen/trckr/ledger.json,
  4. drops signal files the gate hook reads: signals/<session>.warn at warn_pct, signals/<session>.over at the cap,
  5. when TRCKR_INGEST_KEY is set, pushes each capped session's totals to the cloud Worker (/v1/budget),
     request by request through /v1/turns, so both watchers keep one ledger. Offline, the local side
     still enforces and catches up on the next online run.

Caps:  trckr_watch.py set <session_id> <cap_usd> [--warn 80] [--label NAME] [--stop-rule TEXT]
       trckr_watch.py set default <cap_usd>      (applies to every session without its own cap)
       trckr_watch.py close <session_id>
       trckr_watch.py status
Env:   TRCKR_URL (default https://trckr.nougenai.com), TRCKR_INGEST_KEY (Keymaker, optional),
       TRCKR_HOME (default ~/.nougen/trckr), CLAUDE_PROJECTS (default ~/.claude/projects),
       TRCKR_ALERT_CMD (optional, run with the alert text as one argument when a session hits its cap).
"""
import json, os, re, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("TRCKR_HOME", Path.home() / ".nougen" / "trckr"))
PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS", Path.home() / ".claude" / "projects"))
URL = os.environ.get("TRCKR_URL", "https://trckr.nougenai.com").rstrip("/")
KEY = os.environ.get("TRCKR_INGEST_KEY")
SCAN_DAYS = float(os.environ.get("TRCKR_SCAN_DAYS", "3"))
PRICE_TTL = 6 * 3600
FIELDS = (("input_tokens", "input"), ("output_tokens", "output"), ("cache_read_tokens", "cache_read"),
          ("cache_write_5m_tokens", "cache_write_5m"), ("cache_write_1h_tokens", "cache_write_1h"))


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def norm(model):
    s = re.sub(r"\[[^\]]*\]", "", str(model or "").strip().lower())
    s = re.sub(r"^models/", "", s)
    s = re.sub(r"[\s_.]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


def candidates(model):
    exact = norm(model)
    out = [exact, exact.split("/")[-1]]
    out.append(re.sub(r"-(?:20\d{6}|\d{4}-\d{2}-\d{2})$", "", out[-1]))
    out.append(re.sub(r"-(?:latest|preview|exp)$", "", out[-1]))
    return list(dict.fromkeys(x for x in out if x))


def prices(now):
    cache = HOME / "prices.json"
    data = load(cache, None)
    if data is None or now - data.get("_read", 0) > PRICE_TTL:
        try:
            with urllib.request.urlopen(f"{URL}/v1/prices", timeout=8) as r:
                fresh = json.loads(r.read())
            fresh["_read"] = now
            save(cache, fresh)
            data = fresh
        except Exception as e:  # offline: keep the last board
            print(f"trckr: price board not reachable ({e}); using cache", file=sys.stderr)
    index = {}
    for p in (data or {}).get("providers", []):
        for m in p.get("models", []):
            for name in [m.get("model")] + list(m.get("aliases") or []):
                if name:
                    index.setdefault(name, m)
    return index


def pick_band(model, day, size):
    for b in model.get("bands", []):
        if (b.get("valid_from") is None or day >= b["valid_from"]) and (b.get("valid_until") is None or day < b["valid_until"]) \
           and (b.get("min_prompt_tokens") is None or size >= b["min_prompt_tokens"]) \
           and (b.get("max_prompt_tokens") is None or size <= b["max_prompt_tokens"]) and not b.get("quarantined"):
            return b
    return None


def price(turn, index):
    """USD for one request, or None when the board has no rate for it."""
    model = next((index[c] for c in candidates(turn["model"]) if c in index), None)
    if not model:
        return None
    size = turn["input_tokens"] + turn["cache_read_tokens"] + turn["cache_write_5m_tokens"] + turn["cache_write_1h_tokens"]
    band = pick_band(model, (turn.get("at") or "")[:10], size)
    if not band:
        return None
    usd = 0.0
    for tk, field in FIELDS:
        n = turn.get(tk) or 0
        if n:
            if band.get(field) is None:
                return None
            usd += n * band[field] / 1e6
    return usd


def sessions(now):
    """{session_id: {request_id: turn}} from transcripts touched in the last SCAN_DAYS."""
    out = {}
    if not PROJECTS.exists():
        return out
    cutoff = now - SCAN_DAYS * 86400
    for path in PROJECTS.glob("**/*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            fh = open(path, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                msg = row.get("message") or {}
                usage = msg.get("usage") if isinstance(msg, dict) else None
                rid = row.get("requestId") or (msg.get("id") if isinstance(msg, dict) else None)
                model = msg.get("model") if isinstance(msg, dict) else None
                sid = row.get("sessionId") or path.stem
                if row.get("type") != "assistant" or not usage or not rid or not model or str(model).startswith("<"):
                    continue
                created = usage.get("cache_creation") or {}
                w5, w1 = created.get("ephemeral_5m_input_tokens"), created.get("ephemeral_1h_input_tokens")
                if w5 is None and w1 is None:
                    w5, w1 = usage.get("cache_creation_input_tokens", 0), 0
                out.setdefault(sid, {})[rid] = {
                    "model": model, "at": row.get("timestamp") or "",
                    "input_tokens": usage.get("input_tokens", 0) or 0, "output_tokens": usage.get("output_tokens", 0) or 0,
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0) or 0,
                    "cache_write_5m_tokens": w5 or 0, "cache_write_1h_tokens": w1 or 0,
                }
    return out


def post(path, body):
    req = urllib.request.Request(f"{URL}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"authorization": f"Bearer {KEY}", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def push(sid, cap, reqs, sent):
    """Send requests the cloud hasn't seen through /v1/turns (priced there per request, same as the
    Stop hook, idempotent by request id) and keep the cloud cap in step. Returns the cloud budget."""
    if not KEY:
        return None
    try:
        post("/v1/budget", {"session_id": sid, "cap_usd": cap["cap_usd"], "warn_pct": cap.get("warn_pct", 80),
                            "label": cap.get("label"), "stop_rule": cap.get("stop_rule")})
        new = [dict(t, request_id=rid, session_id=sid, lane="local-watch", client="claude-code", provider="anthropic")
               for rid, t in reqs.items() if rid not in sent]
        last = None
        for i in range(0, len(new), 200):
            last = post("/v1/turns", new[i:i + 200])
            sent.update(t["request_id"] for t in new[i:i + 200])
        if last and last.get("budget", {}).get(sid):
            return last["budget"][sid]
        return post("/v1/budget", {"session_id": sid})
    except Exception as e:
        print(f"trckr: cloud push failed for {sid} ({e})", file=sys.stderr)
        return None


def run():
    now = time.time()
    caps = load(HOME / "caps.json", {})
    default = caps.get("default")
    index = prices(now)
    ledger = load(HOME / "ledger.json", {"sessions": {}})
    pushed = load(HOME / "pushed.json", {})
    signals = HOME / "signals"
    signals.mkdir(parents=True, exist_ok=True)
    for sid, reqs in sessions(now).items():
        cap = caps.get(sid) or default
        if not cap or cap.get("closed"):
            continue
        spent, unpriced = 0.0, 0
        for t in reqs.values():
            usd = price(t, index)
            if usd is None:
                unpriced += 1
            else:
                spent += usd
        cap_usd = float(cap["cap_usd"])
        warn_pct = int(cap.get("warn_pct", 80))
        pct = int(spent * 100 // cap_usd) if cap_usd > 0 else 100
        state = "over" if spent >= cap_usd else "warn" if pct >= warn_pct else "ok"
        prev = ledger["sessions"].get(sid, {}).get("state")
        entry = {"label": cap.get("label"), "spent_usd": round(spent, 6), "cap_usd": cap_usd, "pct": pct, "state": state,
                 "unpriced": unpriced, "requests": len(reqs), "stop_rule": cap.get("stop_rule"),
                 "checked": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        ledger["sessions"][sid] = entry
        for kind in ("warn", "over"):
            f = signals / f"{sid}.{kind}"
            if state == kind:
                f.write_text(json.dumps(entry))
            elif f.exists():
                f.unlink()
        if state == "over" and prev != "over":
            text = f"{cap.get('label') or sid} hit its ${cap_usd:.2f} cap at ${spent:.2f} and was told to stop."
            print(text)
            if os.environ.get("TRCKR_ALERT_CMD"):
                subprocess.run([os.environ["TRCKR_ALERT_CMD"], text], timeout=30, check=False)
        sent = set(pushed.get(sid, []))
        cloud = push(sid, cap, reqs, sent)
        pushed[sid] = sorted(sent)
        if cloud:
            entry["cloud_state"] = cloud.get("state")
    save(HOME / "ledger.json", ledger)
    save(HOME / "pushed.json", pushed)
    return ledger


def main(argv):
    caps_path = HOME / "caps.json"
    if len(argv) >= 3 and argv[0] == "set":
        caps = load(caps_path, {})
        entry = {"cap_usd": float(argv[2]), "warn_pct": 80}
        rest = argv[3:]
        for flag, key in (("--warn", "warn_pct"), ("--label", "label"), ("--stop-rule", "stop_rule")):
            if flag in rest:
                v = rest[rest.index(flag) + 1]
                entry[key] = int(v) if key == "warn_pct" else v
        caps[argv[1]] = entry
        save(caps_path, caps)
        print(f"cap {argv[1]}: ${entry['cap_usd']:.2f}, warn at {entry['warn_pct']}%")
        return 0
    if len(argv) == 2 and argv[0] == "close":
        caps = load(caps_path, {})
        caps.setdefault(argv[1], {"cap_usd": 0})["closed"] = True
        save(caps_path, caps)
        for kind in ("warn", "over"):
            (HOME / "signals" / f"{argv[1]}.{kind}").unlink(missing_ok=True)
        return 0
    ledger = run()
    if argv and argv[0] == "status":
        for sid, e in sorted(ledger["sessions"].items(), key=lambda kv: -kv[1]["pct"]):
            print(f"{e['state'].upper():5} {e.get('label') or sid}: ${e['spent_usd']:.2f} of ${e['cap_usd']:.2f} ({e['pct']}%)" + (f", {e['unpriced']} unpriced" if e["unpriced"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
