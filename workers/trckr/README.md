# NouGen//Trckr

Per-turn token and cost tracking as an MCP connector, at **https://trckr.nougenai.com/mcp**.

Every model request a session makes (Claude, GPT, Gemini, OpenRouter lanes, local Ollama) is priced the way the provider bills it: per component (input, output, cache read, 5 minute and 1 hour cache writes), at the prompt-size tier the request fell in, on the dated schedule that applied that day. Prices come straight off each provider's own pricing page, and every price carries the page it was read from, when, and the snapshot hash of the bytes.

## Rules the code keeps

- **Never a guessed price.** A field a page does not publish is `n/a`. A model no page lists is `unpriced`, with the reason. A row that fails a sanity gate (output below input, cache read at or above input, a rate over $1,000/MTok) is refused, not repaired.
- **Deterministic pools.** Each provider has an ordered list of its own official URLs. The first that answers 200 with parseable rows wins. Same bytes + same parser version = same `snapshot_id`; re-reading an unchanged page only bumps `last_seen_at`.
- **Delta guard.** A rate that moves more than 50% between reads is held (shown, flagged, not billed) until a second read confirms it.
- **Exact money.** Rates scale to integer micro-dollars; cost is computed in nano-dollars with BigInt and rounded half-even once.
- **Local is $0, not unknown.** `provider: ollama | lmstudio | local | vllm | mlx` turns keep their tokens at $0, labeled self-hosted.

| Pool | Read from |
| --- | --- |
| Anthropic | `platform.claude.com/docs/en/about-claude/pricing.md`, then the page |
| OpenAI | `developers.openai.com/api/docs/pricing?latest-pricing=standard` (Markdown), then HTML hydration |
| Google Gemini | `ai.google.dev/gemini-api/docs/pricing.md.txt` (Standard tab) |
| OpenRouter | `openrouter.ai/api/v1/models` |

## Surfaces

| Path | What |
| --- | --- |
| `/mcp` | MCP, Streamable HTTP (JSON). OAuth 2.1 bearer. |
| `/.well-known/oauth-*`, `/register`, `/authorize`, `/token` | OAuth 2.1 with dynamic registration and PKCE S256, same shape as shards and Eye |
| `/` | Live board: price pools, price table, and (signed in) today's spend, the turn feed and per-day totals |
| `/connect` | Setup for Claude, Claude Code, ChatGPT, Codex, Gemini CLI, Cursor and the hook |
| `POST /v1/turns` | Hook ingest. `Authorization: Bearer trk_...` from `trckr_ingest_key`. One turn, an array, or `{turns:[...]}`. Idempotent on lane + `request_id`. |
| `GET /v1/prices` | The current pools as JSON |

Tools: `trckr_prices`, `trckr_price`, `trckr_turn_cost`, `trckr_record_turn`, `trckr_session`, `trckr_usage`, `trckr_pools`, `trckr_refresh`, `trckr_ingest_key`. The fleet's daily rollups stay on NouGenShards `tracker_daily` / `tracker_spend`; Trckr is the per-turn layer under them.

Google sign-in uses the fleet's shared Google client. Its registered redirect is Eye's callback, so Trckr sends `state` prefixed `trckr~` and Eye relays the browser to `https://trckr.nougenai.com/oauth/google/callback` (Who-Visions/nougeneye `CALLBACK_RELAYS`).

## Run it

```sh
cd workers/trckr
npm test                      # parsers on saved provider pages, engine, OAuth, MCP, hook
TRCKR_SEED_SAMPLE=1 npm run dev   # http://localhost:8787, D1 on node:sqlite, sign-in stubbed locally
```

## Deploy (named yes only)

```sh
npx wrangler d1 create nougen-trckr                     # put the id in wrangler.jsonc env.production
npx wrangler d1 migrations apply nougen-trckr --remote --env production
npx wrangler secret put TRCKR_SIGNING_SECRET --env production   # from Keymaker
npx wrangler secret put GOOGLE_CLIENT_ID --env production       # shared fleet Google client
npx wrangler secret put GOOGLE_CLIENT_SECRET --env production
npm run deploy:prod                                     # binds trckr.nougenai.com as a custom domain
```

## Claude Code hook

`hooks/trckr_stop_hook.py` (stdlib only) runs on Stop, reads new turns from the session transcript (one per `requestId`, with 5m and 1h cache writes split), and posts them. Needs `TRCKR_INGEST_KEY`; optional `TRCKR_LANE`, `TRCKR_URL`. It never blocks the session.
