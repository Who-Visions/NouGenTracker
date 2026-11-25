import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { createD1 } from '../dev/d1shim.mjs';
import { makeApp } from '../src/worker.js';
import { sha256b64u } from '../src/auth.js';
import { fixtureFetch } from './helpers.mjs';

const BASE = 'https://trckr.nougenai.com';
const KEY = 'trk_test_key_for_budgets';
const OPUS = { model: 'claude-opus-5-5', provider: 'anthropic', at: '2026-11-24T06:00:00Z' };

async function setup() {
  const alerts = [];
  const env = { DB: createD1(), PUBLIC_BASE_URL: BASE, TRCKR_SIGNING_SECRET: 's', TRCKR_ALERT_URL: 'https://alerts.example/budget', TRCKR_ALERT_TOKEN: 'tok' };
  const alertFetch = async (url, init) => {
    alerts.push({ url, auth: init.headers.authorization, body: JSON.parse(init.body) });
    return new Response('{}', { status: 200 });
  };
  const app = makeApp(env, { fetchImpl: fixtureFetch(), alertFetch });
  await app.scheduled();
  await app.store.createIngestKey('nougen', 'test-lane', await sha256b64u(KEY));
  const call = (path, body) =>
    app.fetch(new Request(`${BASE}${path}`, body === undefined ? { headers: { authorization: `Bearer ${KEY}` } } : { method: 'POST', headers: { authorization: `Bearer ${KEY}`, 'content-type': 'application/json' }, body: JSON.stringify(body) }), { waitUntil() {} });
  return { app, call, alerts };
}

test('a session is warned at 80% by its own check, stopped at the cap, and only the stop reaches the coordinator', async () => {
  const { app, call, alerts } = await setup();
  const set = await (await call('/v1/budget', { session_id: 'thread-1', cap_usd: 1, label: 'Trckr budgets', stop_rule: 'write a handoff shard' })).json();
  assert.equal(set.state, 'ok');
  // 112.5k uncached input at the fixture board's $4/M is $0.45 a turn.
  const turn = (n) => ({ ...OPUS, request_id: `r${n}`, session_id: 'thread-1', input_tokens: 112500 });
  let r = await (await call('/v1/turns', [turn(1)])).json();
  assert.equal(r.budget['thread-1'].state, 'ok');
  r = await (await call('/v1/turns', [turn(2)])).json();
  assert.equal(r.budget['thread-1'].state, 'warn');
  assert.match(r.budget['thread-1'].text, /Wrap up: finish the current step, write a handoff shard/);
  await app.scheduled('*/5 * * * *');
  assert.equal(alerts.length, 0, 'no alert at the warning: the session itself is told');

  r = await (await call('/v1/turns', [turn(3), turn(3)])).json(); // a repeat request id counts once
  const b = r.budget['thread-1'];
  assert.deepEqual([b.state, b.stop, b.pct], ['over', true, 135]);
  assert.match(b.text, /OVER BUDGET.*Stop now.*write a handoff shard/);

  await app.scheduled('*/5 * * * *');
  assert.equal(alerts.length, 1);
  assert.equal(alerts[0].auth, 'Bearer tok');
  assert.deepEqual(alerts[0].body.alerts.map((a) => [a.session_id, a.cap_usd, a.spent_usd]), [['thread-1', 1, 1.35]]);
  await app.scheduled('*/5 * * * *');
  assert.equal(alerts.length, 1, 'an alert goes out once');

  const check = await (await call('/v1/budget?session_id=thread-1')).json();
  assert.equal(check.stop, true);
  const closed = await (await call('/v1/budget', { session_id: 'thread-1', close: true })).json();
  assert.equal(closed.state, 'closed');
});

