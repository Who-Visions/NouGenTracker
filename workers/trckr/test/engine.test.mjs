import assert from 'node:assert/strict';
import { test } from 'node:test';
import { priceTurn } from '../src/engine.js';
import { refreshAll } from '../src/pricing/pool.js';
import { clock, fixtureFetch, freshStore } from './helpers.mjs';

async function loaded() {
  const store = freshStore();
  await refreshAll(store, { fetchImpl: fixtureFetch(), now: clock('2026-11-24T03:00:00Z') });
  return store;
}

test('a Claude turn is priced per component from the page, with provenance', async () => {
  const store = await loaded();
  const p = await priceTurn(store, {
    provider: 'anthropic', model: 'claude-opus-5-5[1m]', at: '2026-11-24T04:00:00Z',
    input_tokens: 1000, cache_read_tokens: 50_000, cache_write_5m_tokens: 2000, output_tokens: 800,
  });
  assert.equal(p.status, 'priced');
  // 1000*$4 + 50k*$0.20 + 2000*$5 + 800*$20, per 1M tokens
  assert.equal(p.cost_usd, 0.04);
  assert.equal(p.cost_nano, '40000000');
  assert.equal(p.components.cache_read.usd, 0.01);
  assert.equal(p.cache_savings_usd, 0.19); // 50k at $4 instead of $0.20
  assert.equal(p.price.ref.source_url, 'https://platform.claude.com/docs/en/about-claude/pricing.md');
  assert.equal(p.price.ref.fetched_at, '2026-11-24T03:00:00.000Z');
  assert.ok(p.price.ref.snapshot_id.length === 24);
  assert.ok(Math.abs(p.cache_hit_rate - 50_000 / 53_000) < 1e-12);
});

test('prompt-size tier: the same model bills a long prompt at the long rate', async () => {
  const store = await loaded();
  const short = await priceTurn(store, { provider: 'anthropic', model: 'claude-haiku-5-5', at: '2026-11-24T04:00:00Z', input_tokens: 100_000, output_tokens: 1000 });
  const long = await priceTurn(store, { provider: 'anthropic', model: 'claude-haiku-5-5', at: '2026-11-24T04:00:00Z', input_tokens: 100_001, output_tokens: 1000 });
  assert.equal(short.price.band.input, 0.1);
  assert.equal(long.price.band.input, 0.5);
  // cached tokens count toward the prompt size a tier is judged on
  const cached = await priceTurn(store, { provider: 'anthropic', model: 'claude-haiku-5-5', at: '2026-11-24T04:00:00Z', input_tokens: 10, cache_read_tokens: 120_000, output_tokens: 1 });
  assert.equal(cached.price.band.input, 0.5);
});

test('dated schedule: a turn on Dec 1 uses the new published rate', async () => {
  const store = await loaded();
  const before = await priceTurn(store, { provider: 'google', model: 'gemini-4-carbon', at: '2026-11-30T23:59:00Z', input_tokens: 1_000_000, output_tokens: 0 });
  const after = await priceTurn(store, { provider: 'google', model: 'gemini-4-carbon', at: '2026-12-01T00:00:00Z', input_tokens: 1_000_000, output_tokens: 0 });
  assert.equal(before.cost_usd, 2);
  assert.equal(after.cost_usd, 4);
});

test('local Ollama turns keep their tokens at $0, labeled self-hosted', async () => {
  const store = await loaded();
  const p = await priceTurn(store, { provider: 'ollama', model: 'qwen3:32b', input_tokens: 4000, output_tokens: 900 });
  assert.equal(p.status, 'local');
  assert.equal(p.cost_usd, 0);
  assert.equal(p.total_tokens, 4900);
  assert.match(p.price.basis, /self-hosted/);
});

test('a model no page lists is unpriced, never guessed', async () => {
  const store = await loaded();
  const p = await priceTurn(store, { model: 'made-up-model-9', input_tokens: 10, output_tokens: 10 });
  assert.equal(p.status, 'unpriced');
  assert.equal(p.cost_usd, null);
  assert.match(p.reason, /not on/);
  const none = await priceTurn(freshStore(), { model: 'claude-opus-5-5', input_tokens: 1, output_tokens: 1 });
  assert.equal(none.status, 'unpriced');
  assert.match(none.reason, /no provider page has been read/);
});

test('a missing component rate makes the turn partial and says which', async () => {
  const store = await loaded();
  // Gemini pages publish no cache-write price; the write tokens are named, not invented
  const p = await priceTurn(store, { provider: 'gemini', model: 'gemini-4-carbon', at: '2026-11-24T04:00:00Z', input_tokens: 100, cache_write_5m_tokens: 100, output_tokens: 100 });
  assert.equal(p.status, 'partial');
  assert.deepEqual(p.unpriced_components, ['cache_write_5m']);
});

test('turns upsert on lane + request id, and usage rolls them up', async () => {
  const store = await loaded();
  const owner = 'owner@example.com';
  const turn = { provider: 'anthropic', model: 'claude-sonnet-5-5', at: '2026-11-24T04:00:00Z', input_tokens: 1000, output_tokens: 1000 };
  await store.recordTurn(owner, { lane: 'test-lane', request_id: 'req_1', session_id: 's1' }, await priceTurn(store, turn));
  await store.recordTurn(owner, { lane: 'test-lane', request_id: 'req_1', session_id: 's1' }, await priceTurn(store, turn));
  await store.recordTurn(owner, { lane: 'test-lane', request_id: 'req_2', session_id: 's1' }, await priceTurn(store, { ...turn, at: '2026-11-24T05:00:00Z', provider: 'ollama', model: 'llama3' }));
  const rows = await store.listTurns(owner, { session_id: 's1' });
  assert.equal(rows.length, 2);
  const [usage] = await store.usage(owner, { group_by: 'session' });
  assert.equal(usage.turns, 2);
  assert.equal(usage.local, 1);
  assert.equal(usage.cost_usd, 0.012); // 1000 * $2 + 1000 * $10 per 1M
  const days = await store.usage(owner, { group_by: 'day' });
  // 04:00 UTC is 11 PM Eastern on Nov 23; 05:00 UTC is midnight Eastern, Nov 24
  assert.deepEqual(days.map((d) => [d.key, d.turns]), [['2026-11-24', 1], ['2026-11-23', 1]]);
  assert.deepEqual(await store.listTurns('someone-else@example.com'), []);
});
