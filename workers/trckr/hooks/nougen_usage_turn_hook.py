#!/usr/bin/env python3
"""Antigravity PreInvocation hook: per-turn NouGen token & cost telemetry.

Fast (<50ms), zero-network, zero-bloat ephemeral HUD injected on every turn.
Computes:
1. Per-turn token delta & estimated USD cost from the active conversation transcript.
2. Session cumulative tokens & estimated USD cost.
3. Today's Phoebus daily aggregate from NouGenTracker/dailies/phoebus/ if present.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

HOME = Path.home()
BRAIN_DIR = HOME / ".gemini" / "antigravity" / "brain"
STATE_FILE = HOME / ".nougen" / "state" / "agy_turn_usage.json"
TRACKER_DIR = Path(
    os.environ.get("NOUGENTRACKER_DIR", str(HOME / "The Observatory" / "NouGenTracker"))
)
TRCKR_MCP_URL = os.environ.get("TRCKR_URL", "https://trckr.nougenai.com").rstrip("/") + "/mcp"
TRCKR_MODEL = os.environ.get("NOUGEN_AGY_MODEL", "gemini-2.5-flash")
TRCKR_PROVIDER = os.environ.get("NOUGEN_AGY_PROVIDER", "google")

# Default conservative Gemini/Antigravity blended estimation rates ($ per 1M tokens)
INPUT_RATE_PER_M = float(os.environ.get("NOUGEN_AGY_INPUT_RATE_PER_M", "0.30"))
CACHE_READ_RATE_PER_M = float(os.environ.get("NOUGEN_AGY_CACHE_READ_RATE_PER_M", "0.03"))
OUTPUT_RATE_PER_M = float(os.environ.get("NOUGEN_AGY_OUTPUT_RATE_PER_M", "2.50"))


def _read_stdin_payload() -> dict[str, Any]:
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read().strip()
            if raw:
                return json.loads(raw)
    except Exception:
        pass
    return {}


def _find_transcript(payload: dict[str, Any]) -> tuple[str, Path | None]:
    conv_id = (
        payload.get("conversation_id")
        or payload.get("conversationId")
        or payload.get("session_id")
        or ""
    )
    if conv_id:
        p = BRAIN_DIR / str(conv_id) / ".system_generated" / "logs" / "transcript.jsonl"
        if p.exists():
            return str(conv_id), p

    # Fallback: most recently modified transcript.jsonl in BRAIN_DIR
    best_path: Path | None = None
    best_mtime = -1.0
    if BRAIN_DIR.exists():
        for entry in BRAIN_DIR.iterdir():
            if not entry.is_dir():
                continue
            cand = entry / ".system_generated" / "logs" / "transcript.jsonl"
            try:
                st = cand.stat()
                if st.st_mtime > best_mtime:
                    best_mtime = st.st_mtime
                    best_path = cand
                    conv_id = entry.name
            except OSError:
                continue
    return str(conv_id), best_path


def _estimate_transcript_tokens(path: Path) -> tuple[int, int, int, int, dict[str, dict[str, int]]]:
    """Return (uncached_input_tokens, cache_read_tokens, output_tokens, step_count, per_model_usage) from transcript.jsonl."""
    in_chars = 0
    out_chars = 0
    exact_in = 0
    exact_cr = 0
    exact_out = 0
    steps = 0
    by_model: dict[str, dict[str, int]] = {}
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                steps += 1
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                stype = str(obj.get("type") or "")
                content = str(obj.get("content") or "")
                thinking = str(obj.get("thinking") or "")
                model = str(obj.get("model") or "").strip()
                if stype == "PLANNER_RESPONSE":
                    out_chars += len(content) + len(thinking)
                    m_in = int(obj.get("input_tokens") or 0)
                    m_out = int(obj.get("output_tokens") or 0)
                    m_cr = int(obj.get("cache_read_tokens") or 0)
                    if model and (m_in or m_out or m_cr):
                        bucket = by_model.setdefault(model, {"in": 0, "out": 0, "cache_read": 0})
                        bucket["in"] += m_in
                        bucket["out"] += m_out
                        bucket["cache_read"] += m_cr
                        exact_in += m_in
                        exact_cr += m_cr
                        exact_out += m_out
                else:
                    in_chars += len(content)
    except OSError:
        pass
    # Prefer exact model token counts recorded on PLANNER_RESPONSE steps; fall back to ~4 chars/token
    tot_in = exact_in if (exact_in > 0 or exact_cr > 0) else (in_chars // 4)
    tot_out = exact_out if exact_out > 0 else (out_chars // 4)
    return tot_in, exact_cr, tot_out, steps, by_model


def _load_state() -> dict[str, Any]:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:
        pass


def _cost_usd(in_tok: int, out_tok: int, cache_read_tok: int = 0) -> float:
    return (
        (in_tok / 1_000_000.0) * INPUT_RATE_PER_M
        + (cache_read_tok / 1_000_000.0) * CACHE_READ_RATE_PER_M
        + (out_tok / 1_000_000.0) * OUTPUT_RATE_PER_M
    )


def _today_daily_summary() -> str:
    today = datetime.date.today().isoformat()
    for node_dir in ("phoebus", "Who_Mac_Mini", "local"):
        p = TRACKER_DIR / "dailies" / node_dir / f"{today}.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                tot = data.get("totals") or data
                t_in = int(tot.get("input_tokens") or tot.get("input") or 0)
                t_out = int(tot.get("output_tokens") or tot.get("output") or 0)
                if t_in or t_out:
                    return f"{t_in:,} in / {t_out:,} out"
            except Exception:
                pass
    return "local-est"


def _get_trckr_token() -> str:
    tok = os.environ.get("TRCKR_INGEST_KEY") or os.environ.get("TRCKR_ACCESS_TOKEN") or ""
    if tok:
        return tok
    try:
        sys.path.insert(0, str(HOME / "The Observatory" / "NouGen" / "nougenshards" / "src"))
        from nougen_shards import keymaker  # type: ignore
        tok = keymaker.get_secret("TRCKR_INGEST_KEY") or keymaker.get_secret("TRCKR_ACCESS_TOKEN") or ""
        if tok:
            return tok
    except Exception:
        pass
    try:
        env_p = HOME / "The Observatory" / ".env"
        if env_p.exists():
            for line in env_p.read_text(encoding="utf-8").splitlines():
                if line.startswith("TRCKR_INGEST_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _record_turn_trckr(
    conv_id: str,
    turn_num: int,
    deltas_by_model: dict[str, dict[str, int]],
    fallback_in: int,
    fallback_out: int,
) -> str:
    """Post per-turn delta to Trckr split per model (lane=antigravity, never lumped). Never blocks or raises."""
    rows = []
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if deltas_by_model:
        for model_id, d in sorted(deltas_by_model.items()):
            di, do, dcr = int(d.get("in", 0)), int(d.get("out", 0)), int(d.get("cache_read", 0))
            if di <= 0 and do <= 0 and dcr <= 0:
                continue
            prov = "anthropic" if model_id.startswith("claude") else ("openai" if model_id.startswith("gpt") else TRCKR_PROVIDER)
            rows.append({
                "lane": "antigravity",
                "session_id": conv_id,
                "request_id": f"agy-{conv_id[:8]}-t{turn_num}-{model_id}",
                "model": model_id,
                "provider": prov,
                "input_tokens": di,
                "output_tokens": do,
                "cache_read_tokens": dcr,
                "at": now_iso,
            })
    elif fallback_in > 0 or fallback_out > 0:
        rows.append({
            "lane": "antigravity",
            "session_id": conv_id,
            "request_id": f"agy-{conv_id[:8]}-t{turn_num}",
            "model": TRCKR_MODEL,
            "provider": TRCKR_PROVIDER,
            "input_tokens": fallback_in,
            "output_tokens": fallback_out,
            "cache_read_tokens": 0,
            "at": now_iso,
        })
    if not rows:
        return "skip"

    # Always append to local Trckr ledger (~/.nougen/trckr/agy_turns.jsonl) so local caps work with no cloud
    try:
        trckr_dir = HOME / ".nougen" / "trckr"
        trckr_dir.mkdir(parents=True, exist_ok=True)
        with (trckr_dir / "agy_turns.jsonl").open("a", encoding="utf-8") as lf:
            for r in rows:
                lf.write(json.dumps(r) + "\n")
    except Exception:
        pass

    tok = _get_trckr_token()
    if not tok:
        return f"local({len(rows)}m)"
    ingest_url = os.environ.get("TRCKR_URL", "https://trckr.nougenai.com").rstrip("/") + "/v1/turns"
    try:
        req = urllib.request.Request(
            ingest_url,
            data=json.dumps({"turns": rows}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {tok}",
                "Content-Type": "application/json",
                "User-Agent": "nougen-usage-turn-hook/1.2",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            if resp.status == 200:
                return f"live({len(rows)})"
            return f"http-{resp.status}"
    except Exception:
        return "local-buffered"


def main() -> int:
    payload = _read_stdin_payload()
    conv_id, transcript = _find_transcript(payload)
    if not transcript:
        print(json.dumps({"injectSteps": []}))
        return 0

    in_tok, cr_tok, out_tok, steps, by_model = _estimate_transcript_tokens(transcript)
    state = _load_state()
    prev = state.get(conv_id) if isinstance(state.get(conv_id), dict) else {}
    prev_models = prev.get("by_model") if isinstance(prev.get("by_model"), dict) else {}

    turn_num = int(prev.get("turn", 0)) + 1
    d_in = max(0, in_tok - int(prev.get("in_tok", 0)))
    d_cr = max(0, cr_tok - int(prev.get("cr_tok", 0)))
    d_out = max(0, out_tok - int(prev.get("out_tok", 0)))

    deltas_by_model: dict[str, dict[str, int]] = {}
    for m, cur in by_model.items():
        pm = prev_models.get(m) if isinstance(prev_models.get(m), dict) else {}
        dm = {
            "in": max(0, int(cur.get("in", 0)) - int(pm.get("in", 0))),
            "out": max(0, int(cur.get("out", 0)) - int(pm.get("out", 0))),
            "cache_read": max(0, int(cur.get("cache_read", 0)) - int(pm.get("cache_read", 0))),
        }
        if dm["in"] or dm["out"] or dm["cache_read"]:
            deltas_by_model[m] = dm

    state[conv_id] = {
        "turn": turn_num,
        "in_tok": in_tok,
        "cr_tok": cr_tok,
        "out_tok": out_tok,
        "steps": steps,
        "by_model": by_model,
    }
    # Keep state bounded to last 50 conversations
    if len(state) > 50:
        for k in list(state.keys())[:-50]:
            state.pop(k, None)
    _save_state(state)

    trckr_status = _record_turn_trckr(conv_id, turn_num, deltas_by_model, d_in, d_out)
    turn_cost = _cost_usd(d_in, d_out)
    sess_cost = _cost_usd(in_tok, out_tok)
    daily = _today_daily_summary()

    msg = (
        f"📊 [NOUGEN-USAGE | TURN #{turn_num}] "
        f"Δturn: ~{d_in:,} in / ~{d_out:,} out (~${turn_cost:.4f}) · "
        f"Session: ~{in_tok:,} in / ~{out_tok:,} out (~${sess_cost:.4f}) · "
        f"Daily: {daily} · Trckr: {trckr_status}"
    )
    print(json.dumps({"injectSteps": [{"ephemeralMessage": msg}]}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({"injectSteps": []}))
        sys.exit(0)
