"""Tests for the token-usage MCP server.

Driven through the JSON-RPC surface a real client uses, rather than through the
Python functions underneath — a server that passes unit tests but answers the
wire wrongly is the failure mode that matters here.

Nothing touches a real tracker: the subprocess call is stubbed, so these run in
CI on a machine with no logs, no dailies and no agent CLIs installed.
"""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "integrations" / "nougen_usage_mcp.py"

FLEET_REPORT = """
Fleet totals - 3 machine(s), 91 day(s)
======================================================================
  machine                          input      output    cache read       spend
  blade1tb                   351,616,989  16,654,01810,888,376,074   $5,550.70
  phoebus                     43,545,792   1,357,321   901,763,133     $263.04
  whoart                         356,838   1,945,413   676,670,276     $352.49
  --------------------------------------------------------------------------
  counter 71aef8ff08fa (current) 351,973,947  18,639,25611,582,140,148
  FLEET                      395,519,619   19,956,752 12,466,809,483   $6,166.23

  spend by model (recomputed from tokens at this clone's prices):
        $2,135.10  claude-fable-5
          $762.90  gemini-3.5-flash-high (estimated)
"""


def _load(monkeypatch, tmp_path, report=FLEET_REPORT):
    """A fresh module with the tracker stubbed and the cache redirected."""
    monkeypatch.setenv("NOUGEN_USAGE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("NOUGENTRACKER_DIR", str(tmp_path))
    # The fleet these fixtures publish for; a real box gets its list from the mesh.
    monkeypatch.setenv("NOUGENTRACKER_EXPECTED_MACHINES", "blade1tb,phoebus,whoart")
    (tmp_path / "token_tracker.py").write_text("# stub", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("nougen_usage_mcp", MODULE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nougen_usage_mcp"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "run_tracker", lambda args, key: (report, 0.0))
    return mod


def _rpc(mod, monkeypatch, message):
    """Send one message, capture what the server writes to stdout."""
    sent = []
    monkeypatch.setattr(mod, "send", lambda payload: sent.append(payload))
    mod.handle(message)
    return sent


def _call(mod, monkeypatch, name, args=None):
    out = _rpc(mod, monkeypatch, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args or {}}})
    return out[0]


# --- protocol ---------------------------------------------------------------

def test_initialize_echoes_a_supported_protocol(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    for version in mod.SUPPORTED_PROTOCOLS:
        out = _rpc(mod, monkeypatch, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": version}})
        assert out[0]["result"]["protocolVersion"] == version


def test_initialize_falls_back_for_an_unknown_protocol(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    out = _rpc(mod, monkeypatch, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "1999-01-01"}})
    assert out[0]["result"]["protocolVersion"] == mod.PREFERRED_PROTOCOL


def test_initialize_ships_instructions(monkeypatch, tmp_path):
    """The client shows these to the model; an empty string wastes the slot."""
    mod = _load(monkeypatch, tmp_path)
    out = _rpc(mod, monkeypatch,
               {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert "estimat" in out[0]["result"]["instructions"].lower()


def test_notifications_get_no_reply(monkeypatch, tmp_path):
    """A notification has no id; answering one corrupts the stream."""
    mod = _load(monkeypatch, tmp_path)
    assert _rpc(mod, monkeypatch,
                {"jsonrpc": "2.0", "method": "notifications/initialized"}) == []


def test_unknown_method_is_an_error(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    out = _rpc(mod, monkeypatch,
               {"jsonrpc": "2.0", "id": 7, "method": "does/not/exist"})
    assert out[0]["error"]["code"] == mod.METHOD_NOT_FOUND


# --- tool surface -----------------------------------------------------------

def test_every_tool_declares_schema_output_and_annotations(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    out = _rpc(mod, monkeypatch,
               {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = out[0]["result"]["tools"]
    assert len(tools) == 7
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["outputSchema"]["type"] == "object"
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["description"] and tool["title"]
    by_name = {tool["name"]: tool for tool in tools}
    assert by_name["tracker_live_status"]["annotations"]["openWorldHint"] is False
    assert by_name["token_usage_provenance"]["annotations"]["openWorldHint"] is False
    assert by_name["fleet_token_usage"]["annotations"]["openWorldHint"] is True
    assert "YTD" in by_name["tracker_query"]["inputSchema"]["properties"]["period"]["enum"]


def test_input_schemas_refuse_extra_properties(monkeypatch, tmp_path):
    """A client that validates should reject junk before it reaches us."""
    mod = _load(monkeypatch, tmp_path)
    out = _rpc(mod, monkeypatch,
               {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    for tool in out[0]["result"]["tools"]:
        assert tool["inputSchema"]["additionalProperties"] is False


def test_unknown_tool_and_unknown_argument_are_rejected(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    assert _call(mod, monkeypatch, "nope")["error"]["code"] == mod.METHOD_NOT_FOUND
    bad = _call(mod, monkeypatch, "my_token_usage", {"day": 3})
    assert bad["error"]["code"] == mod.INVALID_PARAMS


def test_argument_types_and_bounds_are_enforced_server_side(monkeypatch, tmp_path):
    """Schemas guide clients, but an untrusted client can skip validation."""
    mod = _load(monkeypatch, tmp_path)
    for args in ({"days": "7"}, {"days": True}, {"days": 0}, {"days": 999999}):
        out = _call(mod, monkeypatch, "fleet_token_usage", args)
        assert out["error"]["code"] == mod.INVALID_PARAMS
    out = _call(mod, monkeypatch, "tracker_live_status", {"stale_after_days": -1})
    assert out["error"]["code"] == mod.INVALID_PARAMS


# --- answers ----------------------------------------------------------------

def test_fleet_usage_returns_structured_machines(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    data = _call(mod, monkeypatch, "fleet_token_usage")["result"]["structuredContent"]
    names = [m["machine"] for m in data["machines"]]
    assert names == ["blade1tb", "phoebus", "whoart"]
    assert data["machine_count"] == 3
    assert data["total_spend_usd"] == 6166.23


def test_counter_and_fleet_rows_are_not_read_as_machines(monkeypatch, tmp_path):
    """`counter …` and `FLEET` sit in the same table and are not machines.
    Counting either would invent a box and inflate the total."""
    mod = _load(monkeypatch, tmp_path)
    data = _call(mod, monkeypatch, "fleet_token_usage")["result"]["structuredContent"]
    assert not any(m["machine"].startswith(("counter", "FLEET"))
                   for m in data["machines"])


def test_every_answer_carries_its_provenance(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    for name in ("fleet_token_usage", "machine_token_usage",
                 "token_cost_by_model", "my_token_usage"):
        data = _call(mod, monkeypatch, name)["result"]["structuredContent"]
        assert "cache_age_seconds" in data
        assert data["as_of"] in ("live", "cached")


def test_a_cached_answer_says_so(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "run_tracker", lambda a, k: (FLEET_REPORT, 400.0))
    data = _call(mod, monkeypatch, "fleet_token_usage")["result"]["structuredContent"]
    assert data["as_of"] == "cached" and data["cache_age_seconds"] == 400.0


def test_estimated_models_are_named(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    data = _call(mod, monkeypatch, "fleet_token_usage")["result"]["structuredContent"]
    assert "gemini-3.5-flash-high" in data["estimated_sources"]


def test_fleet_answer_warns_that_silence_is_not_zero(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    data = _call(mod, monkeypatch, "fleet_token_usage")["result"]["structuredContent"]
    assert "stopped publishing" in data["caveat"]


def test_my_usage_declares_itself_estimated(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    report = "--- Google Antigravity (Hybrid) ---\n2026-07-31  1  2  3  0\n"
    monkeypatch.setattr(mod, "run_tracker", lambda a, k: (report, 0.0))
    out = _call(mod, monkeypatch, "my_token_usage")["result"]
    assert out["structuredContent"]["measured"] is False
    assert "estimated" in out["content"][0]["text"].lower()


def test_empty_report_does_not_pretend_to_be_zero_usage(monkeypatch, tmp_path):
    """No dailies in THIS checkout is not the same claim as no fleet spend.

    A shared working tree gets its branch switched by whoever is using it, and
    dailies/ exists only on some branches. The empty answer therefore has to
    name the clone and branch it read, or it reads as "the fleet spent
    nothing" — confidently, and wrongly.
    """
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "run_tracker", lambda a, k: ("", 0.0))
    out = _call(mod, monkeypatch, "fleet_token_usage")["result"]
    text = out["content"][0]["text"].lower()
    assert "not about whether" in text          # refuses the wrong reading
    assert str(tmp_path).lower() in text        # names the checkout
    assert "branch" in text
    assert out["structuredContent"]["machines"] == []
    assert "dailies_machines" in out["structuredContent"]


def _write_daily(root, machine, day, *, exact=0, estimated=0, partial=False):
    import json

    path = root / "dailies" / machine / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = {"input_tokens": exact, "output_tokens": 0,
              "cache_creation": 0, "cache_read": 0}
    est = {"input_tokens": estimated, "output_tokens": 0,
           "cache_creation": 0, "cache_read": 0}
    record = {"machine": machine, "date": day, "exact": fields,
              "estimated": est, "models": {}, "partial": partial}
    path.write_text(json.dumps(record), encoding="utf-8")


def test_tracker_query_returns_partial_floor_and_deferred_not_zero(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    _write_daily(tmp_path, "blade1tb", "2026-01-01", exact=12)
    data = mod.call_tool("tracker_query", {
        "period": "RANGE", "scope": "fleet", "start": "2026-01-01",
        "end": "2026-01-02", "as_of": "2026-01-02",
    })["structuredContent"]
    assert data["status"] == "partial"
    assert data["total"] is None
    assert data["observed_total"] == 12
    assert data["floor"] is True
    rows = {item["machine"]: item for item in data["machines"]}
    assert rows["blade1tb"]["state"] == "PARTIAL"
    assert rows["phoebus"]["state"] == "DEFERRED"
    assert rows["phoebus"]["tokens"] is None
    assert data["state"]["completeness"] == "PARTIAL"
    assert data["state"]["observation"] == "PARSED"
    assert data["state"]["coverage"]["missing_nodes"] == ["phoebus", "whoart"]
    assert data["state"]["recovery"] == "CONTINUE_FEDERATION"
    assert "coverage.missing_machine_days" in data["state"]["reason_codes"]
    assert "EXPECTED_NODE_NOT_QUERIED" in data["state"]["reason_codes"]
    assert data["state"]["federation"] == "NODE_DEFERRED"
    assert data["state"]["execution"] == "COMPLETE"
    assert data["state"]["flags"]["missing_expected_nodes"] is True
    assert data["state"]["flags"]["missing_expected_dates"] is True


def test_tracker_query_complete_integer_sum_and_provenance_are_deterministic(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    for machine, amount in (("blade1tb", 10), ("phoebus", 20), ("whoart", 30)):
        _write_daily(tmp_path, machine, "2026-01-01", exact=amount)
        _write_daily(tmp_path, machine, "2026-01-02", estimated=amount)
    args = {"period": "RANGE", "scope": "fleet", "start": "2026-01-01",
            "end": "2026-01-02", "as_of": "2026-01-02", "prove": True}
    first = mod.call_tool("tracker_query", args)["structuredContent"]
    second = mod.call_tool("tracker_query", args)["structuredContent"]
    assert first == second
    assert first["status"] == "complete"
    assert first["total"] == 120
    assert first["observed_total"] == 120
    assert first["quality_activity"] == {"exact": 60, "estimated": 60}
    assert len(first["provenance"]["source_hashes"]) == 6
    assert first["state"]["completeness"] == "COMPLETE"
    assert first["state"]["truth_quality"] == "ESTIMATED"
    assert first["state"]["recovery"] == "STOP_WITH_EXPLICIT_STATE"


def test_tracker_query_partial_daily_cannot_advance_fleet_total(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    for machine in ("blade1tb", "phoebus", "whoart"):
        _write_daily(tmp_path, machine, "2026-01-01", exact=4,
                     partial=(machine == "whoart"))
    data = mod.call_tool("tracker_query", {
        "period": "LATEST", "scope": "fleet", "as_of": "2026-01-01",
    })["structuredContent"]
    assert data["total"] is None
    assert data["observed_total"] == 12
    assert data["partial_by_machine"] == {
        "whoart": {"count": 1, "ranges": ["2026-01-01"]}}
    assert "coverage.partial_daily_artifact" in data["state"]["reason_codes"]
    assert "SNAPSHOT_INCOMPLETE" in data["state"]["reason_codes"]
    assert data["state"]["validation"] == "VALID"
    assert data["state"]["anomaly"] == "GAP"


def test_tracker_state_distinguishes_observed_zero_from_no_hit(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    for machine in ("blade1tb", "phoebus", "whoart"):
        _write_daily(tmp_path, machine, "2026-01-01", exact=0)
    data = mod.call_tool("tracker_query", {
        "period": "LATEST", "scope": "fleet", "as_of": "2026-01-01",
    })["structuredContent"]
    assert data["total"] == 0
    assert data["state"]["completeness"] == "COMPLETE"
    assert data["state"]["retrieval"] == "HIT"
    assert data["state"]["observation"] == "PARSED"
    assert data["state"]["execution"] == "COMPLETE"
    assert data["state"]["confidence"] == "HIGH"
    assert data["state"]["flags"]["observed"] is True
    assert data["state"]["flags"]["complete"] is True
    assert {
        "observation", "completeness", "conflict", "freshness", "retrieval", "pagination", "availability",
        "truth_quality", "validation", "canonicality", "computation", "provenance", "federation", "execution",
        "anomaly", "confidence", "flags", "reason_codes", "evidence", "coverage", "canonical_key", "recovery",
    } <= data["state"].keys()
    assert {
        "observed", "complete", "canonical", "current", "exact", "validated", "reconciled", "estimated",
        "stale", "partial", "conflicted", "superseded", "missing_expected_entities", "missing_expected_nodes",
        "missing_expected_dates", "provenance_incomplete", "retryable", "recoverable", "failover_available",
        "continuation_available", "deeper_search_available", "exact_source_available",
    } <= data["state"]["flags"].keys()


def test_tracker_state_distinguishes_malformed_source_from_deferred_node(tmp_path):
    daily = tmp_path / "dailies" / "blade1tb" / "2026-01-01.json"
    daily.parent.mkdir(parents=True)
    daily.write_text("not-json", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("tracker_query_expanded_state", ROOT / "tracker_query.py")
    tracker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = tracker
    spec.loader.exec_module(tracker)

    data = tracker.tracker_query(
        tmp_path, machines=["blade1tb"], period="LATEST", as_of="2026-01-01"
    )
    state = data["state"]
    assert data["total"] is None
    assert state["observation"] == "FETCHED"
    assert state["validation"] == "INVALID"
    assert state["federation"] == "NODE_FAILED"
    assert state["provenance"] == "SOURCE_UNAVAILABLE"
    assert state["flags"]["observed"] is True
    assert state["flags"]["missing_expected_nodes"] is False


def test_passive_live_status_never_runs_tracker_or_creates_cache(
        monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    daily = tmp_path / "dailies" / "phoebus" / "2026-09-04.json"
    daily.parent.mkdir(parents=True)
    daily.write_text(json.dumps({
        "date": "2026-09-04", "machine": "phoebus", "partial": False,
        "generated_at": "2026-09-04T05:00:00Z",
    }), encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("passive status invoked the usage tracker")

    monkeypatch.setattr(mod, "run_tracker", forbidden)
    out = _call(mod, monkeypatch, "tracker_live_status")
    data = out["result"]["structuredContent"]
    assert data["mode"] == "passive_metadata_only"
    assert not any(data["side_effects"].values())
    assert not (tmp_path / "cache").exists()


def test_provenance_does_not_create_cache_directory(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    _call(mod, monkeypatch, "token_usage_provenance")
    assert not (tmp_path / "cache").exists()


def test_tracker_probe_skips_an_untraversable_mount(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.delenv("NOUGENTRACKER_DIR")
    mount = tmp_path / "mount" / "NouGenTracker"
    mirror = tmp_path / "home" / ".nougen" / "tracker"
    mirror.mkdir(parents=True)
    (mirror / "token_tracker.py").write_text("# tracker", encoding="utf-8")
    monkeypatch.setenv("NOUGEN_ROOT", str(tmp_path / "home" / ".nougen"))
    monkeypatch.setattr(mod.mesh, "repo_dir", lambda name, near=None: mount)
    original_exists = mod.Path.exists

    def flaky_exists(path):
        if "mount" in path.parts:
            raise OSError("untrusted mount point")
        return original_exists(path)

    monkeypatch.setattr(mod.Path, "exists", flaky_exists)
    assert mod.tracker_dir() == mirror


def test_machine_checkout_takes_precedence_over_managed_mirror(
        monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.delenv("NOUGENTRACKER_DIR")
    checkout = tmp_path / "repos" / "NouGenTracker"
    mirror = tmp_path / "home" / ".nougen" / "tracker"
    for root in (checkout, mirror):
        root.mkdir(parents=True)
        (root / "token_tracker.py").write_text("# tracker", encoding="utf-8")
    monkeypatch.setenv("NOUGEN_ROOT", str(tmp_path / "home" / ".nougen"))
    monkeypatch.setenv("NOUGEN_TRACKER_DIR", str(checkout))

    assert mod.tracker_dir() == checkout


# --- failure behaviour ------------------------------------------------------

def test_a_missing_tracker_is_content_not_a_protocol_error(monkeypatch, tmp_path):
    """The agent must be able to relay this to the user."""
    mod = _load(monkeypatch, tmp_path)

    def boom(args, key):
        raise mod.TrackerError("cannot find token_tracker.py")

    monkeypatch.setattr(mod, "run_tracker", boom)
    out = _call(mod, monkeypatch, "fleet_token_usage")
    assert "error" not in out
    assert out["result"]["isError"] is True
    assert "token_tracker.py" in out["result"]["content"][0]["text"]


def test_an_unexpected_exception_does_not_take_the_server_down(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)

    def boom(args, key):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(mod, "run_tracker", boom)
    out = _call(mod, monkeypatch, "machine_token_usage")
    assert out["result"]["isError"] is True


# --- resources --------------------------------------------------------------

def test_resource_round_trip(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    listed = _rpc(mod, monkeypatch,
                  {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    uri = listed[0]["result"]["resources"][0]["uri"]
    read = _rpc(mod, monkeypatch, {
        "jsonrpc": "2.0", "id": 2, "method": "resources/read",
        "params": {"uri": uri}})
    payload = json.loads(read[0]["result"]["contents"][0]["text"])
    assert payload["estimated_lanes"] == ["Antigravity"]


def test_unknown_resource_is_rejected(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    out = _rpc(mod, monkeypatch, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "nougen://nope"}})
    assert out[0]["error"]["code"] == mod.INVALID_PARAMS


# --- installer --------------------------------------------------------------

def test_install_dry_run_changes_nothing(monkeypatch, tmp_path, capsys):
    mod = _load(monkeypatch, tmp_path)
    target = tmp_path / "mcp.json"
    original = json.dumps({"mcpServers": {"existing": {"command": "x"}}})
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(mod, "TARGETS", (("test-cli", str(target), "json"),))
    mod.install(dry_run=True)
    assert target.read_text(encoding="utf-8") == original
    assert "WOULD" in capsys.readouterr().out


def test_install_merges_and_preserves_existing_servers(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps({"mcpServers": {"existing": {"command": "x"}},
                                  "otherSetting": 42}), encoding="utf-8")
    monkeypatch.setattr(mod, "TARGETS", (("test-cli", str(target), "json"),))
    mod.install()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert "existing" in data["mcpServers"]
    assert data["otherSetting"] == 42
    assert data["mcpServers"][mod.SERVER_NAME]["args"]
    assert list(tmp_path.glob("mcp.json.bak-nougen-*"))


def test_install_is_idempotent(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setattr(mod, "TARGETS", (("test-cli", str(target), "json"),))
    mod.install()
    first = json.loads(target.read_text(encoding="utf-8"))
    mod.install()
    assert json.loads(target.read_text(encoding="utf-8")) == first


def test_install_refuses_to_touch_malformed_json(monkeypatch, tmp_path):
    """Rewriting a config we could not parse would destroy someone's setup."""
    mod = _load(monkeypatch, tmp_path)
    target = tmp_path / "mcp.json"
    target.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(mod, "TARGETS", (("test-cli", str(target), "json"),))
    mod.install()
    assert target.read_text(encoding="utf-8") == "{ this is not json"


def test_toml_install_appends_once_and_keeps_existing_content(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    target = tmp_path / "config.toml"
    target.write_text('model = "gpt-5.5"\n\n[mcp_servers.other]\ncommand = "x"\n',
                      encoding="utf-8")
    monkeypatch.setattr(mod, "TARGETS", (("codex", str(target), "toml"),))
    mod.install()
    once = target.read_text(encoding="utf-8")
    assert 'model = "gpt-5.5"' in once
    assert "[mcp_servers.other]" in once
    assert once.count(f"[mcp_servers.{mod.SERVER_NAME}]") == 1
    mod.install()
    assert target.read_text(encoding="utf-8").count(
        f"[mcp_servers.{mod.SERVER_NAME}]") == 1


def test_install_skips_a_cli_that_is_not_present(monkeypatch, tmp_path, capsys):
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(
        mod, "TARGETS", (("ghost", str(tmp_path / "nothing.json"), "json"),))
    mod.install()
    assert "not present" in capsys.readouterr().out
