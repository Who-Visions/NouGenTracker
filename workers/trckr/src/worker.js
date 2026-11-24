// NouGen//Trckr Worker: MCP at /mcp, OAuth + Google sign-in, the live board, the per-turn
// ingest endpoint for hooks, and a cron that re-reads every provider's pricing page.
import { createAuth, sha256b64u } from './auth.js';
import { priceTurn } from './engine.js';
import { createMcp } from './mcp.js';
import { createBudgets, budgetText } from './budget.js';
import { boardView, connectView } from './pages.js';
import { SOURCES, refreshAll } from './pricing/pool.js';
import { createStore, easternDay } from './store.js';

const CORS = {
  'access-control-allow-origin': '*',
  'access-control-allow-methods': 'GET, POST, DELETE, OPTIONS',
  'access-control-allow-headers': 'authorization, content-type, mcp-protocol-version, mcp-session-id, last-event-id',
  'access-control-expose-headers': 'www-authenticate, mcp-session-id',
  'access-control-max-age': '86400',
};
const json = (status, body, headers = {}) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store', ...CORS, ...headers } });
const PAGE_HEADERS = {
  'content-type': 'text/html; charset=utf-8',
  'cache-control': 'no-store',
  'content-security-policy': "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; img-src data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
  'referrer-policy': 'no-referrer',
  'x-content-type-options': 'nosniff',
};

// Filters the price board as you type. No data leaves the page.
const BOARD_JS = `(()=>{const q=document.getElementById('q');if(!q)return;q.addEventListener('input',()=>{const v=q.value.trim().toLowerCase();for(const d of document.querySelectorAll('details.prov')){let any=false;for(const r of d.querySelectorAll('tbody tr')){const hit=!v||r.dataset.q.includes(v);r.hidden=!hit;any||=hit;}d.hidden=!any;if(v&&any)d.open=true;}});})();`;

const LOCAL_TURN_LIMIT = 500;

