import assert from 'node:assert/strict';
import { test } from 'node:test';
import { costNano, gateBand, modelCandidates, nanoToUsd, normalizeModel, parseThreshold } from '../src/pricing/core.js';
import { parseAnthropic } from '../src/pricing/anthropic.js';
import { parseGemini } from '../src/pricing/gemini.js';
import { parseOpenAI } from '../src/pricing/openai.js';
import { parseOpenRouter } from '../src/pricing/openrouter.js';
import { applyDeltaGuard, refreshAll } from '../src/pricing/pool.js';
import { clock, fixture, fixtureFetch, freshStore } from './helpers.mjs';

test('model ids normalize the way clients report them', () => {
  assert.equal(normalizeModel('claude-opus-5-5[1m]'), 'claude-opus-5-5');
  assert.equal(normalizeModel('Claude Opus 5.5'), 'claude-opus-5-5');
  assert.equal(normalizeModel('models/gemini-2.5-pro'), 'gemini-2-5-pro');
  assert.deepEqual(modelCandidates('anthropic/claude-sonnet-4-5-20250929'), ['anthropic/claude-sonnet-4-5-20250929', 'claude-sonnet-4-5-20250929', 'claude-sonnet-4-5']);
});

test('money math is exact (nano-USD, half-even)', () => {
  assert.equal(costNano(1_000_000, 4), 4_000_000_000n);
  assert.equal(costNano(1, 0.3), 300n);
  assert.equal(costNano(123_456, 0.075), 9_259_200n);
  assert.equal(costNano(5, null), null);
  assert.equal(nanoToUsd(1_234_567_891n), 1.234567891);
});

test('gates refuse impossible bands', () => {
  assert.equal(gateBand({ input: 3, output: 15, cache_read: 0.3 }), null);
  assert.match(gateBand({ input: 15, output: 3 }), /output/);
  assert.match(gateBand({ input: 3, output: 15, cache_read: 3 }), /cache/);
  assert.deepEqual(parseThreshold('prompts <= 200k tokens'), { max_prompt_tokens: 200000 });
  assert.deepEqual(parseThreshold('(<272K context length)'), { max_prompt_tokens: 271999 });
});

test('Anthropic: live page of Nov 24 2026 reads every model with cache tiers', () => {
  const { models, rejected } = parseAnthropic(fixture('anthropic_pricing_live_2026-11-24.md'));
  assert.equal(rejected.length, 0);
  assert.ok(models.length >= 20, `got ${models.length}`);
  const opus = models.find((m) => m.model === 'claude-opus-5-5');
  assert.ok(opus);
  const b = opus.bands[0];
  assert.equal(b.input, 4);
  assert.equal(b.output, 20);
  assert.equal(b.cache_read, 0.2);
  assert.ok(b.cache_write_5m > b.input && b.cache_write_1h > b.cache_write_5m);
});

test('OpenAI: Standard tier only, long-context band split at the published limit', () => {
  const { models } = parseOpenAI(fixture('openai_standard_catalog.md'));
  assert.ok(models.length >= 30);
  for (const m of models) for (const b of m.bands) assert.ok(b.output >= b.input, m.model);
  const multi = models.find((m) => m.bands.length === 2);
  assert.ok(multi, 'a model with short + long context bands');
  assert.equal(multi.bands[1].min_prompt_tokens, multi.bands[0].max_prompt_tokens + 1);
  const html = parseOpenAI(fixture('openai_pricing.html'));
  assert.ok(html.models.length > 0, 'hydration fallback reads the HTML page');
});

test('Gemini: Standard tab, prompt tiers and dated schedules become bands', () => {
  const { models, rejected } = parseGemini(fixture('gemini_pricing.md'));
  assert.ok(models.length >= 15);
  const tiered = models.find((m) => m.bands.some((b) => b.max_prompt_tokens === 200000));
  assert.ok(tiered, 'a <=200k / >200k model');
  for (const m of models) for (const b of m.bands) assert.ok(b.input >= 0 && b.output >= b.input, m.model);
  assert.ok(rejected.every((r) => r.reason), 'every rejection says why');
});

test('OpenRouter: per-token strings convert without float drift; free lanes are $0', () => {
  const doc = JSON.stringify({
    data: [
      { id: 'vendor/model-a', name: 'Model A', pricing: { prompt: '0.000003', completion: '0.000015', input_cache_read: '0.0000003' } },
      { id: 'vendor/model-b:free', name: 'Model B (free)', pricing: { prompt: '0', completion: '0' } },
      { id: 'vendor/router', name: 'Router', pricing: { prompt: '-1', completion: '-1' } },
    ],
  });
  const { models, rejected } = parseOpenRouter(doc);
  assert.deepEqual(models[0].bands[0].input, 3);
  assert.deepEqual(models[0].bands[0].output, 15);
  assert.deepEqual(models[0].bands[0].cache_read, 0.3);
  assert.equal(models[1].notes, 'free lane');
  assert.equal(rejected.length, 1);
});

test('pool: same bytes = same snapshot id, re-read touches instead of writing', async () => {
  const store = freshStore();
  const now = clock('2026-11-24T03:00:00Z');
  const first = await refreshAll(store, { fetchImpl: fixtureFetch(), now });
  const ok = first.filter((r) => r.ok).map((r) => r.source).sort();
  assert.deepEqual(ok, ['anthropic', 'google', 'openai']);
  assert.equal(first.find((r) => r.source === 'openrouter').ok, false, 'unreadable source is reported, not guessed');
  now.set('2026-11-24T03:30:00Z');
  const second = await refreshAll(store, { fetchImpl: fixtureFetch(), now });
  for (const r of second.filter((x) => x.ok)) {
    assert.equal(r.changed, false);
    assert.equal(r.snapshot_id, first.find((x) => x.source === r.source).snapshot_id);
  }
  const snap = await store.latestSnapshot('anthropic');
  assert.equal(snap.fetched_at, '2026-11-24T03:00:00.000Z');
  assert.equal(snap.last_seen_at, '2026-11-24T03:30:00.000Z');
  assert.equal(snap.source_url, 'https://platform.claude.com/docs/en/about-claude/pricing.md');
});

test('delta guard quarantines a price that jumps more than 50%', () => {
  const prev = { fetched_at: 'x', models: [{ model: 'm', bands: [{ min_prompt_tokens: null, max_prompt_tokens: null, valid_from: null, input: 3, output: 15 }] }] };
  const fresh = { models: [{ model: 'm', bands: [{ min_prompt_tokens: null, max_prompt_tokens: null, valid_from: null, input: 30, output: 15 }] }] };
  applyDeltaGuard(fresh, prev);
  assert.match(fresh.models[0].bands[0].quarantined, /input moved 3 -> 30/);
});
