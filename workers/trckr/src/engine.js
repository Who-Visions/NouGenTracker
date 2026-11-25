// Per-turn cost engine. A turn is one model request: the unit every provider bills.
//
// Because a turn knows its own size and time, it gets priced the way the provider bills it:
// the prompt-size tier ("prompts > 200k"), the dated schedule ("starting December 1"), and the
// cache write TTL are all applied per request. Daily aggregates can't do that; per turn can.
import { costNano, modelCandidates, nanoToUsd, normalizeModel } from './pricing/core.js';
import { SOURCES } from './pricing/pool.js';

// Self-hosted runtimes: the tokens are real, the API bill is zero.
export const LOCAL_PROVIDERS = new Set(['ollama', 'lmstudio', 'lm-studio', 'llama.cpp', 'llamacpp', 'vllm', 'local', 'mlx', 'gemma-local']);

const FIRST_PARTY = ['anthropic', 'openai', 'google', 'openrouter', 'huggingface'];
const PROVIDER_ALIASES = { hf: 'huggingface', 'hugging-face': 'huggingface', claude: 'anthropic', gemini: 'google', vertex: 'google', codex: 'openai', chatgpt: 'openai' };

// A turn's time picks the dated price, so an unreadable one is an error the caller sees, never a guess.
function isoAt(v) {
  if (!v) return new Date().toISOString();
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) throw new TypeError(`at is not a valid date: ${String(v).slice(0, 40)}`);
  return d.toISOString();
}

export function normalizeTurn(t) {
  const n = (v) => Math.max(0, Math.trunc(Number(v) || 0));
  const provider = String(t.provider || '').toLowerCase().trim();
  return {
    provider: PROVIDER_ALIASES[provider] || provider || null,
    model: String(t.model || '').trim(),
    at: isoAt(t.at),
    input_tokens: n(t.input_tokens),
    output_tokens: n(t.output_tokens),
    cache_read_tokens: n(t.cache_read_tokens),
    cache_write_5m_tokens: n(t.cache_write_5m_tokens ?? t.cache_write_tokens),
    cache_write_1h_tokens: n(t.cache_write_1h_tokens),
    reasoning_tokens: n(t.reasoning_tokens),
    // Session totals summed over many requests: tiers are per request, so price at the base tier.
    rollup: t.rollup === true,
  };
}

// Size the provider tiers on: everything the model read this turn.
export const promptSize = (t) => t.input_tokens + t.cache_read_tokens + t.cache_write_5m_tokens + t.cache_write_1h_tokens;

function pickBand(model, t) {
  const day = t.at.slice(0, 10);
  const size = t.rollup ? 0 : promptSize(t);
  return model.bands.find(
    (b) =>
      (b.valid_from === null || day >= b.valid_from) &&
      (b.valid_until === null || day < b.valid_until) &&
      (b.min_prompt_tokens === null || size >= b.min_prompt_tokens) &&
      (b.max_prompt_tokens === null || size <= b.max_prompt_tokens),
  );
}

function findModel(snapshot, raw) {
  const cands = modelCandidates(raw);
  for (const c of cands) {
    const hit = snapshot.models.find((m) => m.model === c || (m.aliases || []).includes(c));
    if (hit) return hit;
  }
  return null;
}

// Resolve the price that applied to this turn. Returns { found, ... } with full provenance,
// or { found: false, reason }: never a guessed rate.
export async function resolvePrice(store, turn) {
  const t = normalizeTurn(turn);
  if (!t.model) return { found: false, reason: 'no model on the turn' };
  if (t.provider && LOCAL_PROVIDERS.has(t.provider)) {
    return { found: true, local: true, provider: t.provider, model: normalizeModel(t.model), basis: 'self-hosted: no API price, tokens tracked at $0' };
  }
  const order = t.provider && FIRST_PARTY.includes(t.provider) ? [t.provider] : FIRST_PARTY;
  for (const source of order) {
    let snap = await store.snapshotAt(source, t.at);
    let basis = 'read at or before the turn';
    if (!snap) {
      snap = await store.earliestSnapshot(source);
      basis = 'turn predates the first read of this page; earliest read used';
    }
    if (!snap) continue;
    const model = findModel(snap, t.model);
    if (!model) continue;
    const b = pickBand(model, t);
    if (!b) return { found: false, reason: `${model.display} has no published band for a ${promptSize(t)}-token prompt on ${t.at.slice(0, 10)}`, source };
    return {
      found: true,
      local: false,
      provider: model.provider,
      model: model.model,
      display: model.display,
      band: b,
      basis,
      ref: { source, snapshot_id: snap.snapshot_id, source_url: snap.source_url, page_url: snap.page_url, fetched_at: snap.fetched_at, last_seen_at: snap.last_seen_at || snap.fetched_at, parser: snap.parser },
    };
  }
  const read = (await Promise.all(order.map((s) => store.latestSnapshot(s)))).filter(Boolean).map((s) => s.source);
  return { found: false, reason: read.length ? `"${t.model}" is not on ${read.join(', ')} pricing pages as last read` : 'no provider page has been read yet' };
}

const COMPONENTS = [
  ['input_tokens', 'input'],
  ['output_tokens', 'output'],
  ['cache_read_tokens', 'cache_read'],
  ['cache_write_5m_tokens', 'cache_write_5m'],
  ['cache_write_1h_tokens', 'cache_write_1h'],
];

export async function priceTurn(store, turn) {
  const t = normalizeTurn(turn);
  const price = await resolvePrice(store, t);
  const tokens = {
    input: t.input_tokens,
    output: t.output_tokens,
    cache_read: t.cache_read_tokens,
    cache_write_5m: t.cache_write_5m_tokens,
    cache_write_1h: t.cache_write_1h_tokens,
    reasoning: t.reasoning_tokens,
  };
  const total_tokens = t.input_tokens + t.output_tokens + t.cache_read_tokens + t.cache_write_5m_tokens + t.cache_write_1h_tokens;
  const cacheBase = t.input_tokens + t.cache_read_tokens + t.cache_write_5m_tokens + t.cache_write_1h_tokens;
  const base = { turn: t, tokens, total_tokens, cache_hit_rate: cacheBase ? t.cache_read_tokens / cacheBase : null };
  if (!price.found) return { ...base, status: 'unpriced', cost_usd: null, cost_nano: null, reason: price.reason, price: null };
  if (price.local) return { ...base, status: 'local', cost_usd: 0, cost_nano: '0', components: {}, price };
  if (price.band.quarantined) return { ...base, status: 'quarantined', cost_usd: null, cost_nano: null, reason: price.band.quarantined, price };
  let total = 0n;
  const components = {};
  const unpriced = [];
  for (const [tk, field] of COMPONENTS) {
    const n = t[tk];
    if (!n) continue;
    const c = costNano(n, price.band[field]);
    if (c === null) {
      unpriced.push(field);
      continue;
    }
    components[field] = { tokens: n, usd_per_mtok: price.band[field], usd: nanoToUsd(c) };
    total += c;
  }
  // Without cache reads every cached token would have been fresh input: what caching saved.
  const savedNano = t.cache_read_tokens && price.band.cache_read !== null
    ? costNano(t.cache_read_tokens, price.band.input) - costNano(t.cache_read_tokens, price.band.cache_read)
    : 0n;
  return {
    ...base,
    status: unpriced.length ? 'partial' : 'priced',
    cost_usd: nanoToUsd(total),
    cost_nano: total.toString(),
    cache_savings_usd: nanoToUsd(savedNano),
    components,
    unpriced_components: unpriced,
    price,
  };
}

export { SOURCES };
