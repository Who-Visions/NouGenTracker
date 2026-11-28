#!/usr/bin/env python3
"""Muse client for the NouGen//Trckr MCP server at trckr.nougenai.com.

Speaks Streamable HTTP JSON-RPC to the live Trckr server: list the tool
catalog (`list-tools`) or call one registered tool (`call-tool`) with its
arguments as a JSON object in a file. The server holds the credential; this
client only carries the Muse surrogate for the registered provider.

Secrets never reach stdout or stderr: `trckr_ingest_key` is not an allowed tool, and
every result is passed through `redact_secrets` before printing. What
`redact_secrets` guarantees: (1) any substring matching ``trk_`` followed by
token characters — anywhere inside any string, at any nesting depth, including
inside longer strings, inside error text, and inside bytes values (decoded
UTF-8/replace first) — is replaced with ``[REDACTED]``; (2) the whole value of
any field whose name, after homoglyph/separator normalisation, contains
key/secret/token/password/credential is replaced, except a numeric value under
an explicit usage-counter name (``input_tokens``, ``total_tokens``,
``token_count`` and the like), which keeps its number; (3) the stderr paths
(`decode`, `safe_http_error_detail`, the `__main__` error print) run through
the same redaction. What it does NOT guarantee: a secret that contains no
``trk_`` pattern and sits under an innocent field name, or a key split across
fields so no single value contains ``trk_`` — neither is detectable from the
value alone. Nothing in this client writes a secret to a file; minting ingest
keys is out of scope here.
"""

import argparse
import json
from pathlib import Path
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    from dynamic_credentials import DynamicCredentialError, add_surrogate_to_request
except ImportError as error:
    print(
        "error: Muse connector helper not found "
        f"(/opt/muse/skills/custom-connector-setup/bin/dynamic_credentials.py: {error}); "
        "the Trckr connector cannot attach its credential",
        file=sys.stderr,
    )
    raise SystemExit(2)


PROVIDER = "custom.trckr-nougenai"
ENDPOINT = "https://trckr.nougenai.com/mcp"
# trckr_ingest_key is deliberately NOT here: it mints ingest keys, and a
# minted key must never be printed to stdout, a transcript or a log.
# Minting keys is out of scope for this client.
ALLOWED_TOOLS: set[str] = {'trckr_prices', 'trckr_price', 'trckr_turn_cost', 'trckr_record_turn', 'trckr_session', 'trckr_usage', 'trckr_pools', 'trckr_refresh'}
PROTOCOLS = {"2025-11-25", "2025-06-18", "2025-03-26"}

# Total wall-clock deadline for one MCP request, seconds. This is separate
# from the per-socket-operation timeout passed to urlopen: a stream that
# trickles keepalives forever is cut off here.
REQUEST_TIMEOUT = 45
# How much of an HTTP error body may be inspected, bytes.
ERROR_BODY_LIMIT = 2048
# How much of a JSON-RPC error message may be shown, characters.
ERROR_MESSAGE_LIMIT = 200

_SECRET_FIELD = re.compile(r"key|secret|token|password|credential")
# An ingest key anywhere inside a string, not just at its start: the
# trailing class is `*` so a bare `trk_` fragment is redacted too (a key
# split across fields leaves only a useless suffix behind). Case-insensitive:
# a server echoing an upper-cased key must not slip through.
_INGEST_KEY_RE = re.compile(r"trk_[A-Za-z0-9_-]*", re.IGNORECASE)
# Numeric usage counters keep their numbers even though their names contain
# "token": input_tokens / total_tokens / token_count and the like are counts,
# not secrets. Singular "token" / "api_token" / "access_token" stay redacted.
_COUNT_FIELD = re.compile(r"(tokens|tokencount|tokensused|tokenusage)$")
# Cyrillic / Greek lookalikes of Latin letters, for field-name evasion such
# as "kеy" (Cyrillic е). Applied after NFKC + lowercasing.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
    "і": "i", "ј": "j", "ѕ": "s", "һ": "h", "ԁ": "d", "ԝ": "w", "к": "k",
    "м": "m", "н": "h", "т": "t", "в": "b", "α": "a", "ε": "e", "ο": "o",
    "ρ": "p", "ι": "i", "κ": "k", "ν": "v", "τ": "t", "χ": "x", "υ": "y",
    "η": "n", "μ": "u", "ı": "i",
})
# Leetspeak digits in field names only ("k3y", "t0ken").
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s",
                       "7": "t", "@": "a", "$": "s"})
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)


def _normalize_field_name(field):
    """Fold a field name for secret matching: NFKC, lower, homoglyphs and
    leet digits to Latin, then strip every separator.

    "k_e_y", "kеy" (Cyrillic е), "K3Y" and "k.e.y" all normalise to "key".
    """
    name = unicodedata.normalize("NFKC", str(field)).lower()
    name = name.translate(_ZERO_WIDTH)
    name = name.translate(_CONFUSABLES)
    name = name.translate(_LEET)
    return re.sub(r"[^a-z0-9]", "", name)


