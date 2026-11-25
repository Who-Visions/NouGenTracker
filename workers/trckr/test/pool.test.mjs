import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createD1 } from '../dev/d1shim.mjs';
import { createStore } from '../src/store.js';
import { refreshAll } from '../src/pricing/pool.js';
import { priceTurn } from '../src/engine.js';

// OpenRouter body with one model at the given input price (USD per 1M tokens).
const body = (input) => JSON.stringify({ data: [{ id: 'acme/m1', name: 'Acme M1', pricing: { prompt: String(input / 1e6), completion: '0.00001' } }] });

function rig() {
  const store = createStore(createD1());
  let next = body(3);
  let clock = Date.parse('2026-11-24T00:00:00Z');
  const opts = { only: 'openrouter', fetchImpl: async () => new Response(next, { status: 200 }), now: () => new Date((clock += 60000)) };
  const read = async (input) => {
    next = body(input);
    await refreshAll(store, opts);
  };
  const cost = async (at) => (await priceTurn(store, { model: 'acme/m1', provider: 'openrouter', input_tokens: 1_000_000, at })).cost_usd;
  return { store, read, cost };
}

test('a price the delta guard held is released when the next read of the same page confirms it', async () => {
  const { read, cost } = rig();
  await read(3);
  await read(6);
  assert.equal(await cost(), null, 'a 100% jump is held on first sight');
  await read(6);
  assert.equal(await cost(), 6, 'the confirming read releases it');
});

test('a provider that goes A, B, A resolves to A again, and history before B stays A', async () => {
  const { store, read, cost } = rig();
  await read(3);
  const a = (await store.latestSnapshot('openrouter')).last_seen_at;
  await read(4);
  await read(3);
  assert.equal(await cost(), 3);
  assert.equal(await cost(a), 3);
});
