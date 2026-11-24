import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import { createD1 } from '../dev/d1shim.mjs';
import { makeApp } from '../src/worker.js';
import { TOOLS } from '../src/mcp.js';
import { fixtureFetch } from './helpers.mjs';

const BASE = 'https://trckr.nougenai.com';
const OWNER = 'owner@example.com';

async function setup(identity = { email: OWNER, email_verified: true }) {
  const env = {
    DB: createD1(),
    PUBLIC_BASE_URL: BASE,
    TRCKR_SIGNING_SECRET: 'test-secret',
    TRCKR_ALLOWED_EMAILS: `${OWNER},second@example.com`,
    GOOGLE_CLIENT_ID: 'gid',
    GOOGLE_REDIRECT_URI: 'https://eye.nougenai.com/oauth/google/callback',
  };
  const app = makeApp(env, { fetchImpl: fixtureFetch(), googleExchange: async () => identity });
  await app.scheduled();
  const call = (path, init = {}) => app.fetch(new Request(`${BASE}${path}`, init), { waitUntil() {} });
  return { app, call };
}

async function signIn(call) {
  const reg = await (await call('/register', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ client_name: 'Claude', redirect_uris: ['https://claude.ai/api/mcp/auth_callback'] }) })).json();
  const verifier = 'v'.repeat(64);
  const challenge = createHash('sha256').update(verifier).digest('base64url');
  const q = new URLSearchParams({ client_id: reg.client_id, redirect_uri: 'https://claude.ai/api/mcp/auth_callback', response_type: 'code', code_challenge: challenge, code_challenge_method: 'S256', state: 'xyz' });
  const login = await (await call(`/authorize?${q}`)).text();
  assert.match(login, /Continue with Google/);
  const google = new URL(login.match(/href="(https:\/\/accounts\.google\.com[^"]+)"/)[1].replace(/&#38;/g, '&'));
  assert.equal(google.searchParams.get('redirect_uri'), 'https://eye.nougenai.com/oauth/google/callback');
  const state = google.searchParams.get('state');
  assert.ok(state.startsWith('trckr~'), 'state carries the Eye relay tag');
  const cb = await call(`/oauth/google/callback?code=g&state=${encodeURIComponent(state)}`);
  return { cb, reg, verifier };
}

test('OAuth metadata names the bare URL and /mcp', async () => {
  const { call } = await setup();
  const root = await (await call('/.well-known/oauth-protected-resource')).json();
  assert.equal(root.resource, BASE);
  const mcp = await (await call('/.well-known/oauth-protected-resource/mcp')).json();
  assert.equal(mcp.resource, `${BASE}/mcp`);
  const as = await (await call('/.well-known/oauth-authorization-server')).json();
  assert.deepEqual(as.code_challenge_methods_supported, ['S256']);
  const unauth = await call('/mcp', { method: 'POST', body: '{}' });
  assert.equal(unauth.status, 401);
  assert.match(unauth.headers.get('www-authenticate'), /oauth-protected-resource\/mcp/);
});

