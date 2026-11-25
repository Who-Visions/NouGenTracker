#!/usr/bin/env python3
"""NouGen//Trckr budget gate for Claude Code. Tells the AI first (Dave's call):
  - at the warning: UserPromptSubmit adds a wrap-up note to the model's context, nothing blocks,
  - at the cap: PreToolUse denies every tool call and UserPromptSubmit blocks the prompt, with the reason.
Reads the local watcher's signal files first (works with no cloud), then the cloud Worker when
TRCKR_INGEST_KEY is set. Any error lets the call through: a tracker outage never bricks a session.

settings.json:
  "hooks": {
    "PreToolUse":       [{ "matcher": "*", "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/trckr_budget_gate.py" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/trckr_budget_gate.py" }] }]
  }
"""
import json, os, sys, urllib.parse, urllib.request
from pathlib import Path

HOME = Path(os.environ.get("TRCKR_HOME", Path.home() / ".nougen" / "trckr"))
URL = os.environ.get("TRCKR_URL", "https://trckr.nougenai.com").rstrip("/")
KEY = os.environ.get("TRCKR_INGEST_KEY")


def status(sid):
    for kind in ("over", "warn"):
        f = HOME / "signals" / f"{sid}.{kind}"
        if f.exists():
            try:
                e = json.loads(f.read_text())
            except ValueError:
                e = {}
            return dict(e, state=kind)
    if KEY:
        req = urllib.request.Request(f"{URL}/v1/budget?session_id={urllib.parse.quote(sid)}", headers={"authorization": f"Bearer {KEY}"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read())
    return None


def main():
    try:
        hook = json.load(sys.stdin)
        sid = hook.get("session_id")
        b = status(sid) if sid else None
    except Exception as e:
        print(f"trckr gate: skipped ({e})", file=sys.stderr)
        return 0
    if not b or b.get("state") not in ("warn", "over"):
        return 0
    event = hook.get("hook_event_name")
    head = f"{b.get('label') or 'This session'} has spent ${float(b.get('spent_usd', 0)):.2f} of its ${float(b.get('cap_usd', 0)):.2f} cap ({b.get('pct')}%)."
    if b["state"] == "over":
        reason = f"OVER BUDGET. {head} Stop now: make no more model calls. {b.get('stop_rule') or ''}".strip()
        if event == "PreToolUse":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
        else:
            print(json.dumps({"decision": "block", "reason": reason}))
        return 0
    if event == "UserPromptSubmit":
        note = f"Budget warning. {head} Wrap up: finish the current step, write a handoff shard, and report."
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": note}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
