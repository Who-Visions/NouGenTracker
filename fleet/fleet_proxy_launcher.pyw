"""Windowless launcher for the fleet usage proxy (run with pythonw).

Binds the port real Ollama used and forwards to the relocated upstream, teeing
token counts into the canonical ledger.

Every value here is a documented default that the environment can override,
and the ledger path is derived from the vault location rather than written in.
NOUGEN_VAULT_DIR is the same variable the rest of the fleet reads; when it is
absent the ledger goes to this checkout's vault/, the path token_tracker.py
reads by default, never to another machine's layout.
"""
import os
import sys

_UPSTREAM_PORT = os.environ.get("FLEET_OLLAMA_UPSTREAM_PORT", "11436")
_VAULT = os.environ.get(
    "NOUGEN_VAULT_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vault"),
)

os.environ.setdefault("FLEET_PROXY_HOST", "0.0.0.0")
os.environ.setdefault("FLEET_PROXY_PORT", "11434")
os.environ.setdefault("FLEET_OLLAMA_UPSTREAM", f"http://127.0.0.1:{_UPSTREAM_PORT}")
os.environ.setdefault("FLEET_USAGE_LEDGER",
                      os.path.join(_VAULT, "fleet_usage.jsonl"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fleet_usage_proxy  # noqa: E402

fleet_usage_proxy.main()
