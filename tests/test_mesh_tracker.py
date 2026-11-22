"""NouGenTracker on the NouGen mesh: the tester's own machines, no owner names.

A fresh box expects only itself; a mesh.json adds the tester's own peers; no
runtime file names another operator's machines, hosts, paths or email; and the
repo's tools load from nougen.tools.json, generated from the MCP server.
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import nougen_mesh  # noqa: E402
import tracker_live  # noqa: E402


def _mcp():
    spec = importlib.util.spec_from_file_location("nougen_usage_mcp_mesh_t", ROOT / "integrations" / "nougen_usage_mcp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mesh_file(tmp_path, monkeypatch, cfg):
    f = tmp_path / "mesh.json"
    f.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("NOUGEN_MESH_FILE", str(f))


# ---------------------------------------------------------------- (a) identity
def test_fresh_box_expects_only_itself(monkeypatch):
    monkeypatch.setenv("NOUGEN_MACHINE", "Tester Box")
    assert nougen_mesh.node() == "standalone"
    assert tracker_live.expected_machines() == ["tester-box"]


def test_tester_mesh_adds_their_own_peers(tmp_path, monkeypatch):
    monkeypatch.setenv("NOUGEN_MACHINE", "tester-box")
    _mesh_file(tmp_path, monkeypatch, {
        "identity": {"node": "home"},
        "nodes": {"home": {}, "peer1": {}, "peer2": {"machine": "peer2-laptop"}}})
    assert tracker_live.expected_machines() == ["peer1", "peer2-laptop", "tester-box"]


def test_existing_fleet_hosts_file_still_counts(tmp_path, monkeypatch):
    root = Path(nougen_mesh.home())
    root.mkdir(parents=True, exist_ok=True)
    (root / "fleet_hosts.json").write_text(json.dumps({"nodes": {"peer9": {"host_patterns": ["peer9"]}}}),
                                           encoding="utf-8")
    monkeypatch.setenv("NOUGEN_MACHINE", "tester-box")
    assert tracker_live.expected_machines() == ["peer9", "tester-box"]


def test_explicit_expected_list_wins(tmp_path, monkeypatch):
    _mesh_file(tmp_path, monkeypatch, {"nodes": {"peer1": {}}})
    monkeypatch.setenv("NOUGENTRACKER_EXPECTED_MACHINES", "a, B")
    assert tracker_live.expected_machines() == ["a", "b"]


def test_checkout_resolves_through_the_mesh(tmp_path, monkeypatch):
    monkeypatch.delenv("NOUGENTRACKER_DIR", raising=False)
    assert tracker_live.default_checkout() == ROOT
    other = tmp_path / "NouGenTracker"
    (other / ".git").mkdir(parents=True)
    monkeypatch.setenv("NOUGEN_TRACKER_DIR", str(other))
    assert tracker_live.default_checkout() == other
    monkeypatch.setenv("NOUGENTRACKER_DIR", str(tmp_path))
    assert tracker_live.default_checkout() == tmp_path


def test_mcp_finds_this_checkout_on_a_fresh_box(monkeypatch):
    monkeypatch.delenv("NOUGENTRACKER_DIR", raising=False)
    assert _mcp().tracker_dir() == ROOT


# ---------------------------------------------------------------- (b) no owner names
_OWNER = re.compile(
    r"(?i)The Observatory|Watchtower|Outpost|C:[\\/]+Users[\\/]+super|/Users/kushboygroup|\bblade(1tb)?\b"
    r"|\bphoebus\b|\bwhoart\b|nougenai\.com|aiwithdav3|\bhyperion\b|192\.168\.\d+\.\d+|10\.0\.0\.\d+"
    r"|@gmail\.com|whoentertains|meralus")

#: Runtime files swept. Left out on purpose: dailies/ and vault/ (published
#: data), .handoffs/ (relay records), dashboard.html (a page rendered from the
#: dailies), regen_whoart_dailies.py (a one-off repair of one box's history).
RUNTIME = [
    "tracker_live.py", "tracker_query.py", "token_tracker.py", "fleet_dailies.py", "fleet_summary.py",
    "pricing_live.py", "pricing_catalog.py", "public_surface.py", "subscriptions.py", "run_daily.py",
    "audit_daemon.py", "dashboard.py", "nougen_mesh.py", "nougen.tools.json",
    "integrations/nougen_usage_mcp.py", "integrations/mcp_triage.py",
    "fleet/fleet_usage_log.py", "fleet/fleet_usage_proxy.py", "fleet/fleet_proxy_launcher.pyw",
    "fleet/agy_usage.py", "fleet/ollama_upstream.cmd", "fleet/start_fleet_lane.vbs",
    "scheduler/tracker_daily_publish.ps1", "scheduler/register_tracker_daily.ps1", "scheduler/export_window.py",
    "analysis/export_window.py", ".githooks/pre-commit", ".githooks/prepare-commit-msg",
    "README.md", "README.nougen.json",
]


def test_runtime_names_no_owner_paths_or_hosts():
    for rel in RUNTIME:
        text = (ROOT / rel).read_text(encoding="utf-8")
        hit = _OWNER.search(text)
        assert not hit, (rel, hit.group(0))


def test_every_script_is_swept():
    skip_dirs = {"dailies", "vault", "tests", ".git", "__pycache__", ".handoffs"}
    skip_files = {"regen_whoart_dailies.py"}
    code = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
            if p.is_file() and p.suffix in (".py", ".pyw", ".ps1", ".cmd", ".vbs")
            and not skip_dirs & set(p.relative_to(ROOT).parts) and p.name not in skip_files}
    assert sorted(code - set(RUNTIME)) == []


# ---------------------------------------------------------------- (c) manifest
def test_manifest_is_generated_from_the_mcp_server():
    on_disk = json.loads((ROOT / "nougen.tools.json").read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(_mcp().mesh_manifest()))
    assert on_disk["schema"] == nougen_mesh.MANIFEST_SCHEMA and on_disk["repo"] == "tracker"
    names = {t["name"] for t in on_disk["tools"]}
    assert set(_mcp().TOOLS) | {"log_fleet_usage"} == names


def test_manifest_tools_run_through_the_mesh(tmp_path, monkeypatch):
    monkeypatch.setenv("NOUGEN_TRACKER_DIR", str(ROOT))
    monkeypatch.setenv("NOUGENTRACKER_DIR", str(ROOT))
    reg = nougen_mesh.tools()
    assert "tracker.tracker_live_status" in reg and reg["tracker.tracker_live_status"]["repo_dir"] == str(ROOT)
    status = nougen_mesh.call("tracker.tracker_live_status", {"stale_after_days": 2}, timeout=120)
    assert status["result"]["mode"] == "passive_metadata_only"
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("FLEET_USAGE_LEDGER", str(ledger))
    nougen_mesh.call("tracker.log_fleet_usage", {"provider": "arliai", "model": "m1", "input_tokens": 7,
                                                 "output_tokens": 3, "lane": "open:arliai",
                                                 "source": "nougen-open"}, timeout=60)
    row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert (row["provider"], row["input_tokens"], row["output_tokens"], row["source"]) == ("arliai", 7, 3, "nougen-open")


def test_fleet_usage_log_signature_is_unchanged():
    import inspect
    sys.path.insert(0, str(ROOT / "fleet"))
    import fleet_usage_log
    params = list(inspect.signature(fleet_usage_log.log_fleet_usage).parameters)
    assert params == ["provider", "model", "input_tokens", "output_tokens", "cached_tokens",
                      "reasoning_tokens", "lane", "source", "timestamp"]