test('cumulative usage from a cloud session is priced at list price on the base tier, and a rollup never double counts turns', async () => {
  const { call } = await setup();
  await call('/v1/budget', { session_id: 'cloud-1', cap_usd: 20 });
  // 300k cache reads at $0.20/M plus 4k output at $20/M is $0.14, summed over many requests, so no long-context tier.
  const usage = { ...OPUS, cache_read_tokens: 300000, output_tokens: 4000 };
  let b = await (await call('/v1/budget', { session_id: 'cloud-1', usage })).json();
  assert.equal(b.spent_usd, 0.14);
  b = await (await call('/v1/budget', { session_id: 'cloud-1', usage: { ...usage, output_tokens: 8000 } })).json();
  assert.equal(b.spent_usd, 0.22, 'the rollup is replaced, not added');
  await call('/v1/turns', [{ ...OPUS, request_id: 'x', session_id: 'cloud-1', input_tokens: 10000 }]);
  b = await (await call('/v1/budget?session_id=cloud-1')).json();
  assert.equal(b.spent_usd, 0.22, 'larger of per-turn sum and rollup');
  const none = await (await call('/v1/budget?session_id=nobody')).json();
  assert.equal(none.state, 'none');
  const noKey = await (await import('../src/worker.js')).makeApp({ DB: createD1(), TRCKR_SIGNING_SECRET: 's' }).fetch(new Request(`${BASE}/v1/budget?session_id=a`));
  assert.equal(noKey.status, 401);
});

test('local watcher enforces with no cloud: ledger, signal files, and the gate tells the AI then blocks', async () => {
  const { call } = await setup();
  const board = await (await call('/v1/prices')).json();
  const home = mkdtempSync(join(tmpdir(), 'trckr-home-'));
  const projects = join(home, 'projects', '-p');
  mkdirSync(projects, { recursive: true });
  writeFileSync(join(home, 'prices.json'), JSON.stringify({ ...board, _read: Date.now() / 1000 }));
  const rows = (n) => Array.from({ length: n }, (_, i) => JSON.stringify({ type: 'assistant', sessionId: 'local-1', requestId: `q${i}`, timestamp: '2026-11-24T06:00:00Z', message: { model: 'claude-opus-5-5', usage: { input_tokens: 112500, output_tokens: 0 } } })).join('\n');
  const env = { ...process.env, TRCKR_HOME: home, CLAUDE_PROJECTS: join(home, 'projects'), TRCKR_URL: 'http://127.0.0.1:9', TRCKR_INGEST_KEY: '' };
  const watch = (...a) => execFileSync('python3', [new URL('../hooks/trckr_watch.py', import.meta.url).pathname, ...a], { env, encoding: 'utf8' });
  const gate = (event) => execFileSync('python3', [new URL('../hooks/trckr_budget_gate.py', import.meta.url).pathname], { env, input: JSON.stringify({ session_id: 'local-1', hook_event_name: event }), encoding: 'utf8' });
  watch('set', 'local-1', '1', '--label', 'Local lane');

  writeFileSync(join(projects, 's.jsonl'), rows(2));
  assert.match(watch('status'), /WARN\s+Local lane: \$0\.90 of \$1\.00 \(90%\)/);
  assert.ok(existsSync(join(home, 'signals', 'local-1.warn')));
  assert.match(JSON.parse(gate('UserPromptSubmit')).hookSpecificOutput.additionalContext, /Budget warning.*Wrap up/);
  assert.equal(gate('PreToolUse').trim(), '', 'a warning never blocks a tool call');

  writeFileSync(join(projects, 's.jsonl'), rows(3));
  assert.match(watch('status'), /OVER\s+Local lane: \$1\.35/);
  assert.ok(!existsSync(join(home, 'signals', 'local-1.warn')));
  const deny = JSON.parse(gate('PreToolUse')).hookSpecificOutput;
  assert.equal(deny.permissionDecision, 'deny');
  assert.match(deny.permissionDecisionReason, /OVER BUDGET/);
  assert.equal(JSON.parse(gate('UserPromptSubmit')).decision, 'block');
  assert.equal(JSON.parse(readFileSync(join(home, 'ledger.json'), 'utf8')).sessions['local-1'].state, 'over');

  watch('close', 'local-1');
  assert.equal(gate('PreToolUse').trim(), '');
});

test('usage totals count a session rollup only when the session has no per-turn rows', async () => {
  const { app, call } = await setup();
  await call('/v1/budget', { session_id: 'mix', cap_usd: 50, usage: { ...OPUS, input_tokens: 1_000_000 } });
  const total = async () => (await app.store.usage('nougen', { group_by: 'session', session_id: 'mix' }))[0].cost_usd;
  assert.equal(await total(), 4, 'rollup alone counts');
  await call('/v1/turns', [{ ...OPUS, request_id: 't1', session_id: 'mix', input_tokens: 250000 }]);
  assert.equal(await total(), 1, 'per-turn rows replace the rollup in totals');
});
