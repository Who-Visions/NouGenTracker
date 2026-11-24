// Local run of the Worker on Node: node dev/server.mjs [port]
// D1 is node:sqlite (same SQL). Provider pages are fetched live; any page this machine cannot
// reach falls back to the saved copy in test/fixtures, and the board says which is which.
import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { createD1 } from './d1shim.mjs';
import { makeApp } from '../src/worker.js';
import { SOURCES } from '../src/pricing/pool.js';

const port = Number(process.argv[2] || process.env.PORT || 8787);
const base = `http://localhost:${port}`;
const fixtures = {
  'https://developers.openai.com/api/docs/pricing?latest-pricing=standard': 'openai_standard_catalog.md',
  'https://ai.google.dev/gemini-api/docs/pricing.md.txt': 'gemini_pricing.md',
};
const served = {};
const fetchImpl = async (url, init) => {
  try {
    const r = await fetch(url, init);
    if (r.ok) {
      served[url] = 'live';
      return r;
    }
    throw new Error(String(r.status));
  } catch (e) {
    if (fixtures[url]) {
      served[url] = 'saved copy';
      return new Response(readFileSync(new URL(`../test/fixtures/${fixtures[url]}`, import.meta.url), 'utf8'), { status: 200 });
    }
    throw e;
  }
};

const env = {
  DB: createD1(process.env.TRCKR_DB || ':memory:'),
  PUBLIC_BASE_URL: base,
  TRCKR_SIGNING_SECRET: process.env.TRCKR_SIGNING_SECRET || 'local-dev-only-secret',
  TRCKR_ALLOWED_EMAILS: process.env.TRCKR_ALLOWED_EMAILS || 'dev@localhost.test',
  GOOGLE_CLIENT_ID: process.env.GOOGLE_CLIENT_ID || 'local-dev-client',
};
// Local only: Google sign-in is stubbed to the first allowed account.
const googleExchange = async () => ({ email: env.TRCKR_ALLOWED_EMAILS.split(',')[0], email_verified: true });
const app = makeApp(env, { fetchImpl, googleExchange, sampleNote: process.env.TRCKR_SAMPLE_NOTE });
const results = await app.scheduled();
for (const r of results) {
  const src = SOURCES.find((s) => s.id === r.source);
  const how = r.ok ? Object.entries(served).find(([u]) => src.urls.some(([x]) => x === u))?.[1] : 'not reachable';
  console.log(`${r.source}: ${r.ok ? `${r.models} models (${how})` : 'not read'}`);
}

// Sample turns for a local look at the feed, never on a deployment.
if (process.env.TRCKR_SEED_SAMPLE) {
  const { priceTurn } = await import('../src/engine.js');
  const owner = env.TRCKR_TENANT || 'nougen';
  const now = Date.now();
  const sample = [
    ['phoebus-claude-code', 'anthropic', 'claude-opus-5-5', 9, 1800, 61200, 2400, 0],
    ['phoebus-claude-code', 'anthropic', 'claude-opus-5-5', 6, 920, 63900, 1100, 0],
    ['whoart-sonnet', 'anthropic', 'claude-sonnet-5-5', 14, 3100, 41800, 0, 5200],
    ['phoebus-codex', 'openai', 'gpt-5-5', 22, 3800, 25600, 0, 0],
    ['blade-gemini', 'google', 'gemini-2-5-pro', 31, 240000, 0, 0, 0],
    ['blade-ollama', 'ollama', 'qwen3:32b', 45, 5200, 0, 0, 0],
    ['phoebus-claude-code', 'anthropic', 'claude-haiku-5-5', 58, 640, 8800, 0, 0],
  ];
  for (const [i, [lane, provider, model, minsAgo, out, read, w5, w1]] of sample.entries()) {
    const t = { provider, model, at: new Date(now - minsAgo * 60000).toISOString(), input_tokens: provider === 'google' ? 210000 : 1200 + i * 310, output_tokens: out, cache_read_tokens: read, cache_write_5m_tokens: w5, cache_write_1h_tokens: w1 };
    await app.store.recordTurn(owner, { lane, request_id: `sample_${i}`, session_id: `sample-${lane}`, client: 'sample' }, await priceTurn(app.store, t));
  }
}

createServer(async (req, res) => {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const request = new Request(`${base}${req.url}`, { method: req.method, headers: req.headers, body: ['GET', 'HEAD'].includes(req.method) ? undefined : Buffer.concat(chunks) });
  const response = await app.fetch(request, { waitUntil: () => {} });
  res.writeHead(response.status, Object.fromEntries(response.headers));
  res.end(Buffer.from(await response.arrayBuffer()));
}).listen(port, () => console.log(`NouGen//Trckr on ${base}`));
