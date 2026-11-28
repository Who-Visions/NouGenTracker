"""Tests for the Muse//Trckr connector client (PR #48 fixes).

Covers the Opus-review fixes as failure paths, not just happy paths:
a minted ingest key cannot reach stdout through any code path in
client.py, a negative compaction delta is flagged rather than recorded,
an HTTP error body is only ever reported when it matches a known
non-secret pattern, and a trickling SSE stream is cut off by the total
deadline. Nothing here touches the live server.

Follow-up (Jensen verification, 2026-11-25): the first redactor only
redacted strings that *started* with ``trk_`` and whole values under
obvious secret field names, so mid-string keys, non-string secrets under
evasive field names, nested-list cases and both stderr channels leaked.
The tests below pin each of those variants shut.
"""
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import time
import urllib.error

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "muse-connector" / "bin"
CLIENT = BIN / "client.py"
TURNS = BIN / "turns.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = _load("muse_trckr_client", CLIENT)
turns = _load("muse_trckr_turns", TURNS)


# --- Blocker 1: ingest keys cannot leak -------------------------------

def test_ingest_key_not_allowed():
    assert "trckr_ingest_key" not in client.ALLOWED_TOOLS
    assert client.ALLOWED_TOOLS == {
        "trckr_prices", "trckr_price", "trckr_turn_cost", "trckr_record_turn",
        "trckr_session", "trckr_usage", "trckr_pools", "trckr_refresh",
    }


def test_call_tool_refuses_ingest_key(monkeypatch, tmp_path):
    args_file = tmp_path / "args.json"
    args_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "client.py", "call-tool",
        "--name", "trckr_ingest_key",
        "--arguments-file", str(args_file),
    ])
    with pytest.raises(SystemExit) as exc:
        client.main()
    assert exc.value.code == 2


def test_redact_secrets_nested_and_prefixed():
    result = {
        "ingest_key": "trk_live_would_leak",
        "nested": {"token": "abc", "prices": [{"model": "x", "input": 1}]},
        "other": "trk_live_in_plain_string",
        "key_id": "kid-1",
    }
    redacted = client.redact_secrets(result)
    assert "trk_live_would_leak" not in json.dumps(redacted)
    assert "trk_live_in_plain_string" not in json.dumps(redacted)
    assert redacted["ingest_key"] == "[REDACTED]"
    assert redacted["nested"]["token"] == "[REDACTED]"
    assert redacted["nested"]["prices"] == [{"model": "x", "input": 1}]
    assert redacted["other"] == "[REDACTED]"
    # A field that merely mentions a key id is not a secret value, but a
    # secret-named field is redacted regardless of its value.
    assert redacted["key_id"] == "[REDACTED]"


def test_main_prints_redacted_result_stdout(monkeypatch, capsys):
    secret = "trk_live_minted_key_value"

    class FakeClient:
        def initialize(self):
            return None

        def send(self, method, params):
            return {"ok": True, "ingest_key": secret}

    monkeypatch.setattr(client, "Client", FakeClient)
    monkeypatch.setattr(sys, "argv", ["client.py", "list-tools"])
    client.main()
    out = capsys.readouterr().out
    assert secret not in out
    assert "[REDACTED]" in out


def test_redact_secrets_mid_string_iserror():
    """Jensen attack A: a key mid-string inside an isError result."""
    result = {
        "isError": True,
        "content": [{"type": "text", "text": "validation failed for trk_abc123"}],
    }
    redacted = client.redact_secrets(result)
    assert "trk_abc123" not in json.dumps(redacted)
    assert redacted["content"][0]["text"] == "validation failed for [REDACTED]"
    # The surrounding text is kept, only the key is replaced.
    assert redacted["isError"] is True


