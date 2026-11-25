#!/usr/bin/env python3
"""Muse client for the NouGen//Trckr MCP server at trckr.nougenai.com.

Speaks Streamable HTTP JSON-RPC to the live Trckr server: list the tool
catalog (`list-tools`) or call one registered tool (`call-tool`) with its
arguments as a JSON object in a file. The server holds the credential; this
client only carries the Muse surrogate for the registered provider.

Secrets never reach stdout: `trckr_ingest_key` is not an allowed tool, and
every result is passed through `redact_secrets` before printing, so a key,
token or password in any field — including inside an error result — is
replaced with `[REDACTED]`. Nothing in this client writes a secret to a
file; minting ingest keys is out of scope here.
"""

import argparse
import json
from pathlib import Path
import re
import sys
import time
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

_SECRET_FIELD = re.compile(r"key|secret|token|password|credential", re.IGNORECASE)
_INGEST_KEY_PREFIX = "trk_"


def redact_secrets(value):
    """Recursively replace secret-looking values with ``[REDACTED]``.

    A dict value is redacted when its field name looks secret-bearing
    (key/secret/token/password/credential) or when a string value starts
    with the Trckr ingest-key prefix ``trk_``. Everything else passes
    through unchanged, so prices, token counts and usage figures are
    never altered. Apply to every result before printing — including
    ``isError`` results, which may echo a server-side value.
    """
    if isinstance(value, dict):
        return {
            field: "[REDACTED]"
            if _SECRET_FIELD.search(field)
            else redact_secrets(item)
            for field, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str) and value.startswith(_INGEST_KEY_PREFIX):
        return "[REDACTED]"
    return value


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
    if isinstance(parsed, dict) and isinstance(parsed.get("error"), str) and parsed["error"]:
        return f"; server error: {parsed['error'][:ERROR_MESSAGE_LIMIT]}"
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
                    detail += f" code {code}"
                if isinstance(text, str) and text:
                    detail += f": {text[:ERROR_MESSAGE_LIMIT]}"
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
    print(json.dumps(redact_secrets(result), ensure_ascii=False))
    return 1 if result.get("isError") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (McpError, DynamicCredentialError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
