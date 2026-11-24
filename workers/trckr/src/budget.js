// Budget enforcement. Each session registers a dollar cap and a stop rule; every check prices the
// session's real token usage at list price (per-turn rows, or one cumulative rollup) and answers
// ok | warn | over. Who hears what (Dave's call): the spending session itself gets the warning at
// warn_pct, through its own check, so it can wrap up and write a handoff shard. Only a session at
// its cap raises an 'over' alert to the coordinator. Nobody pings the human at 80%.
import { priceTurn } from './engine.js';
import { fmtUsd } from './pricing/core.js';

const NANO = 1e9;
const usd = (nano) => fmtUsd(Number(nano) / NANO);
const MAX_CAP_USD = 10000;

export function budgetText(b) {
  if (!b) return '';
  const head = `${b.label ? `${b.label}: ` : ''}${usd(b.spent_nano)} of ${usd(b.cap_nano)} (${b.pct}%)`;
  const gap = b.unpriced ? ` ${b.unpriced} turn(s) unpriced, so real spend is higher.` : '';
  if (b.state === 'over') return `OVER BUDGET. ${head}. Stop now: no more model calls. Stop rule: ${b.stop_rule || 'stop at cap'}.${gap}`;
  if (b.state === 'warn') return `Budget warning. ${head}, ${usd(b.remaining_nano)} left. Wrap up: finish the current step, write a handoff shard, and report.${gap}`;
  if (b.state === 'closed') return `Budget closed. ${head}.`;
  return `Budget ok. ${head}, ${usd(b.remaining_nano)} left.${gap}`;
}

export function createBudgets({ store, now = () => new Date().toISOString() }) {
  function shape(row, spend) {
    const cap = Number(row.cap_nano);
    const spent = spend.spent_nano;
    const pct = cap > 0 ? Math.floor((spent * 100) / cap) : 100;
    let state = row.state === 'closed' ? 'closed' : spent >= cap ? 'over' : pct >= row.warn_pct ? 'warn' : 'ok';
    return {
      session_id: row.session_id,
      label: row.label || null,
      state,
      stop: state === 'over',
      warn: state === 'warn',
      spent_nano: spent,
      cap_nano: cap,
      remaining_nano: Math.max(0, cap - spent),
      spent_usd: Number((spent / NANO).toFixed(6)),
      cap_usd: cap / NANO,
      pct,
      warn_pct: row.warn_pct,
      stop_rule: row.stop_rule || null,
      unpriced: spend.unpriced,
      turns: spend.turns,
      last_turn_at: spend.last_at,
    };
  }

  // Re-price and move the state; a threshold crossing records one event.
  async function evaluate(owner, session_id) {
    const row = await store.getBudget(owner, session_id);
    if (!row) return null;
    const spend = await store.sessionSpend(owner, session_id);
    const b = shape(row, spend);
    const at = now();
    const crossed = b.state !== row.state && b.state !== 'ok' && b.state !== 'closed' && !(row.state === 'over' && b.state === 'warn');
    await store.saveBudgetState(owner, session_id, { state: b.state, spent_nano: b.spent_nano, unpriced: b.unpriced, warned_at: crossed && b.state === 'warn' ? at : null, over_at: crossed && b.state === 'over' ? at : null }, at);
    if (crossed) await store.addBudgetEvent(owner, session_id, b.state, b.spent_nano, b.cap_nano, at);
    return b;
  }

  async function set(owner, a) {
    const session_id = String(a.session_id || '').trim().slice(0, 200);
    const cap = Number(a.cap_usd);
    if (!session_id) throw new Error('session_id is required');
    if (!(cap > 0 && cap <= MAX_CAP_USD)) throw new Error(`cap_usd must be above 0 and at most ${MAX_CAP_USD}`);
    const warn_pct = Math.min(99, Math.max(1, Math.round(Number(a.warn_pct || 80))));
    const at = now();
    await store.upsertBudget(owner, { session_id, label: a.label ? String(a.label).slice(0, 120) : null, cap_nano: Math.round(cap * NANO), warn_pct, stop_rule: a.stop_rule ? String(a.stop_rule).slice(0, 300) : null }, at);
    await store.addBudgetEvent(owner, session_id, 'set', 0, Math.round(cap * NANO), at);
    return evaluate(owner, session_id);
  }

  async function close(owner, session_id) {
    const row = await store.getBudget(owner, session_id);
    if (!row) return null;
    const at = now();
    await store.saveBudgetState(owner, session_id, { state: 'closed', spent_nano: row.spent_nano, unpriced: row.unpriced }, at);
    await store.addBudgetEvent(owner, session_id, 'closed', row.spent_nano, row.cap_nano, at);
    return { ...(await evaluate(owner, session_id)), state: 'closed', stop: true, warn: false };
  }

  // Cumulative usage for the whole session (e.g. a cloud session's own usage block), priced at list
  // price and kept as one rollup row that is overwritten on each report. Prompt-size tiers are per
  // request, so a total is priced at the base tier: a floor when some requests ran long-context.
  async function rollup(owner, session_id, u, lane) {
    if (!u?.model) throw new Error('usage.model is required');
    const p = await priceTurn(store, { ...u, rollup: true });
    await store.recordTurn(owner, { lane: String(lane || u.lane || 'session').slice(0, 80), request_id: `rollup:${session_id}`, session_id, client: u.client ? String(u.client).slice(0, 60) : null }, p);
    return p;
  }

  // Model-free watcher (cron): re-check every open budget, then push unsent 'over' alerts.
  async function watch({ alertUrl, alertToken, fetchImpl = fetch } = {}) {
    let checked = 0;
    for (const owner of await store.budgetOwners()) {
      for (const row of await store.listBudgets(owner, { active: true, limit: 500 })) {
        await evaluate(owner, row.session_id);
        checked++;
      }
    }
    const alerts = await store.pendingBudgetAlerts();
    let sent = 0;
    if (alertUrl && alerts.length) {
      const body = {
        source: 'nougen-trckr',
        kind: 'budget_over',
        alerts: alerts.map((e) => ({ session_id: e.session_id, label: e.label, spent_usd: Number(e.spent_nano) / NANO, cap_usd: Number(e.cap_nano) / NANO, stop_rule: e.stop_rule, at: e.at, text: `${e.label || e.session_id} hit its ${usd(e.cap_nano)} cap at ${usd(e.spent_nano)} and was told to stop.` })),
      };
      try {
        const res = await fetchImpl(alertUrl, { method: 'POST', headers: { 'content-type': 'application/json', ...(alertToken ? { authorization: `Bearer ${alertToken}` } : {}) }, body: JSON.stringify(body) });
        if (res.ok) {
          await store.markBudgetAlerts(alerts.map((e) => e.id), now());
          sent = alerts.length;
        }
      } catch {
        // Left unsent; the next run retries.
      }
    }
    return { checked, alerts: alerts.length, sent };
  }

  return { evaluate, set, close, rollup, watch };
}