def test_main_prints_redacted_iserror_result_stdout(monkeypatch, capsys):
    """Jensen attack A end to end: the isError payload printed verbatim."""
    class FakeClient:
        def initialize(self):
            return None

        def send(self, method, params):
            return {
                "isError": True,
                "content": [{"type": "text", "text": "validation failed for trk_abc123"}],
            }

    monkeypatch.setattr(client, "Client", FakeClient)
    monkeypatch.setattr(sys, "argv", ["client.py", "list-tools"])
    assert client.main() == 1  # isError results still exit 1
    out = capsys.readouterr().out
    assert "trk_abc123" not in out
    assert "validation failed for [REDACTED]" in out
    json.loads(out)  # still valid JSON


def test_redact_secrets_uppercase_prefix():
    """An upper-cased TRK_ echo is redacted the same way."""
    assert client.redact_secrets({"detail": "TRK_ABC123 rejected"}) == {
        "detail": "[REDACTED] rejected",
    }


def test_redact_secrets_nested_list_mid_string():
    """Jensen variant: a key mid-string inside a nested list."""
    result = {"items": [{"detail": "see trk_xyz789 for details"}]}
    redacted = client.redact_secrets(result)
    assert "trk_xyz789" not in json.dumps(redacted)
    assert redacted["items"][0]["detail"] == "see [REDACTED] for details"


def test_redact_secrets_nonstring_evasive_field():
    """Jensen attack B: a non-string secret under an evasive field name."""
    assert client.redact_secrets({"k_e_y": 12345}) == {"k_e_y": "[REDACTED]"}
    nested = client.redact_secrets({"results": [{"k_e_y": 987654}]})
    assert nested == {"results": [{"k_e_y": "[REDACTED]"}]}
    assert "987654" not in json.dumps(nested)


def test_redact_secrets_unicode_lookalike_field():
    """Jensen variant: a lookalike field name (Cyrillic е in "kеy")."""
    assert client.redact_secrets({"kеy": "some-secret-value"}) == {"kеy": "[REDACTED]"}
    # Fullwidth letters normalise the same way.
    assert client.redact_secrets({"ｋｅｙ": "v"}) == {"ｋｅｙ": "[REDACTED]"}
    # Leetspeak digits in a field name ("k3y", "t0ken").
    assert client.redact_secrets({"k3y": 1}) == {"k3y": "[REDACTED]"}
    assert client.redact_secrets({"t0ken": "x"}) == {"t0ken": "[REDACTED]"}


def test_redact_secrets_dict_key_is_ingest_key():
    """A dict key that itself contains a trk_ token is the secret."""
    redacted = client.redact_secrets({"trk_abc123": "innocent-value"})
    assert "trk_abc123" not in json.dumps(redacted)
    assert redacted == {"[REDACTED]": "innocent-value"}


def test_redact_secrets_bare_trk_fragment():
    """A bare "trk_" fragment is redacted too; the split-key suffix alone
    is not a usable key."""
    assert client.redact_secrets({"detail": "key is trk_"}) == {"detail": "key is [REDACTED]"}
    assert client.redact_secrets({"part1": "trk_", "part2": "abc123"}) == {
        "part1": "[REDACTED]", "part2": "abc123",
    }


def test_redact_secrets_bytes_value():
    """Bytes are decoded (UTF-8/replace) and redacted, never a crash."""
    redacted = client.redact_secrets({"data": b"trk_abc123"})
    assert redacted == {"data": "[REDACTED]"}
    json.dumps(redacted)  # serialisable now
    assert client.redact_secrets({"data": b"prefix trk_xyz789 \xff\xfe"}) == {
        "data": "prefix [REDACTED] \ufffd\ufffd",
    }


def test_main_prints_bytes_result_without_crash(monkeypatch, capsys):
    """Bytes in a result used to crash json.dumps with TypeError."""
    class FakeClient:
        def initialize(self):
            return None

        def send(self, method, params):
            return {"ok": True, "data": b"trk_abc123"}

    monkeypatch.setattr(client, "Client", FakeClient)
    monkeypatch.setattr(sys, "argv", ["client.py", "list-tools"])
    assert client.main() == 0
    out = capsys.readouterr().out
    assert "trk_abc123" not in out
    assert json.loads(out) == {"ok": True, "data": "[REDACTED]"}


