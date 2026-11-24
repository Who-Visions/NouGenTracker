// D1-backed store. The same code runs in the Worker (env.DB) and locally (dev/d1shim.mjs wraps
// node:sqlite in the D1 API), so the tests exercise the real queries.
import { nanoToUsd, sha256Hex } from './pricing/core.js';

const GROUPS = {
  model: 'model',
  provider: "COALESCE(provider, 'unknown')",
  lane: 'lane',
  session: "COALESCE(session_id, '-')",
  client: "COALESCE(client, '-')",
  hour: 'substr(at, 1, 13)',
};

function snapRow(r) {
  if (!r) return null;
  return {
    snapshot_id: r.snapshot_id,
    source: r.source,
    source_url: r.source_url,
    page_url: r.page_url,
    fetched_at: r.fetched_at,
    last_seen_at: r.last_seen_at,
    http_status: r.http_status,
    body_sha256: r.body_sha256,
    parser: r.parser,
    models: JSON.parse(r.models_json),
    rejected: JSON.parse(r.rejected_json),
  };
}

// Eastern calendar day for an ISO instant (handles EST/EDT).
const ET_DAY = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' });
export const easternDay = (iso) => ET_DAY.format(new Date(iso));

export function createStore(db) {
  const all = async (sql, ...args) => (await db.prepare(sql).bind(...args).all()).results || [];
  const first = (sql, ...args) => db.prepare(sql).bind(...args).first();
  const run = (sql, ...args) => db.prepare(sql).bind(...args).run();

  return {
    async latestSnapshot(source) {
      return snapRow(await first('SELECT * FROM snapshots WHERE source = ? ORDER BY fetched_at DESC LIMIT 1', source));
    },
    async snapshotAt(source, at) {
      return snapRow(await first('SELECT * FROM snapshots WHERE source = ? AND fetched_at <= ? ORDER BY fetched_at DESC LIMIT 1', source, at));
    },
    async earliestSnapshot(source) {
      return snapRow(await first('SELECT * FROM snapshots WHERE source = ? ORDER BY fetched_at ASC LIMIT 1', source));
    },
    async touchSnapshot(snapshot_id, at) {
      await run('UPDATE snapshots SET last_seen_at = ? WHERE snapshot_id = ?', at, snapshot_id);
    },
    async saveSnapshot(s) {
      await run(
        `INSERT INTO snapshots (snapshot_id, source, source_url, page_url, fetched_at, last_seen_at, http_status, body_sha256, parser, models_json, rejected_json)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(snapshot_id) DO UPDATE SET last_seen_at = excluded.last_seen_at`,
        s.snapshot_id, s.source, s.source_url, s.page_url, s.fetched_at, s.fetched_at, s.http_status, s.body_sha256, s.parser,
        JSON.stringify(s.models), JSON.stringify(s.rejected || []),
      );
    },
    async saveFetchLog(s) {
      await run(
        'INSERT INTO fetch_log (source, at, ok, snapshot_id, attempts_json) VALUES (?, ?, ?, ?, ?)',
        s.source, s.fetched_at, s.ok ? 1 : 0, s.snapshot_id || null, JSON.stringify(s.attempts || []),
      );
    },
    async lastFetch(source) {
      const r = await first('SELECT * FROM fetch_log WHERE source = ? ORDER BY id DESC LIMIT 1', source);
      return r ? { ...r, ok: !!r.ok, attempts: JSON.parse(r.attempts_json) } : null;
    },

    // Upsert one priced turn (see engine.priceTurn) under owner + lane + request_id.
    async recordTurn(owner, meta, p) {
      const t = p.turn;
      const id = (await sha256Hex(`${owner}|${meta.lane}|${meta.request_id}`)).slice(0, 32);
      await run(
        `INSERT INTO turns (id, owner, lane, session_id, request_id, client, provider, model, at,
           input_tokens, output_tokens, cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens, reasoning_tokens,
           status, cost_nano, savings_nano, snapshot_id, source_url, price_fetched_at, band_json, reason, received_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(id) DO UPDATE SET
           session_id = excluded.session_id, client = excluded.client, provider = excluded.provider, model = excluded.model, at = excluded.at,
           input_tokens = excluded.input_tokens, output_tokens = excluded.output_tokens, cache_read_tokens = excluded.cache_read_tokens,
           cache_write_5m_tokens = excluded.cache_write_5m_tokens, cache_write_1h_tokens = excluded.cache_write_1h_tokens,
           reasoning_tokens = excluded.reasoning_tokens, status = excluded.status, cost_nano = excluded.cost_nano,
           savings_nano = excluded.savings_nano, snapshot_id = excluded.snapshot_id, source_url = excluded.source_url,
           price_fetched_at = excluded.price_fetched_at, band_json = excluded.band_json, reason = excluded.reason,
           received_at = excluded.received_at`,
        id, owner, meta.lane, meta.session_id || null, meta.request_id, meta.client || null,
        p.price?.provider || t.provider || null, t.model, t.at,
        t.input_tokens, t.output_tokens, t.cache_read_tokens, t.cache_write_5m_tokens, t.cache_write_1h_tokens, t.reasoning_tokens,
        p.status, p.cost_nano === null ? null : Number(p.cost_nano), p.cache_savings_usd ? Math.round(p.cache_savings_usd * 1e9) : 0,
        p.price?.ref?.snapshot_id || null, p.price?.ref?.source_url || null, p.price?.ref?.fetched_at || null,
        p.price?.band ? JSON.stringify(p.price.band) : null, p.reason || null, new Date().toISOString(),
      );
      return id;
    },

    async listTurns(owner, { session_id, lane, since, until, limit = 50 } = {}) {
      const where = ['owner = ?'];
      const args = [owner];
      if (session_id) where.push('session_id = ?'), args.push(session_id);
      if (lane) where.push('lane = ?'), args.push(lane);
      if (since) where.push('at >= ?'), args.push(since);
      if (until) where.push('at < ?'), args.push(until);
      const rows = await all(`SELECT * FROM turns WHERE ${where.join(' AND ')} ORDER BY at DESC LIMIT ?`, ...args, Math.min(Math.max(1, limit), 500));
      return rows.map((r) => ({
        ...r,
        cost_usd: r.cost_nano === null ? null : nanoToUsd(BigInt(r.cost_nano)),
        cache_savings_usd: r.savings_nano ? r.savings_nano / 1e9 : 0,
        band: r.band_json ? JSON.parse(r.band_json) : null,
        band_json: undefined,
      }));
    },

    // Totals grouped by model | provider | lane | session | client | day (Eastern).
    async usage(owner, { group_by = 'model', since, until, session_id, lane } = {}) {
      const byDay = group_by === 'day';
      const key = byDay ? GROUPS.hour : GROUPS[group_by];
      if (!key) throw new Error(`group_by must be one of ${[...Object.keys(GROUPS).filter((g) => g !== 'hour'), 'day'].join(', ')}`);
      const where = ['owner = ?'];
      const args = [owner];
      if (since) where.push('at >= ?'), args.push(since);
      if (until) where.push('at < ?'), args.push(until);
      if (session_id) where.push('session_id = ?'), args.push(session_id);
      if (lane) where.push('lane = ?'), args.push(lane);
      const rows = await all(
        `SELECT ${key} AS k, COUNT(*) AS turns,
           SUM(input_tokens) AS input, SUM(output_tokens) AS output, SUM(cache_read_tokens) AS cache_read,
           SUM(cache_write_5m_tokens + cache_write_1h_tokens) AS cache_write, SUM(reasoning_tokens) AS reasoning,
           SUM(COALESCE(cost_nano, 0)) AS cost_nano, SUM(COALESCE(savings_nano, 0)) AS savings_nano,
           SUM(CASE WHEN status IN ('unpriced', 'quarantined') THEN 1 ELSE 0 END) AS unpriced,
           SUM(CASE WHEN status = 'local' THEN 1 ELSE 0 END) AS local,
           MIN(at) AS first_at, MAX(at) AS last_at
         FROM turns WHERE ${where.join(' AND ')} GROUP BY k`,
        ...args,
      );
      const groups = new Map();
      for (const r of rows) {
        const k = byDay ? easternDay(`${r.k}:00:00Z`) : r.k;
        const g = groups.get(k) || { key: k, turns: 0, input: 0, output: 0, cache_read: 0, cache_write: 0, reasoning: 0, cost_nano: 0, savings_nano: 0, unpriced: 0, local: 0, first_at: r.first_at, last_at: r.last_at };
        for (const f of ['turns', 'input', 'output', 'cache_read', 'cache_write', 'reasoning', 'cost_nano', 'savings_nano', 'unpriced', 'local']) g[f] += r[f] || 0;
        if (r.first_at < g.first_at) g.first_at = r.first_at;
        if (r.last_at > g.last_at) g.last_at = r.last_at;
        groups.set(k, g);
      }
      const out = [...groups.values()].map((g) => {
        const read = g.input + g.cache_read + g.cache_write;
        return {
          ...g,
          total_tokens: read + g.output,
          cost_usd: g.cost_nano / 1e9,
          cache_savings_usd: g.savings_nano / 1e9,
          cache_hit_rate: read ? g.cache_read / read : null,
        };
      });
      out.sort(byDay ? (a, b) => (a.key < b.key ? 1 : -1) : (a, b) => b.cost_nano - a.cost_nano || b.total_tokens - a.total_tokens);
      return out;
    },

    // The OAuth signing secret, minted once into D1 when the Worker has none set.
    // INSERT OR IGNORE then re-read, so two cold starts racing agree on one value.
    async signingSecret() {
      const have = await first("SELECT value FROM settings WHERE key = 'signing_secret'");
      if (have) return have.value;
      const raw = crypto.getRandomValues(new Uint8Array(48));
      const value = btoa(String.fromCharCode(...raw)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
      await run("INSERT OR IGNORE INTO settings (key, value, created_at) VALUES ('signing_secret', ?, ?)", value, new Date().toISOString());
      return (await first("SELECT value FROM settings WHERE key = 'signing_secret'")).value;
    },

    // Session spend for budgets: per-turn rows summed, or a session rollup row (cumulative usage,
    // request_id 'rollup:...' or status 'session_rollup'), whichever is larger, so the two never double count.
    async sessionSpend(owner, session_id) {
      const row = await first(
        `SELECT COALESCE(SUM(CASE WHEN r = 0 THEN cost_nano END), 0) AS turn_nano,
                COALESCE(MAX(CASE WHEN r = 1 THEN cost_nano END), 0) AS rollup_nano,
                COALESCE(SUM(CASE WHEN cost_nano IS NULL THEN 1 ELSE 0 END), 0) AS unpriced,
                COUNT(*) AS turns, MAX(at) AS last_at
           FROM (SELECT cost_nano, at, (status = 'session_rollup' OR request_id LIKE 'rollup:%') AS r
                   FROM turns WHERE owner = ? AND session_id = ?)`,
        owner, session_id,
      );
      return { spent_nano: Math.max(Number(row?.turn_nano || 0), Number(row?.rollup_nano || 0)), unpriced: Number(row?.unpriced || 0), turns: Number(row?.turns || 0), last_at: row?.last_at || null };
    },

    async getBudget(owner, session_id) {
      return first('SELECT * FROM budgets WHERE owner = ? AND session_id = ?', owner, session_id);
    },

    async upsertBudget(owner, b, at) {
      await run(
        `INSERT INTO budgets (owner, session_id, label, cap_nano, warn_pct, stop_rule, state, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?)
         ON CONFLICT(owner, session_id) DO UPDATE SET label = COALESCE(excluded.label, label), cap_nano = excluded.cap_nano,
           warn_pct = excluded.warn_pct, stop_rule = COALESCE(excluded.stop_rule, stop_rule),
           state = CASE WHEN state = 'closed' THEN 'ok' ELSE state END, updated_at = excluded.updated_at`,
        owner, b.session_id, b.label || null, b.cap_nano, b.warn_pct, b.stop_rule || null, at, at,
      );
    },

    async saveBudgetState(owner, session_id, s, at) {
      await run(
        `UPDATE budgets SET state = ?, spent_nano = ?, unpriced = ?, checked_at = ?,
           warned_at = COALESCE(?, warned_at), over_at = COALESCE(?, over_at), updated_at = ?
         WHERE owner = ? AND session_id = ?`,
        s.state, s.spent_nano, s.unpriced, at, s.warned_at || null, s.over_at || null, at, owner, session_id,
      );
    },

    async addBudgetEvent(owner, session_id, kind, spent_nano, cap_nano, at) {
      await run('INSERT INTO budget_events (owner, session_id, at, kind, spent_nano, cap_nano) VALUES (?, ?, ?, ?, ?, ?)', owner, session_id, at, kind, spent_nano, cap_nano);
    },

    async listBudgets(owner, { active = true, limit = 100 } = {}) {
      return all(`SELECT * FROM budgets WHERE owner = ? ${active ? "AND state != 'closed'" : ''} ORDER BY updated_at DESC LIMIT ?`, owner, limit);
    },

    async budgetOwners() {
      return (await all("SELECT DISTINCT owner FROM budgets WHERE state != 'closed'")).map((r) => r.owner);
    },

    async pendingBudgetAlerts(limit = 50) {
      return all(
        `SELECT e.*, b.label, b.stop_rule FROM budget_events e LEFT JOIN budgets b ON b.owner = e.owner AND b.session_id = e.session_id
          WHERE e.kind = 'over' AND e.notified_at IS NULL ORDER BY e.id LIMIT ?`,
        limit,
      );
    },

    async markBudgetAlerts(ids, at) {
      for (const id of ids) await run('UPDATE budget_events SET notified_at = ? WHERE id = ?', at, id);
    },

    async createIngestKey(owner, label, keyHash) {
      await run('INSERT INTO ingest_keys (key_hash, owner, label, created_at) VALUES (?, ?, ?, ?)', keyHash, owner, label, new Date().toISOString());
    },
    async ingestOwner(keyHash) {
      const r = await first('SELECT owner, label FROM ingest_keys WHERE key_hash = ?', keyHash);
      if (r) await run('UPDATE ingest_keys SET last_used_at = ? WHERE key_hash = ?', new Date().toISOString(), keyHash);
      return r || null;
    },
    async listIngestKeys(owner) {
      return all('SELECT label, created_at, last_used_at, substr(key_hash, 1, 8) AS key_id FROM ingest_keys WHERE owner = ? ORDER BY created_at DESC', owner);
    },
  };
}