test('full sign-in: Google via Eye relay, consent, PKCE token, then MCP tools', async () => {
  const { call } = await setup();
  const { cb, verifier } = await signIn(call);
  const consent = await cb.text();
  assert.equal(cb.status, 200);
  assert.match(consent, /Claude would like access to your NouGen\/\/Trckr/);
  const token = consent.match(/name="token" value="([^"]+)"/)[1];
  const allowed = await (await call('/oauth/consent', { method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ token, decision: 'allow' }) })).text();
  const back = new URL(allowed.match(/url=([^"]+)"/)[1].replace(/&#38;/g, '&'));
  assert.equal(back.searchParams.get('state'), 'xyz');
  const tok = await (await call('/token', { method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ grant_type: 'authorization_code', code: back.searchParams.get('code'), code_verifier: verifier, redirect_uri: 'https://claude.ai/api/mcp/auth_callback' }) })).json();
  assert.ok(tok.access_token && tok.refresh_token);
  const bad = await call('/token', { method: 'POST', body: new URLSearchParams({ grant_type: 'authorization_code', code: back.searchParams.get('code'), code_verifier: 'wrong', redirect_uri: 'https://claude.ai/api/mcp/auth_callback' }) });
  assert.equal(bad.status, 400);

  const rpc = async (method, params, id = 1) => (await call('/mcp', { method: 'POST', headers: { authorization: `Bearer ${tok.access_token}`, 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id, method, params }) })).json();
  const init = await rpc('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 't', version: '1' } });
  assert.equal(init.result.serverInfo.title, 'NouGen//Trckr');
  const list = await rpc('tools/list');
  assert.deepEqual(list.result.tools.map((t) => t.name), TOOLS.map((t) => t.name));
  for (const t of list.result.tools) assert.ok(t.annotations && t.inputSchema.type === 'object', t.name);

  const cost = await rpc('tools/call', { name: 'trckr_turn_cost', arguments: { model: 'claude-opus-5-5', input_tokens: 1000, cache_read_tokens: 50000, cache_write_5m_tokens: 2000, output_tokens: 800 } });
  assert.equal(cost.result.structuredContent.cost_usd, 0.04);
  assert.match(cost.result.content[0].text, /Claude Opus 5\.5: \$0\.04/);
  assert.match(cost.result.content[0].text, /platform\.claude\.com/);
  assert.ok(!cost.result.content[0].text.includes(String.fromCharCode(0x2014)), 'no em dashes');

  const rec = await rpc('tools/call', { name: 'trckr_record_turn', arguments: { model: 'gpt-5.5', provider: 'openai', lane: 'phoebus-codex', request_id: 'r1', session_id: 'sess', input_tokens: 2000, output_tokens: 300 } });
  assert.equal(rec.result.structuredContent.status, 'priced');
  const sess = await rpc('tools/call', { name: 'trckr_session', arguments: { session_id: 'sess' } });
  assert.equal(sess.result.structuredContent.turns.length, 1);
  const usage = await rpc('tools/call', { name: 'trckr_usage', arguments: { group_by: 'lane' } });
  assert.equal(usage.result.structuredContent.rows[0].key, 'phoebus-codex');
  const pools = await rpc('tools/call', { name: 'trckr_pools', arguments: {} });
  assert.equal(pools.result.structuredContent.pools.filter((p) => p.read).length, 3);
  const prices = await rpc('tools/call', { name: 'trckr_prices', arguments: { search: 'haiku' } });
  assert.match(prices.result.content[0].text, /prompt <= 100,000/);
  const unknown = await rpc('tools/call', { name: 'trckr_price', arguments: { model: 'no-such-model' } });
  assert.equal(unknown.result.structuredContent.found, false);

  // hook key: shown once, stored hashed, then /v1/turns works with it
  const key = (await rpc('tools/call', { name: 'trckr_ingest_key', arguments: { label: 'blade-ollama' } })).result.structuredContent.key;
  assert.match(key, /^trk_/);
  const post = (k, body) => call('/v1/turns', { method: 'POST', headers: { authorization: `Bearer ${k}`, 'content-type': 'application/json' }, body: JSON.stringify(body) });
  assert.equal((await post('trk_nope', { model: 'x', request_id: '1' })).status, 401);
  const ing = await (await post(key, { turns: [{ request_id: 'o1', provider: 'ollama', model: 'qwen3:32b', input_tokens: 4000, output_tokens: 900 }, { request_id: 'c1', model: 'claude-sonnet-5-5', input_tokens: 1000, output_tokens: 1000 }] })).json();
  assert.equal(ing.recorded, 2);
  assert.equal(ing.turns[0].status, 'local');
  assert.equal(ing.turns[1].cost_usd, 0.012);
  const lanes = await rpc('tools/call', { name: 'trckr_usage', arguments: { group_by: 'lane' } });
  assert.ok(lanes.result.structuredContent.rows.some((r) => r.key === 'blade-ollama' && r.turns === 2));
});

test('an account off the allowlist is refused', async () => {
  const { call } = await setup({ email: 'stranger@example.com', email_verified: true });
  const { cb } = await signIn(call);
  assert.equal(cb.status, 403);
  assert.match(await cb.text(), /can't use this NouGen\/\/Trckr/);
});

test('board: public price board, signed-in turn feed, connect page', async () => {
  const { call } = await setup();
  const pub = await (await call('/')).text();
  assert.match(pub, /Every turn, <em>priced from the source/);
  assert.match(pub, /Claude Opus 5\.5/);
  assert.match(pub, /platform\.claude\.com/);
  assert.match(pub, /OpenRouter<\/h3>\s*<p>Not read yet/);
  assert.ok(!pub.includes(String.fromCharCode(0x2014)), 'no em dashes');
  const login = await call('/login?next=/');
  const state = new URL(login.headers.get('location')).searchParams.get('state');
  const cb = await call(`/oauth/google/callback?code=g&state=${encodeURIComponent(state)}`);
  assert.equal(cb.status, 302);
  const cookie = cb.headers.get('set-cookie').split(';')[0];
  const mine = await (await call('/', { headers: { cookie } })).text();
  assert.match(mine, /Spend today/);
  assert.match(mine, /No turns yet/);
  const connect = await (await call('/connect')).text();
  assert.match(connect, /https:\/\/trckr\.nougenai\.com\/mcp/);
  assert.match(connect, /codex mcp add nougen-trckr/);
});