def test_redact_secrets_counts_preserved():
    """Prices, token counts and usage figures pass through unchanged."""
    result = {
        "prices": [{"model": "x", "input": 1, "cost_usd": 0.02}],
        "input_tokens": 1234,
        "output_tokens": 56,
        "total_tokens": 1290,
        "tokens": 12345,
        "token_count": 99,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    assert client.redact_secrets(result) == result
    # The exemption is narrow: a bare singular "token" field is still a
    # secret-shaped name, even with a numeric value.
    assert client.redact_secrets({"token": 12345}) == {"token": "[REDACTED]"}
    assert client.redact_secrets({"api_token": "trk_abc123"}) == {"api_token": "[REDACTED]"}


# --- Stderr channels: server text must be redacted there too ----------

def test_jsonrpc_error_message_redacted():
    """Jensen attack D (decode): a JSON-RPC error message echoing a key."""
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": -32000, "message": "ingest key trk_abc123 rejected"},
    }).encode()
    with pytest.raises(client.McpError) as exc:
        client.Client.decode(payload, 1)
    assert "trk_abc123" not in str(exc.value)
    assert "ingest key [REDACTED] rejected" in str(exc.value)
    assert "-32000" in str(exc.value)


def test_http_error_json_error_field_redacted():
    """Jensen attack D (safe_http_error_detail): a JSON error body."""
    error = _http_error(json.dumps({"error": "bad key trk_abc123"}).encode())
    detail = client.safe_http_error_detail(error)
    assert "trk_abc123" not in detail
    assert "bad key [REDACTED]" in detail
    # A non-string error value is redacted the same way, not echoed raw.
    error = _http_error(json.dumps({"error": {"detail": "see trk_xyz789"}}).encode())
    assert "trk_xyz789" not in client.safe_http_error_detail(error)


def test_print_error_redacts(capsys):
    """The __main__ stderr handler is the final gate for exception text."""
    client._print_error(client.McpError("MCP returned a JSON-RPC error: key trk_abc123 bad"))
    err = capsys.readouterr().err
    assert "trk_abc123" not in err
    assert "[REDACTED]" in err


def test_missing_helper_exits_2_with_clear_message(tmp_path):
    """A client.py whose helper import cannot resolve exits 2, no traceback."""
    copy = tmp_path / "client.py"
    text = CLIENT.read_text(encoding="utf-8")
    text = text.replace(
        "/opt/muse/skills/custom-connector-setup/bin",
        str(tmp_path / "no-such-helper-dir"),
    )
    copy.write_text(text, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(copy), "list-tools"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 2
    assert "error: Muse connector helper not found" in proc.stderr
    assert "Traceback" not in proc.stderr


# --- HTTP error body: only known non-secret patterns ------------------

def _http_error(body: bytes):
    return urllib.error.HTTPError(
        "https://trckr.nougenai.com/mcp", 403, "Forbidden",
        {}, io.BytesIO(body),
    )


def test_http_error_1010_reported_without_body():
    error = _http_error(
        b"<html><head><title>Error</title></head><body>Error code: 1010"
        b"<br>secret-value-that-must-not-appear</body></html>"
    )
    detail = client.safe_http_error_detail(error)
    assert "1010" in detail
    assert "secret-value-that-must-not-appear" not in detail


def test_http_error_json_error_field_reported_truncated():
    error = _http_error(json.dumps({"error": "x" * 500}).encode())
    detail = client.safe_http_error_detail(error)
    assert detail.startswith("; server error: ")
    assert len(detail) <= len("; server error: ") + client.ERROR_MESSAGE_LIMIT


def test_http_error_other_body_discarded():
    error = _http_error(b"<html>some cloudflare page with a ray id 12345</html>")
    assert client.safe_http_error_detail(error) == ""
    error = _http_error(b"")
    assert client.safe_http_error_detail(error) == ""


# --- JSON-RPC errors carry code + truncated message -------------------

def test_jsonrpc_error_includes_code_and_truncated_message():
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": -32602, "message": "bad params " + "y" * 500},
    }).encode()
    with pytest.raises(client.McpError) as exc:
        client.Client.decode(payload, 1)
    assert "-32602" in str(exc.value)
    assert "bad params" in str(exc.value)
    assert "y" * 201 not in str(exc.value)