def redact_secrets(value):
    """Recursively replace secret-looking values with ``[REDACTED]``.

    Three rules, in order:
    1. Any string (or bytes value, decoded UTF-8/replace) has every
       ``trk_`` substring — anywhere inside it, not just at the start —
       replaced with ``[REDACTED]``; the surrounding text is kept.
    2. A dict value is redacted wholesale when its field name, after
       homoglyph/separator normalisation, contains key/secret/token/
       password/credential — whatever the value's type (string, int, list,
       dict). The one exception is a plain number under an explicit
       usage-counter name (``input_tokens``, ``total_tokens``,
       ``token_count`` …), which is a count and is kept.
    3. A dict key that itself contains a ``trk_`` substring is the secret,
       so the key is replaced with ``[REDACTED]`` (suffixed on collision).

    Prices, token counts and usage figures pass through unchanged. The
    guarantee is bounded: a secret with no ``trk_`` pattern under an
    innocent field name, or a key split across fields so no single value
    contains ``trk_``, cannot be detected from the value alone. Apply to
    every result before printing — including ``isError`` results, which may
    echo a server-side value — and to every error string before it reaches
    stderr.
    """
    if isinstance(value, dict):
        redacted = {}
        for field, item in value.items():
            if isinstance(field, str) and _INGEST_KEY_RE.search(field):
                new_field = "[REDACTED]"
                suffix = 2
                while new_field in redacted:
                    new_field = f"[REDACTED:{suffix}]"
                    suffix += 1
                redacted[new_field] = redact_secrets(item)
                continue
            if isinstance(field, str):
                normalized = _normalize_field_name(field)
                if _SECRET_FIELD.search(normalized):
                    if (
                        isinstance(item, (int, float))
                        and not isinstance(item, bool)
                        and _COUNT_FIELD.search(normalized)
                    ):
                        redacted[field] = item
                    else:
                        redacted[field] = "[REDACTED]"
                    continue
            redacted[field] = redact_secrets(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [redact_secrets(item) for item in sorted(value, key=repr)]
    if isinstance(value, bytes):
        return _INGEST_KEY_RE.sub("[REDACTED]", value.decode("utf-8", "replace"))
    if isinstance(value, str):
        return _INGEST_KEY_RE.sub("[REDACTED]", value)
    return value


def _json_default(obj):
    """Fallback for values ``json.dumps`` cannot serialise: render, redact."""
    return redact_secrets(str(obj))


def safe_http_error_detail(error):
    """Return a safe, bounded detail for an HTTPError, or ``""``.

    Reads at most ~2 KB of the error body and reports it only when it
    matches a known non-secret pattern: a Cloudflare ``error code: 1010``
    (the edge user-agent ban, which this client works around with a
    browser User-Agent) or a JSON ``error`` field. Any other body is
    discarded, and headers are never echoed — an error body or header
    must never become a channel for a credential to reach a log.
    """
    try:
        raw = error.read(ERROR_BODY_LIMIT)
    except Exception:
        return ""
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace")
    if "error code: 1010" in text.lower():
        return "; Cloudflare error code: 1010 (edge user-agent ban)"
    try:
        parsed = json.loads(text)
    except ValueError:
        return ""
    if isinstance(parsed, dict) and "error" in parsed:
        # The error value itself is server text: run it through the same
        # redaction before it can reach stderr (Jensen attack D: a JSON
        # {"error": "bad key trk_abc123"} body used to print verbatim).
        redacted = redact_secrets(parsed["error"])
        if isinstance(redacted, str):
            detail_text = redacted
        else:
            try:
                detail_text = json.dumps(redacted, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                detail_text = "[REDACTED]"
        if not detail_text:
            return ""
        return f"; server error: {detail_text[:ERROR_MESSAGE_LIMIT]}"
    return ""


class McpError(RuntimeError):
    """A transport or protocol failure with no automatic replay."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise McpError("MCP endpoint redirected; verify the endpoint before continuing")


class Client:
    def __init__(self):
        endpoint = urllib.parse.urlsplit(ENDPOINT)
        if (
            not PROVIDER.startswith("custom.")
            or endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.port not in (None, 443)
        ):
            raise McpError("Configure a registered provider and its fixed HTTPS endpoint")
        self.host = endpoint.hostname
        self.opener = urllib.request.build_opener(NoRedirect())
        self.session = None
        self.protocol = "2025-11-25"
        self.sequence = 0

    def send(self, method, params, *, notification=False):
        deadline = time.monotonic() + REQUEST_TIMEOUT
        self.sequence += 1
        request_id = None if notification else self.sequence
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            body["id"] = request_id
        encoded = json.dumps(body).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol,
            # Nou's workaround, 2026-11-20, cloudflare-1010-user-agent-ban--nou.md:
            # Cloudflare Error 1010 bans the default Python-urllib user-agent
            # at the edge, returning 403 before the token is evaluated.
            # Send a standard browser User-Agent instead.
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(ENDPOINT, data=encoded, headers=headers, method="POST")
        add_surrogate_to_request(request, PROVIDER, allowed_hosts=[self.host])
        try:
            with self.opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                if session := response.headers.get("Mcp-Session-Id"):
                    if not all(33 <= ord(c) <= 126 for c in session):
                        raise McpError("MCP server returned an invalid session header")
                    self.session = session
                if notification and response.status == 202:
                    return None
                if notification:
                    raise McpError("MCP server did not accept the initialization notification")
                return self.read_result(response, request_id, deadline)
        except urllib.error.HTTPError as error:
            detail = safe_http_error_detail(error)
            raise McpError(
                f"MCP returned HTTP {error.code}{detail}; the operation was not replayed"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise McpError("MCP request did not complete; check any write before retrying") from None

    @staticmethod
    def decode(payload, request_id):
        message = json.loads(payload)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise McpError("MCP returned an invalid JSON-RPC message")
        if type(message.get("id")) is not int or message["id"] != request_id:
            return None
        if "error" in message:
            error = message["error"]
            if isinstance(error, dict):
                code = error.get("code")
                text = error.get("message")
                detail = ""
                if code is not None:
                    # A code is normally an int; redact anyway so a
                    # string code can never smuggle server text out.
                    detail += f" code {redact_secrets(code)}"
                if isinstance(text, str) and text:
                    # Redact BEFORE truncating: the JSON-RPC error
                    # message is server text and reaches stderr via the
                    # __main__ handler (Jensen attack D).
                    detail += f": {redact_secrets(text)[:ERROR_MESSAGE_LIMIT]}"
                raise McpError(f"MCP returned a JSON-RPC error{detail}; the operation was not replayed")
            raise McpError("MCP returned a JSON-RPC error; the operation was not replayed")
        if not isinstance(message.get("result"), dict):
            raise McpError("MCP returned no result object")
        return message["result"]

    @classmethod
    def read_result(cls, response, request_id, deadline=None):
        content_type = response.headers.get_content_type()
        if content_type == "application/json":
            result = cls.decode(response.read(), request_id)
            if result is None:
                raise McpError("MCP returned the wrong request ID")
            if deadline is not None and time.monotonic() > deadline:
                raise McpError("MCP request exceeded the total deadline; the operation was not replayed")
            return result
        if content_type != "text/event-stream":
            raise McpError("MCP returned an unsupported content type")
        data = []
        while True:
            if deadline is not None and time.monotonic() > deadline:
                raise McpError("MCP request exceeded the total deadline; the operation was not replayed")
            line = response.readline()
            if not line:
                raise McpError("MCP event stream ended before the matching result")
            line = line.rstrip(b"\r\n")
            if line.startswith(b"data:"):
                data.append(line[5:].lstrip(b" "))
            elif not line and data:
                result = cls.decode(b"\n".join(data), request_id)
                data.clear()
                if result is not None:
                    return result

    def initialize(self):
        result = self.send("initialize", {
            "protocolVersion": self.protocol,
            "capabilities": {},
            "clientInfo": {"name": "muse-custom-connector", "version": "1"},
        })
        protocol = result.get("protocolVersion")
        capabilities = result.get("capabilities")
        if (
            not isinstance(protocol, str)
            or protocol not in PROTOCOLS
            or not isinstance(capabilities, dict)
            or not isinstance(capabilities.get("tools"), dict)
        ):
            raise McpError("MCP server did not negotiate a supported tool protocol")
        self.protocol = protocol
        self.send("notifications/initialized", {}, notification=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    listing = subparsers.add_parser("list-tools")
    listing.add_argument("--cursor")
    calling = subparsers.add_parser("call-tool")
    calling.add_argument("--name", required=True)
    calling.add_argument("--arguments-file", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "call-tool":
        if args.name not in ALLOWED_TOOLS:
            parser.error("Inspect this operation's schema and add its exact name to ALLOWED_TOOLS")
        arguments = json.loads(args.arguments_file.read_bytes())
        if not isinstance(arguments, dict):
            parser.error("arguments must be a JSON object")
    client = Client()
    client.initialize()
    if args.command == "list-tools":
        result = client.send("tools/list", {"cursor": args.cursor} if args.cursor else {})
    else:
        result = client.send("tools/call", {"name": args.name, "arguments": arguments})
    try:
        print(json.dumps(redact_secrets(result), ensure_ascii=False, default=_json_default))
    except (TypeError, ValueError):
        # Last resort: render the result as text, redact it, print that.
        # Printing must never crash with a traceback (whose repr could
        # carry a secret) on an unserialisable server value.
        print(json.dumps(redact_secrets(str(result)), ensure_ascii=False))
    return 1 if result.get("isError") else 0


def _print_error(error):
    """Print an error to stderr with server text redacted first.

    Final gate: any exception text — including a JSON-RPC error message or
    an HTTP error detail that reached here through another path — passes
    through `redact_secrets` before it can reach a transcript or log.
    """
    print(f"error: {redact_secrets(str(error))}", file=sys.stderr)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (McpError, DynamicCredentialError, ValueError, OSError, TypeError) as error:
        _print_error(error)
        raise SystemExit(1)
