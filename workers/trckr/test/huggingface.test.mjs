import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseHuggingFace } from '../src/pricing/huggingface.js';
import { refreshAll } from '../src/pricing/pool.js';
import { priceTurn } from '../src/engine.js';
import { freshStore } from './helpers.mjs';

// Shape of https://router.huggingface.co/v1/models (read Nov 25 2026), trimmed.
const BODY = JSON.stringify({
  object: 'list',
  data: [
    { id: 'deepseek-ai/DeepSeek-V4.1-Flash', providers: [
      { provider: 'fireworks-ai', status: 'live', pricing: { input: 0.15, output: 0.6 }, provider_name: 'Fireworks' },
      { provider: 'together', status: 'live', pricing: { input: 0.3, output: 1.2 }, provider_name: 'Together' },
      { provider: 'novita', status: 'offline', pricing: { input: 0.1, output: 0.4 } },
    ] },
    { id: 'tencent/Hy4-flash', providers: [{ provider: 'fireworks-ai', status: 'live', pricing: { input: 0.07, output: 0.281 } }] },
    { id: 'x/no-price', providers: [{ provider: 'acme', status: 'live' }] },
  ],
});

test('Hugging Face pool: one priced route per live provider, offline and unpriced routes left out', () => {
  const { models, rejected } = parseHuggingFace(BODY);
  assert.deepEqual(models.map((m) => m.model).sort(), ['deepseek-ai/deepseek-v4-1-flash:fireworks-ai', 'deepseek-ai/deepseek-v4-1-flash:together', 'tencent/hy4-flash:fireworks-ai']);
  assert.deepEqual(rejected.map((r) => r.model), ['x/no-price:acme']);
  assert.deepEqual(models.find((m) => m.model.startsWith('tencent')).aliases, ['tencent/hy4-flash'], 'a single live route also answers to the bare id');
  assert.deepEqual(models.find((m) => m.model.endsWith(':together')).aliases, [], 'several routes: the bare id stays unpriced');
});

test('a router turn is priced at its provider route; a bare id with several routes is not guessed', async () => {
  const store = freshStore();
  await refreshAll(store, { only: 'huggingface', fetchImpl: async () => new Response(BODY, { status: 200 }) });
  const turn = (model) => priceTurn(store, { model, provider: 'hf', input_tokens: 1_000_000, output_tokens: 1_000_000 });
  assert.equal((await turn('deepseek-ai/DeepSeek-V4.1-Flash:together')).cost_usd, 1.5);
  assert.equal((await turn('tencent/Hy4-flash')).cost_usd, 0.351);
  assert.equal((await turn('deepseek-ai/DeepSeek-V4.1-Flash')).status, 'unpriced');
});
