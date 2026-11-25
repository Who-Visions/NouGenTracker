// MCP over Streamable HTTP (JSON responses, stateless). Every tool answers with a short readable
// text block and the full structured result, so any client (Claude, ChatGPT, Codex, Gemini, Cursor)
// can show it or compute on it.
import { normalizeModel, fmtUsd, modelCandidates } from './pricing/core.js';
import { SOURCES, refreshAll } from './pricing/pool.js';
import { priceTurn, resolvePrice } from './engine.js';
import { eastern } from './pages.js';
import { sha256b64u } from './auth.js';
import { createBudgets, budgetText } from './budget.js';

export const PROTOCOL_VERSIONS = ['2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05'];
const SERVER = { name: 'nougen-trckr', title: 'NouGen//Trckr', version: '1.0.0' };
const INSTRUCTIONS =
  'NouGen//Trckr prices model usage per turn from live provider pages. Budgets: register a cap with trckr_budget_set at the start of a session, then call trckr_budget once per turn (pass the session\'s cumulative usage if you have it). At the warning, wrap up and write a handoff; at stop=true, make no more model calls. Use trckr_turn_cost or trckr_record_turn with the token counts a model reported (input, output, cache read, cache write) to get the exact cost; every price carries its source page and read time. A model no page lists comes back unpriced, never estimated. Local runtimes (provider "ollama", "lmstudio", "local") are tracked at $0. Times are shown in 12-hour Eastern.';

const TURN_PROPS = {
  model: { type: 'string', description: 'Model id as the client reports it, e.g. claude-opus-5-5[1m], gpt-5.5, gemini-2.5-pro, qwen3:32b' },
  provider: { type: 'string', description: 'anthropic | openai | google | openrouter | huggingface (model as model:provider) | ollama | lmstudio | local. Optional; searched in that order when left out.' },
  input_tokens: { type: 'integer', minimum: 0, description: 'Uncached input tokens' },
  output_tokens: { type: 'integer', minimum: 0, description: 'Output tokens, reasoning included' },
  cache_read_tokens: { type: 'integer', minimum: 0, description: 'Tokens read from the prompt cache' },
  cache_write_5m_tokens: { type: 'integer', minimum: 0, description: 'Tokens written to a 5 minute cache (OpenAI cache writes go here)' },
  cache_write_1h_tokens: { type: 'integer', minimum: 0, description: 'Tokens written to a 1 hour cache' },
  reasoning_tokens: { type: 'integer', minimum: 0, description: 'Reasoning tokens, informational (already inside output)' },
  at: { type: 'string', description: 'When the turn ran (ISO 8601). Defaults to now; picks the dated price that applied then.' },
};

const READ = { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false };

