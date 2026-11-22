"""Fleet usage ledger — append-only token accounting for every fleet lane.

This is the single place all forward token usage is recorded for lanes that
otherwise write nothing to disk (local Ollama/Gemma, OpenRouter, HF, etc.).
token_tracker.py reads the resulting JSONL as additional providers.

Design rules:
- Best-effort and FAIL-SAFE: logging must NEVER raise into an inference path.
- Append-only JSONL, one record per model call. No mutation of past rows.
- Plain integer token counts only; never write prompt/response text or secrets.
"""
import os
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()


def _ledger_path() -> str:
    # Explicit override wins.
    env = os.environ.get("FLEET_USAGE_LEDGER")
    if env:
        return env
    vault = os.environ.get("SOL_VAULT_DIR")
    if not vault:
        # REPO-relative, and it must stay that way: token_tracker.py defaults
        # FLEET_USAGE_LEDGER to <repo>/vault/fleet_usage.jsonl, so anything
        # else here writes rows the tracker never reads. This module arrived
        # from another repo, where parents[3] was the vault; in THIS repo
        # parents[3] is the user's home, which is nowhere. fleet ->
        # NouGenTracker is parents[1].
        vault = str(Path(__file__).resolve().parents[1] / "vault")
    return os.path.join(vault, "fleet_usage.jsonl")


def log_fleet_usage(
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    lane: str = None,
    source: str = "fleet",
    timestamp: str = None,
) -> None:
    """Append one usage record to the ledger. Silently no-ops on any error."""
    try:
        rec = {
            "timestamp": timestamp or datetime.now(timezone.utc).astimezone().isoformat(),
            "provider": provider,
            "lane": lane,
            "model": model,
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cached_tokens": int(cached_tokens or 0),
            "reasoning_tokens": int(reasoning_tokens or 0),
            "source": source,
        }
        path = _ledger_path()
        with _LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
    except Exception:
        # Accounting must never break a model call.
        pass
