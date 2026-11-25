# NouGen//Trckr

- `custom.trckr-nougenai` connects Muse to NouGen//Trckr through its hosted MCP server at https://trckr.nougenai.com/mcp
- It authenticates with the registered `oauth2_code` credential with scope `trckr` and resource `https://trckr.nougenai.com/mcp`
- Helpers live in `muse-connector/bin/` and `client.py` is the MCP client with `list-tools` and `call-tool` commands

## Connect

- The connection was registered with `credentials.request_api_access` and these details. Reuse them to reconnect. For an OAuth-protected MCP server, add the canonical `resource` from its protected-resource metadata or official docs if missing, and save the updated details

```json
{
  "provider": "trckr-nougenai",
  "auth_scheme": "oauth2_code",
  "issuer_domain": "trckr.nougenai.com",
  "authorization_url": "https://trckr.nougenai.com/authorize",
  "token_url": "https://trckr.nougenai.com/token",
  "token_endpoint_auth_method": "none",
  "scopes": "trckr",
  "resource": "https://trckr.nougenai.com/mcp",
  "registration_url": "https://trckr.nougenai.com/register",
  "api_hosts": ["trckr.nougenai.com"]
}
```

- Setup note, 2026-11-25: the Muse OAuth link opened a Secure credentials store screen asking for a Client ID instead of going straight to Google sign-in. A client was pre-registered via POST to `https://trckr.nougenai.com/register` (client_name "Muse Dav1d", redirect `https://agent.meta.ai/api/hatch/oauth/callback`, grant types authorization_code + refresh_token, token_endpoint_auth_method none), and that returned client_id was given to Dave in chat to paste into the Client ID field. After Add, Google sign-in completed and the connection was stored. Do not put that client_id, or any credential, in chat, a doc, a helper, a command argument, an environment variable or a log.
- The server was already live at trckr.nougenai.com before this connection (verified 2026-11-25 01:37 EST: HTTP 200 landing page, OAuth metadata, /mcp 401 invalid_token). Dav1d did not deploy it; the deploy had been blocked on Dave's re-confirmation and secrets, so who deployed it is unconfirmed.
- Before the first change in a conversation, prove access with a harmless read

```text
python3 muse-connector/bin/client.py list-tools
```

## Credentials

- The registered connection holds the credential. Helpers import `/opt/muse/skills/custom-connector-setup/bin/dynamic_credentials.py`, which returns an `hsurr:` surrogate and places it where the provider reads the key. Muse swaps in the real credential only on an approved request to a registered host.

- Never put a key, token, password or cookie in this doc, a helper, a command argument, an environment variable or a log. Never ask the user to paste one in chat.

- Keep redirects disabled on authenticated requests.

- On a 401 or 403, check the request first. Confirm that the helper attached the credential, that the host is registered and that the placement and scopes match.
- On ANY 403, read the error body before touching scopes, accounts, or links — Nou's workaround, 2026-11-20, `cloudflare-1010-user-agent-ban--nou.md` in the Nougen Tester folder: Cloudflare Error 1010 bans the default Python-urllib user-agent at the edge, returning 403 before the token is evaluated. The fix, applied in `client.py` on 2026-11-23 and verified live, is to send a standard browser User-Agent header.

- Only when a request that carried the credential is still rejected, call `credentials.request_api_access` with the details above and `"reconnect": true`.

## List tools

- `list-tools` reads the full tool catalog from the MCP server. It is a read and it changes nothing.

```text
python3 muse-connector/bin/client.py list-tools
```

- The call returned 9 tools on 2026-11-25 with no `nextCursor` remaining: trckr_prices, trckr_price, trckr_turn_cost, trckr_record_turn, trckr_session, trckr_usage, trckr_pools, trckr_refresh, trckr_ingest_key.

- Write each `call-tool` argument as one JSON object in a workspace file. Keep credentials out of that file.

```text
python3 muse-connector/bin/client.py call-tool --name <tool> --arguments-file <path-to-json>
```

## What Trckr is

- Trckr is the NouGen per-turn usage connector: live price board from the providers' own pricing pages, price for one model at a prompt-size tier and date, cost of one turn per component to the nano-dollar, recording turns to the ledger, sessions, usage, pools, refresh, and ingest keys. Cost math is BigInt nano-dollars; unlisted models come back unpriced with the reason, never estimated; Ollama/local runs tracked at $0, labeled self-hosted.

## Recover from errors

- A 401 or 403 means the credential was missing, rejected or out of scope. Check that the helper attached the credential, that the host is registered and that the scopes match before reconnecting.

- A transport failure means the MCP request did not complete. Check the current state with a read before trying again, because the first request may have completed.

- A tool result with `isError` means the tool itself failed. The client exits with status 1 and does not replay the call.

## Limits

- All 9 tools are allowed in `client.py` as of 2026-11-25. Enabling is not execution: a write (record a turn, refresh, ingest key) still needs Dave's yes for that exact operation first.

- The MCP client speaks Streamable HTTP, negotiates protocol version 2025-11-25, 2025-06-18 or 2025-03-26, sends `notifications/initialized` and keeps the server session header for one invocation only. A connection that stops responding for 45 seconds fails without a retry.

## Related skills

- Use `muse.skill_search` to find other skills and `muse.connector_search` to find other connectors.