export function makeApp(env, deps = {}) {
  const store = deps.store || createStore(env.DB);
  const tenant = env.TRCKR_TENANT || 'nougen';
  const baseFor = (req) => (env.PUBLIC_BASE_URL || new URL(req.url).origin).replace(/\/$/, '');
  // TRCKR_SIGNING_SECRET wins when set; otherwise the Worker mints its own once and keeps it in D1.
  let secretP;
  const signingSecret = () => (secretP ||= env.TRCKR_SIGNING_SECRET ? Promise.resolve(env.TRCKR_SIGNING_SECRET) : store.signingSecret());
  const authFor = async (base) =>
    createAuth({
      base,
      googleId: env.GOOGLE_CLIENT_ID,
      googleSecret: env.GOOGLE_CLIENT_SECRET,
      allowedEmails: env.TRCKR_ALLOWED_EMAILS,
      signingSecret: await signingSecret(),
      googleRedirect: env.GOOGLE_REDIRECT_URI,
      googleExchange: deps.googleExchange,
      now: deps.now,
    });
  const budgets = createBudgets({ store });
  const mcp = createMcp({ store, tenant, fetchImpl: deps.fetchImpl, canRefresh: env.TRCKR_REFRESH !== 'off' });

  async function pools() {
    return Promise.all(SOURCES.map(async (s) => ({ id: s.id, label: s.label, page: s.page, snapshot: await store.latestSnapshot(s.id), last: await store.lastFetch(s.id) })));
  }

  async function keyOwner(req) {
    const [scheme, key] = (req.headers.get('authorization') || '').split(' ');
    return scheme === 'Bearer' && key?.startsWith('trk_') ? store.ingestOwner(await sha256b64u(key)) : null;
  }
  const badKey = () => json(401, { error: 'invalid_key', hint: 'Mint one with the trckr_ingest_key tool and send it as Authorization: Bearer <key>.' });

  // Hooks and watchers: GET ?session_id= checks; POST {session_id, cap_usd?, warn_pct?, stop_rule?, label?, usage?, close?} sets or reports.
  async function budgetApi(req, url) {
    const who = await keyOwner(req);
    if (!who) return badKey();
    let a = { session_id: url.searchParams.get('session_id') };
    if (req.method === 'POST') {
      try {
        a = await req.json();
      } catch {
        return json(400, { error: 'invalid_json' });
      }
    }
    if (!a?.session_id) return json(400, { error: 'session_id is required' });
    const session_id = String(a.session_id).slice(0, 200);
    try {
      if (a.close) return json(200, (await budgets.close(who.owner, session_id)) || { session_id, state: 'none', stop: false, warn: false });
      if (a.cap_usd !== undefined) await budgets.set(who.owner, { ...a, session_id });
      if (a.usage) await budgets.rollup(who.owner, session_id, a.usage, who.label);
    } catch (err) {
      return json(400, { error: String(err?.message || err).slice(0, 200) });
    }
    const b = await budgets.evaluate(who.owner, session_id);
    return json(200, b ? { ...b, text: budgetText(b) } : { session_id, state: 'none', stop: false, warn: false });
  }

  async function ingest(req) {
    const who = await keyOwner(req);
    if (!who) return badKey();
    let body;
    try {
      body = await req.json();
    } catch {
      return json(400, { error: 'invalid_json' });
    }
    const list = Array.isArray(body) ? body : Array.isArray(body?.turns) ? body.turns : [body];
    if (list.length > LOCAL_TURN_LIMIT) return json(413, { error: `at most ${LOCAL_TURN_LIMIT} turns per request` });
    const out = [];
    for (const t of list) {
      if (!t || !t.model || !t.request_id) {
        out.push({ ok: false, error: 'model and request_id are required' });
        continue;
      }
      const p = await priceTurn(store, t);
      const lane = String(t.lane || who.label).slice(0, 80);
      await store.recordTurn(who.owner, { lane, request_id: String(t.request_id).slice(0, 200), session_id: t.session_id ? String(t.session_id).slice(0, 200) : null, client: t.client ? String(t.client).slice(0, 60) : null }, p);
      out.push({ ok: true, request_id: t.request_id, status: p.status, cost_usd: p.cost_usd, source_url: p.price?.ref?.source_url || null, fetched_at: p.price?.ref?.fetched_at || null });
    }
    const sessions = [...new Set(list.map((t) => t?.session_id).filter(Boolean).map((x) => String(x).slice(0, 200)))];
    const budget = {};
    for (const sid of sessions) {
      const b = await budgets.evaluate(who.owner, sid);
      if (b) budget[sid] = { state: b.state, stop: b.stop, warn: b.warn, pct: b.pct, spent_usd: b.spent_usd, cap_usd: b.cap_usd, text: budgetText(b) };
    }
    return json(200, { recorded: out.filter((x) => x.ok).length, turns: out, ...(sessions.length ? { budget } : {}) });
  }

  async function handleMcp(req, auth) {
    const who = await auth.verify(req);
    if (!who) return json(401, { error: 'invalid_token' }, { 'www-authenticate': auth.challengeFor('/mcp') });
    if (req.method === 'GET') return json(405, { error: 'SSE stream not offered; POST JSON-RPC to /mcp' }, { allow: 'POST' });
    if (req.method === 'DELETE') return new Response(null, { status: 204, headers: CORS });
    let msg;
    try {
      msg = await req.json();
    } catch {
      return json(400, { jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error' } });
    }
    if (Array.isArray(msg)) {
      const out = (await Promise.all(msg.map((m) => mcp.rpc(m)))).filter(Boolean);
      return out.length ? json(200, out) : new Response(null, { status: 202, headers: CORS });
    }
    const out = await mcp.rpc(msg);
    return out ? json(200, out) : new Response(null, { status: 202, headers: CORS });
  }

  async function fetch(req, ctx) {
    const url = new URL(req.url);
    const base = baseFor(req);
    if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: CORS });
    if (url.pathname === '/healthz') return json(200, { ok: true, service: 'nougen-trckr' });
    if (url.pathname === '/assets/trckr.js') return new Response(BOARD_JS, { headers: { 'content-type': 'text/javascript; charset=utf-8', 'cache-control': 'public, max-age=300' } });
    if (url.pathname === '/robots.txt') return new Response('User-agent: *\nAllow: /\nDisallow: /mcp\n', { headers: { 'content-type': 'text/plain' } });

    const auth = await authFor(base);
    if (url.pathname === '/mcp' || url.pathname === '/mcp/') return handleMcp(req, auth);
    const handled = await auth.handle(req);
    if (handled) return handled;

    if (url.pathname === '/v1/turns' && req.method === 'POST') return ingest(req);
    if (url.pathname === '/v1/budget' && (req.method === 'GET' || req.method === 'POST')) return budgetApi(req, url);
    if (url.pathname === '/v1/prices' && req.method === 'GET') {
      const ps = await pools();
      return json(200, {
        providers: ps.map((p) => ({ provider: p.id, label: p.label, page_url: p.page, read: !!p.snapshot, source_url: p.snapshot?.source_url || null, fetched_at: p.snapshot?.fetched_at || null, last_seen_at: p.snapshot?.last_seen_at || null, snapshot_id: p.snapshot?.snapshot_id || null, models: p.snapshot?.models || [] })),
      });
    }

    if (url.pathname === '/' || url.pathname === '/connect') {
      const user = await auth.webSession(req);
      if (url.pathname === '/connect') return new Response(connectView({ base, user }), { headers: PAGE_HEADERS });
      const ps = await pools();
      if (!ps.some((p) => p.snapshot) && ctx?.waitUntil) ctx.waitUntil(refreshAll(store, { fetchImpl: deps.fetchImpl }));
      let usage = null;
      let turns = [];
      let days = [];
      if (user) {
        const dayStart = new Date(Date.now() - 36 * 3600000).toISOString();
        const today = easternDay(new Date().toISOString());
        const byDay = await store.usage(tenant, { group_by: 'day', since: new Date(Date.now() - 14 * 86400000).toISOString() });
        usage = { today: byDay.find((d) => d.key === today) || null };
        days = byDay;
        turns = await store.listTurns(tenant, { since: dayStart, limit: 40 });
      }
      return new Response(boardView({ base, user, pools: ps, usage, turns, days, sample: deps.sampleNote }), { headers: PAGE_HEADERS });
    }
    return json(404, { error: 'not_found' });
  }

  // Two crons: prices every 30 minutes, the model-free budget watcher every 5. No cron given (tests) runs both.
  async function scheduled(cron) {
    const watch = () => budgets.watch({ alertUrl: env.TRCKR_ALERT_URL, alertToken: env.TRCKR_ALERT_TOKEN, fetchImpl: deps.alertFetch || deps.fetchImpl || fetch });
    if (cron === '*/5 * * * *') return watch();
    const prices = await refreshAll(store, { fetchImpl: deps.fetchImpl });
    await watch();
    return prices;
  }

  return { fetch, store, scheduled };
}

export default {
  fetch: (req, env, ctx) => makeApp(env).fetch(req, ctx),
  scheduled: (event, env, ctx) => ctx.waitUntil(makeApp(env).scheduled(event?.cron)),
};