# --- Total deadline: a trickling SSE stream is cut off ----------------

class _TrickleResponse:
    """An SSE response that never ends and never yields a result."""

    def __init__(self):
        self.headers = _Headers("text/event-stream")

    def readline(self):
        time.sleep(0.01)
        return b": keepalive\n"


class _Headers:
    def __init__(self, content_type):
        self._content_type = content_type

    def get_content_type(self):
        return self._content_type


def test_sse_trickle_hits_total_deadline():
    deadline = time.monotonic() - 1  # already expired
    with pytest.raises(client.McpError) as exc:
        client.Client.read_result(_TrickleResponse(), 1, deadline)
    assert "total deadline" in str(exc.value)


# --- Turns: estimated growth, compaction flag, never $0 ---------------

def test_growth_record_positive_delta():
    record = turns.growth_record(
        lane="meta-app", model="muse-spark",
        context_tokens=240000, previous_context_tokens=230680,
    )
    assert record["estimated_context_growth_tokens"] == 9320
    assert record["context_tokens"] == 240000
    assert record["compaction_detected"] is False
    assert record["cost_usd"] is None
    assert "never 0" in record["unpriced_reason"]


def test_growth_record_first_record_is_unknown_not_zero():
    record = turns.growth_record(
        lane="meta-app", model="muse-spark", context_tokens=230680,
    )
    assert record["estimated_context_growth_tokens"] is None
    assert record["compaction_detected"] is False
    assert record["cost_usd"] is None


def test_growth_record_negative_delta_flagged_not_recorded():
    record = turns.growth_record(
        lane="meta-app", model="muse-spark",
        context_tokens=120000, previous_context_tokens=230680,
    )
    assert record["estimated_context_growth_tokens"] is None
    assert record["compaction_detected"] is True
    assert "compaction" in record["note"]
    assert record["cost_usd"] is None
    # No field anywhere in the record may carry a negative token number.
    assert all(
        not (isinstance(value, int) and value < 0)
        for value in record.values()
    )


def test_growth_record_rejects_bad_counts():
    with pytest.raises(ValueError):
        turns.growth_record(lane="m", model="m", context_tokens=-5)
    with pytest.raises(ValueError):
        turns.growth_record(
            lane="m", model="m", context_tokens=10, previous_context_tokens=-1,
        )


def test_committed_turns_file_is_schema_example_only():
    data = json.loads((ROOT / "muse-connector" / "dav1d-turns.json").read_text(encoding="utf-8"))
    assert data["kind"] == "dav1d-turns-schema-example"
    assert data["example"] is True
    assert "estimated_context_growth" in data["method"]
    # No mutable runtime state in the file. The junk-row id is named only
    # inside open_item (documentation of work owed in Trckr), never as a
    # live state field.
    blob = json.dumps(data)
    assert "last_context_tokens" not in blob
    assert "last_record_id" not in blob
    assert "last_request_id" not in blob
    assert "last_recorded_at" not in blob
    assert "5fde6115" in data["open_item"]
    assert data["example_record"]["cost_usd"] is None
    assert "never 0" in data["example_record"]["unpriced_reason"]


def test_connector_doc_states_public_client_id_and_open_provenance():
    doc = (ROOT / "muse-connector" / "CONNECTOR.md").read_text(encoding="utf-8")
    assert "given to Dave in chat" not in doc
    assert "public identifier, not a secret" in doc
    assert "NOT CONFIRMED" in doc
    assert "trckr_ingest_key" in doc  # named only as deliberately not enabled
    assert "9 tools" not in doc
    assert "8 tools" in doc
    assert "applied in `client.py` on 2026-11-23" not in doc
