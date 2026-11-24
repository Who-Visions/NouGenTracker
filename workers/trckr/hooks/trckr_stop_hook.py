#!/usr/bin/env python3
"""NouGen//Trckr Stop hook for Claude Code: posts each new turn's usage to /v1/turns.

Install: copy to ~/.claude/hooks/, add to ~/.claude/settings.json:
  {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/trckr_stop_hook.py"}]}]}}
Env: TRCKR_INGEST_KEY (from Keymaker), optional TRCKR_URL (default https://trckr.nougenai.com),
     TRCKR_LANE (default <hostname>-claude-code).

One turn = one API request (requestId). The transcript repeats a request's usage on every content
block it streamed, so turns are de-duplicated by requestId, and ids already sent are remembered per
session. The hook never blocks or fails the session: any error exits 0 quietly.
"""
import json
import os
import socket
import sys
import urllib.request
from pathlib import Path

URL = os.environ.get("TRCKR_URL", "https://trckr.nougenai.com").rstrip("/") + "/v1/turns"
STATE = Path(os.environ.get("TRCKR_STATE_DIR", Path.home() / ".claude" / "trckr"))


def turns_from_transcript(path, session_id, lane):
    by_request = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            msg = row.get("message") or {}
            usage = msg.get("usage")
            rid = row.get("requestId") or msg.get("id")
            model = msg.get("model")
            if row.get("type") != "assistant" or not usage or not rid or not model or model.startswith("<"):
                continue
            created = usage.get("cache_creation") or {}
            w5 = created.get("ephemeral_5m_input_tokens")
            w1 = created.get("ephemeral_1h_input_tokens")
            if w5 is None and w1 is None:
                w5, w1 = usage.get("cache_creation_input_tokens", 0), 0
            by_request[rid] = {
                "request_id": rid,
                "session_id": session_id,
                "lane": lane,
                "client": "claude-code",
                "provider": "anthropic",
                "model": model,
                "at": row.get("timestamp"),
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_write_5m_tokens": w5 or 0,
                "cache_write_1h_tokens": w1 or 0,
            }
    return list(by_request.values())


def main():
    key = os.environ.get("TRCKR_INGEST_KEY")
    if not key:
        return
    hook = json.load(sys.stdin)
    session_id = hook.get("session_id") or "unknown"
    transcript = hook.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        return
    lane = os.environ.get("TRCKR_LANE") or f"{socket.gethostname().split('.')[0].lower()}-claude-code"
    STATE.mkdir(parents=True, exist_ok=True)
    sent_file = STATE / f"{session_id}.sent"
    sent = set(sent_file.read_text().split()) if sent_file.exists() else set()
    new = [t for t in turns_from_transcript(transcript, session_id, lane) if t["request_id"] not in sent]
    if not new:
        return
    for i in range(0, len(new), 200):
        batch = new[i : i + 200]
        req = urllib.request.Request(
            URL,
            data=json.dumps({"turns": batch}).encode(),
            headers={"authorization": f"Bearer {key}", "content-type": "application/json", "user-agent": "trckr-stop-hook/1"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                return
        with sent_file.open("a") as fh:
            fh.write("".join(t["request_id"] + "\n" for t in batch))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
