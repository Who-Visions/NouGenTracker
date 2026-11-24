// Deterministic fetch pools. Each provider has an ordered list of its own official URLs; the
// first one that answers 200 with a parseable body wins. The body is hashed, parsed by a pure
// function, and stored as a snapshot keyed by (source, body hash, parser version), so the same
// page bytes always yield the same prices and a re-fetch of an unchanged page writes nothing.
import { PARSER_VERSION, sha256Hex } from './core.js';
import { parseAnthropic } from './anthropic.js';
import { parseGemini } from './gemini.js';
import { parseOpenAI } from './openai.js';
import { parseOpenRouter } from './openrouter.js';

export const SOURCES = [
  {
    id: 'anthropic',
    label: 'Anthropic',
    page: 'https://platform.claude.com/docs/en/about-claude/pricing',
    urls: [
      ['https://platform.claude.com/docs/en/about-claude/pricing.md', 'text/markdown'],
      ['https://platform.claude.com/docs/en/about-claude/pricing', 'text/markdown, text/html'],
    ],
    parse: parseAnthropic,
  },
  {
    id: 'openai',
    label: 'OpenAI',
    page: 'https://developers.openai.com/api/docs/pricing',
    urls: [
      ['https://developers.openai.com/api/docs/pricing?latest-pricing=standard', 'text/markdown, text/html'],
      ['https://developers.openai.com/api/docs/pricing', 'text/markdown, text/html'],
      ['https://platform.openai.com/docs/pricing', 'text/html'],
    ],
    parse: parseOpenAI,
  },
  {
    id: 'google',
    label: 'Google Gemini',
    page: 'https://ai.google.dev/gemini-api/docs/pricing',
    urls: [
      ['https://ai.google.dev/gemini-api/docs/pricing.md.txt', 'text/plain, text/markdown'],
      ['https://ai.google.dev/gemini-api/docs/pricing', 'text/markdown'],
    ],
    parse: parseGemini,
  },
  {
    id: 'openrouter',
    label: 'OpenRouter',
    page: 'https://openrouter.ai/models',
    urls: [['https://openrouter.ai/api/v1/models', 'application/json']],
    parse: parseOpenRouter,
  },
];

const UA = 'NouGenTrckr/1.0 (price pool; +https://github.com/Who-Visions/NouGenTracker)';

// Delta guard from pricing_live.py: a rate that moves more than this share between two reads
// of the same page is quarantined (shown, flagged, not billed) until a second read confirms it.
export const DELTA_MAX = 0.5;

export async function fetchSource(source, { fetchImpl = fetch, timeoutMs = 15000, now = () => new Date() } = {}) {
  const attempts = [];
  for (const [url, accept] of source.urls) {
    const started = now();
    let status = 0;
    let body = '';
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), timeoutMs);
      const res = await fetchImpl(url, { headers: { accept, 'user-agent': UA }, signal: ctl.signal, redirect: 'follow' });
      clearTimeout(t);
      status = res.status;
      body = res.ok ? await res.text() : '';
    } catch (err) {
      attempts.push({ url, status: 0, error: String(err?.message || err).slice(0, 200), at: started.toISOString() });
      continue;
    }
    if (status !== 200 || !body) {
      attempts.push({ url, status, error: 'no body', at: started.toISOString() });
      continue;
    }
    const parsed = source.parse(body);
    if (!parsed.models.length) {
      attempts.push({ url, status, error: 'page read but no price rows parsed', at: started.toISOString() });
      continue;
    }
    const body_sha256 = await sha256Hex(body);
    const snapshot_id = (await sha256Hex(`${source.id}|${body_sha256}|${PARSER_VERSION}`)).slice(0, 24);
    return {
      ok: true,
      source: source.id,
      source_url: url,
      page_url: source.page,
      fetched_at: now().toISOString(),
      http_status: status,
      body_sha256,
      parser: PARSER_VERSION,
      snapshot_id,
      models: parsed.models,
      rejected: parsed.rejected,
      attempts,
    };
  }
  return { ok: false, source: source.id, page_url: source.page, fetched_at: now().toISOString(), attempts };
}

// Compare a fresh snapshot to the previous accepted one; mark large moves as quarantined.
export function applyDeltaGuard(fresh, previous) {
  if (!previous) return fresh;
  const prev = new Map(previous.models.map((m) => [m.model, m]));
  for (const m of fresh.models) {
    const old = prev.get(m.model);
    if (!old) continue;
    for (const b of m.bands) {
      const ob = old.bands.find(
        (x) => x.min_prompt_tokens === b.min_prompt_tokens && x.max_prompt_tokens === b.max_prompt_tokens && x.valid_from === b.valid_from,
      );
      if (!ob) continue;
      for (const f of ['input', 'output']) {
        if (ob[f] && b[f] && Math.abs(b[f] - ob[f]) / ob[f] > DELTA_MAX && !ob.quarantined) {
          b.quarantined = `${f} moved ${ob[f]} -> ${b[f]} USD/MTok since ${previous.fetched_at}; confirm on next read`;
        }
      }
    }
  }
  return fresh;
}

export async function refreshAll(store, opts = {}) {
  const wanted = opts.only ? SOURCES.filter((s) => s.id === opts.only) : SOURCES;
  const results = [];
  for (const source of wanted) {
    const snap = await fetchSource(source, opts);
    if (snap.ok) {
      const previous = await store.latestSnapshot(source.id);
      if (previous && previous.body_sha256 === snap.body_sha256 && previous.parser === snap.parser) {
        await store.touchSnapshot(previous.snapshot_id, snap.fetched_at);
        results.push({ source: source.id, ok: true, changed: false, snapshot_id: previous.snapshot_id, models: previous.models.length });
      } else {
        applyDeltaGuard(snap, previous);
        await store.saveSnapshot(snap);
        results.push({ source: source.id, ok: true, changed: true, snapshot_id: snap.snapshot_id, models: snap.models.length });
      }
    } else {
      results.push({ source: source.id, ok: false, attempts: snap.attempts });
    }
    await store.saveFetchLog(snap);
  }
  return results;
}