export const TOOLS = [
  {
    name: 'trckr_prices',
    title: 'Live price board',
    description: 'Current API prices (USD per 1M tokens) read from the providers’ own pricing pages, with cache read and write tiers, prompt-size tiers and dated changes. Each provider block names the page and when it was read.',
    inputSchema: { type: 'object', properties: { provider: { type: 'string', enum: SOURCES.map((s) => s.id) }, search: { type: 'string', description: 'Filter models by id or name, e.g. "opus", "flash", ":free"' }, limit: { type: 'integer', minimum: 1, maximum: 400, default: 60 } } },
    annotations: { title: 'Live price board', ...READ },
  },
  {
    name: 'trckr_price',
    title: 'Price for one model',
    description: 'The exact published price band that applies to one model for a given prompt size and date, with its source page, snapshot id and read time. Unlisted models return found=false with the reason.',
    inputSchema: { type: 'object', required: ['model'], properties: { model: TURN_PROPS.model, provider: TURN_PROPS.provider, prompt_tokens: { type: 'integer', minimum: 0, description: 'Prompt size, for tiered models (default 0)' }, at: TURN_PROPS.at } },
    annotations: { title: 'Price for one model', ...READ },
  },
  {
    name: 'trckr_turn_cost',
    title: 'Cost of one turn',
    description: 'Price one turn from its token counts, per component (input, output, cache read, cache writes), to the nano-dollar. Shows cache hit rate and what caching saved. Does not save anything.',
    inputSchema: { type: 'object', required: ['model'], properties: TURN_PROPS },
    annotations: { title: 'Cost of one turn', ...READ },
  },
  {
    name: 'trckr_record_turn',
    title: 'Record a turn',
    description: 'Save one turn to the ledger and return its cost. Idempotent: the same lane + request_id updates in place. Use for sessions that report usage per turn.',
    inputSchema: {
      type: 'object',
      required: ['model', 'lane', 'request_id'],
      properties: { ...TURN_PROPS, lane: { type: 'string', description: 'Lane or machine + agent, e.g. phoebus-opus, blade-ollama' }, request_id: { type: 'string', description: 'The provider request or message id for this turn' }, session_id: { type: 'string' }, client: { type: 'string', description: 'claude-code, codex, gemini-cli, cursor, ollama...' } },
    },
    annotations: { title: 'Record a turn', readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: 'trckr_session',
    title: 'Session ledger',
    description: 'Per-turn ledger and totals for one session: tokens, cost, cache hit rate and savings per turn.',
    inputSchema: { type: 'object', required: ['session_id'], properties: { session_id: { type: 'string' }, limit: { type: 'integer', minimum: 1, maximum: 500, default: 50 } } },
    annotations: { title: 'Session ledger', ...READ },
  },
  {
    name: 'trckr_usage',
    title: 'Usage and spend',
    description: 'Spend, tokens, cache hit rate and cache savings grouped by model, provider, lane, session, client or day (Eastern). Default window: the last 7 days.',
    inputSchema: {
      type: 'object',
      properties: {
        group_by: { type: 'string', enum: ['model', 'provider', 'lane', 'session', 'client', 'day'], default: 'model' },
        since: { type: 'string', description: 'ISO 8601 start (inclusive)' },
        until: { type: 'string', description: 'ISO 8601 end (exclusive)' },
        days: { type: 'integer', minimum: 1, maximum: 366, description: 'Shortcut for since = now minus N days' },
        lane: { type: 'string' },
        session_id: { type: 'string' },
      },
    },
    annotations: { title: 'Usage and spend', ...READ },
  },
  {
    name: 'trckr_pools',
    title: 'Price pool health',
    description: 'Each price pool: whether its page was read, the URL that answered, when, the snapshot hash, how many models parsed and rejected, and the last failure if any.',
    inputSchema: { type: 'object', properties: {} },
    annotations: { title: 'Price pool health', ...READ },
  },
  {
    name: 'trckr_refresh',
    title: 'Re-read provider pages',
    description: 'Fetch the provider pricing pages now instead of waiting for the next scheduled read. An unchanged page writes nothing; a changed one becomes a new snapshot, and a move over 50% is held until confirmed.',
    inputSchema: { type: 'object', properties: { provider: { type: 'string', enum: SOURCES.map((s) => s.id) } } },
    annotations: { title: 'Re-read provider pages', readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  },
  {
    name: 'trckr_budget_set',
    title: 'Set a session budget',
    description: 'Register a dollar cap and stop rule for one session, at its start. Trckr prices the session\'s real usage at list price; at warn_pct (default 80) the session\'s own budget check says wrap up, and at the cap it says stop and the coordinator is alerted. Calling again changes the cap. close=true ends the budget when the session is done.',
    inputSchema: {
      type: 'object',
      required: ['session_id'],
      properties: {
        session_id: { type: 'string', description: 'The session to cap (the same id its turns are recorded under)' },
        cap_usd: { type: 'number', exclusiveMinimum: 0, description: 'Dollar cap for the whole session' },
        warn_pct: { type: 'integer', minimum: 1, maximum: 99, default: 80 },
        stop_rule: { type: 'string', description: 'What stopping means for this session, e.g. "write a handoff shard and report"' },
        label: { type: 'string', description: 'Readable name, e.g. the thread title' },
        close: { type: 'boolean', description: 'true when the session is finished' },
      },
    },
    annotations: { title: 'Set a session budget', readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: 'trckr_budget',
    title: 'Check a session budget',
    description: 'Cheap per-turn check: spend so far, cap, percent and state (ok, warn, over). stop=true means make no more model calls. Pass usage with the session\'s cumulative token counts (from its own usage report) and Trckr prices it at list price first; leave it out when turns are recorded per turn.',
    inputSchema: {
      type: 'object',
      required: ['session_id'],
      properties: {
        session_id: { type: 'string' },
        usage: { type: 'object', description: 'Cumulative session usage', required: ['model'], properties: { ...TURN_PROPS, lane: { type: 'string' }, client: { type: 'string' } } },
      },
    },
    annotations: { title: 'Check a session budget', readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: 'trckr_budgets',
    title: 'All session budgets',
    description: 'Every open budget with spend, cap and state, over-budget first. For the coordinator.',
    inputSchema: { type: 'object', properties: { include_closed: { type: 'boolean', default: false } } },
    annotations: { title: 'All session budgets', ...READ },
  },
  {
    name: 'trckr_ingest_key',
    title: 'Mint a hook key',
    description: 'Create a key for the per-turn hook (POST /v1/turns). The key is shown once; only its hash is stored. Put it in Keymaker as TRCKR_INGEST_KEY.',
    inputSchema: { type: 'object', required: ['label'], properties: { label: { type: 'string', description: 'Which machine or lane uses it, e.g. phoebus-claude-code' } } },
    annotations: { title: 'Mint a hook key', readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
  },
];

const ok = (text, structured) => ({ content: [{ type: 'text', text }], structuredContent: structured });
const fail = (text) => ({ content: [{ type: 'text', text }], isError: true });
const usd = (v) => (v === null || v === undefined ? 'unpriced' : fmtUsd(v));
const n = (v) => Number(v || 0).toLocaleString('en-US');
const r = (v) => (v === null || v === undefined ? 'n/a' : `$${v}`);

function bandText(b) {
  const parts = [`in ${r(b.input)}`, `out ${r(b.output)}`, `cache read ${r(b.cache_read)}`, `write 5m ${r(b.cache_write_5m)}`, `write 1h ${r(b.cache_write_1h)}`];
  const when = [];
  if (b.min_prompt_tokens !== null) when.push(`prompt >= ${n(b.min_prompt_tokens)}`);
  if (b.max_prompt_tokens !== null) when.push(`prompt <= ${n(b.max_prompt_tokens)}`);
  if (b.valid_from) when.push(`from ${b.valid_from}`);
  if (b.valid_until) when.push(`before ${b.valid_until}`);
  return `${parts.join(', ')}${when.length ? ` (${when.join(', ')})` : ''}${b.quarantined ? ` [held: ${b.quarantined}]` : ''}`;
}

function turnText(p) {
  const t = p.tokens;
  const head =
    p.status === 'local'
      ? `${p.turn.model}: $0 (self-hosted)`
      : p.status === 'unpriced' || p.status === 'quarantined'
        ? `${p.turn.model}: unpriced. ${p.reason}`
        : `${p.price.display}: ${usd(p.cost_usd)}${p.status === 'partial' ? ` (no published rate for ${p.unpriced_components.join(', ')})` : ''}`;
  const lines = [head, `Tokens: ${n(t.input)} in, ${n(t.cache_read)} cache read, ${n(t.cache_write_5m + t.cache_write_1h)} cache write, ${n(t.output)} out (${n(p.total_tokens)} total)`];
  if (p.cache_hit_rate !== null) lines.push(`Cache hit rate ${(p.cache_hit_rate * 100).toFixed(1)}%${p.cache_savings_usd ? `, saved ${usd(p.cache_savings_usd)}` : ''}`);
  if (p.components && Object.keys(p.components).length)
    lines.push(`Breakdown: ${Object.entries(p.components).map(([k, c]) => `${k} ${usd(c.usd)}`).join(', ')}`);
  if (p.price?.ref) lines.push(`Priced from ${p.price.ref.source_url}, read ${eastern(p.price.ref.fetched_at)} (snapshot ${p.price.ref.snapshot_id})`);
  return lines.join('\n');
}

function sinceFrom(args, defDays) {
  if (args.since) return new Date(args.since).toISOString();
  const days = args.days || defDays;
  return days ? new Date(Date.now() - days * 86400000).toISOString() : undefined;
}

export function createMcp({ store, tenant, fetchImpl, canRefresh = true }) {
  const budgets = createBudgets({ store });
  const handlers = {
    async trckr_prices(a) {
      const want = a.provider ? SOURCES.filter((s) => s.id === a.provider) : SOURCES;
      const q = a.search ? normalizeModel(a.search) : '';
      const limit = a.limit || 60;
      const providers = [];
      const lines = [];
      let shown = 0;
      for (const s of want) {
        const snap = await store.latestSnapshot(s.id);
        if (!snap) {
          providers.push({ provider: s.id, read: false, page_url: s.page, models: [] });
          lines.push(`${s.label}: not read yet, prices unknown. ${s.page}`);
          continue;
        }
        const models = snap.models.filter((m) => !q || m.model.includes(q) || normalizeModel(m.display).includes(q) || (m.aliases || []).some((x) => x.includes(q)));
        const take = models.slice(0, Math.max(0, limit - shown));
        shown += take.length;
        providers.push({ provider: s.id, read: true, source_url: snap.source_url, page_url: snap.page_url, fetched_at: snap.fetched_at, last_seen_at: snap.last_seen_at, snapshot_id: snap.snapshot_id, models: take, more: models.length - take.length });
        if (!models.length) continue;
        lines.push(`${s.label} (${snap.source_url}, read ${eastern(snap.last_seen_at)}), USD per 1M tokens:`);
        for (const m of take) for (const b of m.bands) lines.push(`  ${m.display} [${m.model}]: ${bandText(b)}`);
        if (models.length > take.length) lines.push(`  ...${models.length - take.length} more; narrow with search`);
      }
      return ok(lines.join('\n') || 'No models match.', { providers });
    },

    async trckr_price(a) {
      const price = await resolvePrice(store, { model: a.model, provider: a.provider, at: a.at, input_tokens: a.prompt_tokens || 0 });
      if (!price.found) return ok(`${a.model}: no published price. ${price.reason}`, { found: false, reason: price.reason, candidates: modelCandidates(a.model) });
      if (price.local) return ok(`${a.model}: self-hosted, $0 API cost. Tokens are still tracked.`, price);
      return ok(`${price.display} [${price.model}]: ${bandText(price.band)}\nSource: ${price.ref.source_url}, read ${eastern(price.ref.fetched_at)} (snapshot ${price.ref.snapshot_id})`, price);
    },

    async trckr_turn_cost(a) {
      const p = await priceTurn(store, a);
      return ok(turnText(p), p);
    },

    async trckr_record_turn(a) {
      const p = await priceTurn(store, a);
      const id = await store.recordTurn(tenant, { lane: a.lane, request_id: a.request_id, session_id: a.session_id, client: a.client }, p);
      const budget = a.session_id ? await budgets.evaluate(tenant, a.session_id) : null;
      return ok(`Recorded on ${a.lane}.\n${turnText(p)}${budget ? `\n${budgetText(budget)}` : ''}`, { id, ...p, budget });
    },

    async trckr_budget_set(a) {
      if (a.close) {
        const b = await budgets.close(tenant, a.session_id);
        return b ? ok(budgetText(b), b) : fail(`No budget for session ${a.session_id}.`);
      }
      const b = await budgets.set(tenant, a);
      return ok(`Cap set. ${budgetText(b)}`, b);
    },

    async trckr_budget(a) {
      if (a.usage) await budgets.rollup(tenant, a.session_id, a.usage);
      const b = await budgets.evaluate(tenant, a.session_id);
      if (!b) return ok(`No budget for session ${a.session_id}. Set one with trckr_budget_set.`, { session_id: a.session_id, state: 'none', stop: false, warn: false });
      return ok(budgetText(b), b);
    },

    async trckr_budgets(a) {
      const rows = await store.listBudgets(tenant, { active: !a.include_closed });
      const list = [];
      for (const r of rows) list.push(await budgets.evaluate(tenant, r.session_id));
      const rank = { over: 0, warn: 1, ok: 2, closed: 3 };
      list.sort((x, y) => rank[x.state] - rank[y.state] || y.pct - x.pct);
      if (!list.length) return ok('No open budgets.', { budgets: [] });
      return ok(list.map((b) => `${b.state.toUpperCase()}  ${b.label || b.session_id}: ${budgetText(b).replace(/^[^.]*\. /, '')}`).join('\n'), { budgets: list });
    },

    async trckr_session(a) {
      const turns = await store.listTurns(tenant, { session_id: a.session_id, limit: a.limit || 50 });
      const [tot] = await store.usage(tenant, { group_by: 'session', session_id: a.session_id });
      if (!turns.length) return ok(`No turns recorded for session ${a.session_id}.`, { session_id: a.session_id, turns: [], totals: null });
      const lines = [`Session ${a.session_id}: ${tot.turns} turns, ${usd(tot.cost_usd)}, ${n(tot.total_tokens)} tokens, cache hit ${tot.cache_hit_rate === null ? 'n/a' : `${(tot.cache_hit_rate * 100).toFixed(1)}%`}, saved ${usd(tot.cache_savings_usd)}`];
      for (const t of turns.slice(0, 25)) lines.push(`  ${eastern(t.at)}  ${t.model}  ${n(t.input_tokens + t.cache_read_tokens + t.cache_write_5m_tokens + t.cache_write_1h_tokens)} in / ${n(t.output_tokens)} out  ${t.status === 'local' ? '$0 local' : usd(t.cost_usd)}`);
      return ok(lines.join('\n'), { session_id: a.session_id, totals: tot, turns });
    },

    async trckr_usage(a) {
      const group_by = a.group_by || 'model';
      const since = sinceFrom(a, 7);
      const rows = await store.usage(tenant, { group_by, since, until: a.until, lane: a.lane, session_id: a.session_id });
      const total = rows.reduce((s, x) => ({ turns: s.turns + x.turns, cost_usd: s.cost_usd + x.cost_usd, tokens: s.tokens + x.total_tokens, saved: s.saved + x.cache_savings_usd }), { turns: 0, cost_usd: 0, tokens: 0, saved: 0 });
      const lines = [`${total.turns} turns, ${usd(Math.round(total.cost_usd * 1e9) / 1e9)}, ${n(total.tokens)} tokens since ${since ? eastern(since) : 'the start'}. Cache saved ${usd(total.saved)}.`];
      for (const x of rows.slice(0, 40)) lines.push(`  ${x.key}: ${usd(x.cost_usd)}, ${x.turns} turns, ${n(x.total_tokens)} tokens, hit ${x.cache_hit_rate === null ? 'n/a' : `${(x.cache_hit_rate * 100).toFixed(1)}%`}${x.unpriced ? `, ${x.unpriced} unpriced` : ''}${x.local ? `, ${x.local} local` : ''}`);
      return ok(lines.join('\n'), { group_by, since: since || null, until: a.until || null, totals: total, rows });
    },

    async trckr_pools() {
      const pools = [];
      for (const s of SOURCES) {
        const snap = await store.latestSnapshot(s.id);
        const last = await store.lastFetch(s.id);
        pools.push({
          provider: s.id,
          label: s.label,
          page_url: s.page,
          urls: s.urls.map(([u]) => u),
          read: !!snap,
          source_url: snap?.source_url || null,
          fetched_at: snap?.fetched_at || null,
          last_seen_at: snap?.last_seen_at || null,
          snapshot_id: snap?.snapshot_id || null,
          body_sha256: snap?.body_sha256 || null,
          models: snap?.models.length || 0,
          rejected: snap?.rejected || [],
          last_attempt: last ? { at: last.at, ok: last.ok, attempts: last.attempts } : null,
        });
      }
      const lines = pools.map((p) =>
        p.read
          ? `${p.label}: ${p.models} models from ${p.source_url}, read ${eastern(p.last_seen_at)}, unchanged since ${eastern(p.fetched_at)}, sha256 ${p.body_sha256.slice(0, 12)}${p.rejected.length ? `, ${p.rejected.length} rows refused` : ''}${p.last_attempt && !p.last_attempt.ok ? `. Last try ${eastern(p.last_attempt.at)} failed` : ''}`
          : `${p.label}: not read yet${p.last_attempt ? ` (last try ${eastern(p.last_attempt.at)}: ${p.last_attempt.attempts.map((x) => `${x.url} ${x.status || x.error}`).join('; ')})` : ''}`,
      );
      return ok(lines.join('\n'), { pools });
    },

    async trckr_refresh(a) {
      if (!canRefresh) return fail('Refresh is off on this deployment.');
      const results = await refreshAll(store, { only: a.provider, fetchImpl });
      const lines = results.map((x) => (x.ok ? `${x.source}: ${x.changed ? 'new snapshot' : 'unchanged'}, ${x.models} models (${x.snapshot_id})` : `${x.source}: page did not answer, prices stay as last read`));
      return ok(lines.join('\n'), { results });
    },

    async trckr_ingest_key(a) {
      const label = String(a.label || '').trim().slice(0, 60);
      if (!label) return fail('Give the key a label, e.g. the machine and lane that will use it.');
      const raw = new Uint8Array(32);
      crypto.getRandomValues(raw);
      const key = `trk_${btoa(String.fromCharCode(...raw)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')}`;
      await store.createIngestKey(tenant, label, await sha256b64u(key));
      return ok(`Hook key for ${label} (shown once, store it in Keymaker as TRCKR_INGEST_KEY):\n${key}`, { label, key });
    },
  };

  async function call(name, args) {
    const h = handlers[name];
    if (!h) return null;
    try {
      return await h(args || {});
    } catch (err) {
      return fail(`${name} failed: ${String(err?.message || err).slice(0, 300)}`);
    }
  }

  // One JSON-RPC message in, one response object out (null for notifications).
  async function rpc(msg) {
    const id = msg?.id ?? null;
    const reply = (result) => ({ jsonrpc: '2.0', id, result });
    const error = (code, message) => ({ jsonrpc: '2.0', id, error: { code, message } });
    if (!msg || msg.jsonrpc !== '2.0' || typeof msg.method !== 'string') return error(-32600, 'Invalid request');
    if (msg.id === undefined) return null; // notification
    switch (msg.method) {
      case 'initialize': {
        const asked = msg.params?.protocolVersion;
        return reply({
          protocolVersion: PROTOCOL_VERSIONS.includes(asked) ? asked : PROTOCOL_VERSIONS[0],
          capabilities: { tools: { listChanged: false } },
          serverInfo: SERVER,
          instructions: INSTRUCTIONS,
        });
      }
      case 'ping':
        return reply({});
      case 'tools/list':
        return reply({ tools: TOOLS });
      case 'tools/call': {
        const out = await call(msg.params?.name, msg.params?.arguments);
        return out ? reply(out) : error(-32602, `Unknown tool: ${msg.params?.name}`);
      }
      case 'resources/list':
        return reply({ resources: [] });
      case 'prompts/list':
        return reply({ prompts: [] });
      default:
        return error(-32601, `Method not found: ${msg.method}`);
    }
  }

  return { rpc, call };
}
