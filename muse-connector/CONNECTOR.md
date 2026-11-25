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

- Setup note, 2026-11-25: the Muse OAuth link opened a Secure credentials store screen asking for a Client ID instead of going straight to Google sign-in. A client was pre-registered via POST to `https://trckr.nougenai.com/register` (client_name "Muse Dav1d", redirect `https://agent.meta.ai/api/hatch/oauth/callback`, grant types authorization_code + refresh_token, token_endpoint_auth_method none). The client_id is a public identifier, not a secret — with `token_endpoint_auth_method none` there is no client secret to protect — so it is not a credential and the no-credentials-in-chat rule does not apply to it. Never put a secret (a key, token, password or cookie) in chat, a doc, a helper, a command argument, an environment variable or a log. After Add, Google sign-in completed and the connection was stored.
- Deploy provenance — NOT CONFIRMED, still blocking: the server was already live at trckr.nougenai.com before this connection (verified 2026-11-25 01:37 EST: HTTP 200 landing page, OAuth metadata, /mcp 401 invalid_token), and the Worker source is PR #46's branch `claude/trckr-connector` (Who-Visions/NouGenTracker, still an unmerged draft), whose README documents the deploy path as Cloudflare Workers Builds — worker `nougen-trckr`, root `workers/trckr`, deploy command `node deploy/cf_build.mjs`, every push to the production branch redeploys — with `deploy/deploy_prod.py` as a second, fleet-node deploy path. No repo record names who enabled the Workers Builds connection in the Cloudflare dashboard or ran the deploy that put the current server up, and the live server exposes no version or commit: `/healthz` returns only `{"ok":true,"service":"nougen-trckr"}`. The live board (checked 2026-11-25 ~04:12 EST) shows 4 price pools / 539 models with the last read at 4:00:45 AM EST — the PR #46 branch head adds a fifth (Hugging Face) pool, so the running code predates that commit, but its exact commit cannot be read from the live server. What would confirm it: the Cloudflare dashboard Deployments tab for worker `nougen-trckr` (Workers Builds records the build commit and who triggered it), or a version/commit stamp added to the Worker (e.g. on `/healthz`). Until one of those lands, do not treat the deploy as confirmed — and note this connector lands against a server whose own source PR (#46) is still unmerged. Revoking the grant and re-registering is Dave's call, not a step taken here.
- Before the first change in a conversation, prove access with a harmless read

```text
python3 muse-connector/bin/client.py list-tools
```

## Credentials

- The registered connection holds the credential. Helpers import `/opt/muse/skills/custom-connector-setup/bin/dynamic_credentials.py`, which returns an `hsurr:` surrogate and places it where the provider reads the key. Muse swaps in the real credential only on an approved request to a registered host.

- Never put a key, token, password or cookie in this doc, a helper, a command argument, an environment variable or a log. Never ask the user to paste one in chat.

- Keep redirects disabled on authenticated requests.

- On a 401 or 403, check the request first. Confirm that the helper attached the credential, that the host is registered and that the placement and scopes match.
- On ANY 403, read the error body before touching scopes, accounts, or links — Nou's workaround, 2026-11-20, `cloudflare-1010-user-agent-ban--nou.md` in the Nougen Tester folder: Cloudflare Error 1010 bans the default Python-urllib user-agent at the edge, returning 403 before the token is evaluated. The client sends a standard browser User-Agent header, following that workaround note. When the client reports an HTTP error it includes at most ~2 KB of the body and only when it matches a known non-secret pattern (Cloudflare `error code: 1010`, or a JSON `error` field); any other body is discarded and headers are never echoed.

- Only when a request that carried the credential is still rejected, call `credentials.request_api_access` with the details above and `"reconnect": true`.

## List tools

- `list-tools` reads the full tool catalog from the MCP server. It is a read and it changes nothing.

```text
python3 muse-connector/bin/client.py list-tools
```

- The call returned 8 tools on 2026-11-25 with no `nextCursor` remaining: trckr_prices, trckr_price, trckr_turn_cost, trckr_record_turn, trckr_session, trckr_usage, trckr_pools, trckr_refresh. `trckr_ingest_key` exists on the server but is deliberately not enabled in this client: it mints ingest keys, and a minted key must never be printed to stdout, a transcript or a log. Minting ingest keys is out of scope for this client.

- Write each `call-tool` argument as one JSON object in a workspace file. Keep credentials out of that file.

```text
python3 muse-connector/bin/client.py call-tool --name <tool> --arguments-file <path-to-json>
```

## What Trckr is

- Trckr is the NouGen per-turn usage connector: live price board from the providers' own pricing pages, price for one model at a prompt-size tier and date, cost of one turn per component to the nano-dollar, recording turns to the ledger, sessions, usage, pools, and refresh. Cost math is BigInt nano-dollars; unlisted models come back unpriced with the reason, never estimated; Ollama/local runs tracked at $0, labeled self-hosted.

## Dav1d turns (Muse Spark)

- Dav1d/Muse Spark turns are not reported to Trckr by Meta, so they are recorded manually from the session context-window size. The method is `estimated_context_growth`, implemented in `muse-connector/bin/turns.py` and documented in `muse-connector/dav1d-turns.json` (schema/example file only): the full context-window size is recorded as `context_tokens`; the growth since the previous record is `estimated_context_growth_tokens` — an estimate, NOT a turn's input/output/cache split and NOT turn tokens. When the window shrinks (compaction) the growth is `null` with `compaction_detected: true`, never a negative delta. Cost is always `null` with an unpriced reason — `$0` is reserved for self-hosted runs. Do not price from this file or from any delta in it. Mutable runtime state (previous context size, last record/request ids) lives in the gitignored `muse-connector/state/dav1d-turns.json` or in Trckr via `trckr_session`, never in the committed file.
- Open item: the first live ledger record (id `5fde6115cdc6f0eec0cb84dc5c7e35d1`) was written with 0 tokens before this method existed. It still needs to be voided or annotated in Trckr by someone with live ledger access.

## Recover from errors

- A 401 or 403 means the credential was missing, rejected or out of scope. Check that the helper attached the credential, that the host is registered and that the scopes match before reconnecting.

- A transport failure means the MCP request did not complete. Check the current state with a read before trying again, because the first request may have completed.

- A tool result with `isError` means the tool itself failed. The client exits with status 1 and does not replay the call.

## Limits

- 8 tools are allowed in `client.py` as of 2026-11-25. Enabling is not execution: a write (record a turn, refresh) still needs Dave's yes for that exact operation first, and the Muse runtime approval layer gates each write — the client itself carries no approval flag.

- The MCP client speaks Streamable HTTP, negotiates protocol version 2025-11-25, 2025-06-18 or 2025-03-26, sends `notifications/initialized` and keeps the server session header for one invocation only. Each request has a 45-second total deadline (on top of the 45-second per-socket timeout), so a stream that trickles keepalives is cut off at the deadline instead of blocking indefinitely, and the request is not retried.

## Related skills

- Use `muse.skill_search` to find other skills and `muse.connector_search` to find other connectors.
