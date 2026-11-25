// Shared pricing primitives: model ids, unit detection, invariant gates, exact money math.
//
// Every price this service shows was read off a provider's own page or API. Nothing here
// invents a rate: a field the page does not publish stays null, and a model the parsers
// cannot read is reported as unknown. That rule is the whole product.

export const PARSER_VERSION = 'trckr-parse/1';

// USD per 1M tokens fields a band may carry. A field absent from the provider's page is null.
export const RATE_FIELDS = ['input', 'output', 'cache_read', 'cache_write_5m', 'cache_write_1h'];

// Model ids as clients report them: "claude-opus-5-5[1m]", "Claude Opus 5.5", "gemini-2.5-pro",
// "models/gemini-2.5-pro", "anthropic/claude-opus-5.5", "claude-sonnet-4-5-20250929".
export function normalizeModel(raw) {
  let s = String(raw || '').trim().toLowerCase();
  if (!s) return '';
  s = s.replace(/\[[^\]]*\]/g, ''); // [1m] context tags
  s = s.replace(/^models\//, '');
  s = s.replace(/\s*\(.*$/, ''); // "(for prompts up to ...)" qualifiers are parsed separately
  s = s.replace(/[\s_]+/g, '-');
  s = s.replace(/\./g, '-');
  s = s.replace(/-+/g, '-').replace(/^-|-$/g, '');
  return s;
}

// Lookup candidates from most to least specific. A provider prefix ("anthropic/") and a
// trailing snapshot date ("-20250929") are dropped only after the exact id misses.
export function modelCandidates(raw) {
  const exact = normalizeModel(raw);
  const out = [exact];
  const noPrefix = exact.includes('/') ? exact.split('/').pop() : exact;
  out.push(noPrefix);
  const noDate = noPrefix.replace(/-(?:20\d{6}|\d{4}-\d{2}-\d{2})$/, '');
  out.push(noDate);
  const noLatest = noDate.replace(/-(?:latest|preview|exp)$/, '');
  out.push(noLatest);
  return [...new Set(out.filter(Boolean))];
}

// USD per 1M tokens => 1.0; USD per 1K tokens => 1000. null when the text names neither or both.
export function detectUnit(text) {
  if (!text) return null;
  const s = String(text).toLowerCase();
  const k =
    /\/\s*(?:1\s*k\b|ktok|thousand)/.test(s) ||
    /per\s+(?:1\s*k\b|1,?000\s+tokens|thousand)/.test(s) ||
    /\b1k\s*tokens?\b/.test(s);
  const m =
    /\/\s*(?:1\s*m\b|mtok|million)/.test(s) ||
    /per\s+(?:1\s*m\b|1,?000,?000|million)/.test(s) ||
    /\b1m\s*tokens?\b/.test(s);
  if (k && m) return null;
  if (m) return 1;
  if (k) return 1000;
  return null;
}

export function dollars(cell) {
  const m = String(cell || '').match(/\$\s*([0-9]+(?:\.[0-9]+)?)/);
  return m ? Number(m[1]) : null;
}

// Invariant gates, ported from pricing_live.py. A row that fails a gate is rejected and
// surfaces as unknown, never "fixed up".
export function gateBand(rates, ceiling = 1000) {
  for (const f of RATE_FIELDS) {
    const v = rates[f];
    if (v === null || v === undefined) continue;
    if (!(v >= 0)) return `${f} is not a number`;
    if (v >= ceiling) return `${f} ${v} exceeds ceiling ${ceiling}`;
  }
  if (rates.input === null || rates.output === null) return 'input or output missing';
  if (rates.input === 0 && rates.output === 0) return null; // published free tier, e.g. a free OpenRouter lane
  if (rates.input <= 0 || rates.output <= 0) return 'input and output must be > 0';
  if (rates.output < rates.input) return `output ${rates.output} < input ${rates.input} (column transposition)`;
  if (rates.cache_read !== null && rates.cache_read !== undefined && rates.cache_read >= rates.input)
    return `cache_read ${rates.cache_read} >= input ${rates.input}`;
  return null;
}

// ---- exact money math -------------------------------------------------------------------
// Rates are USD per 1M tokens with at most 6 decimals. Scaled to integer micro-dollars per
// 1M tokens, the cost of n tokens in nano-dollars is n * micro / 1e3: integer, exact, and
// rounded half-even once at the end. No float ever touches a stored cost.

const SCALE = 1_000_000n;

export function toMicro(rate) {
  if (rate === null || rate === undefined) return null;
  const [w, f = ''] = String(rate).split('.');
  const frac = (f + '000000').slice(0, 6);
  return BigInt(w) * SCALE + BigInt(frac || '0');
}

function divHalfEven(num, den) {
  const q = num / den;
  const r = num % den;
  const twice = r * 2n;
  if (twice > den || (twice === den && q % 2n === 1n)) return q + 1n;
  return q;
}

// tokens (int) at rate (USD/MTok) -> nano-USD (BigInt)
export function costNano(tokens, rate) {
  const micro = toMicro(rate);
  if (micro === null) return null;
  return divHalfEven(BigInt(Math.max(0, Math.trunc(tokens || 0))) * micro, 1000n);
}

export function nanoToUsd(nano) {
  if (nano === null || nano === undefined) return null;
  return Number(nano) / 1e9;
}

export function fmtUsd(usd) {
  if (usd === null || usd === undefined) return 'unknown';
  if (usd === 0) return '$0.00';
  if (usd < 0.01) return `$${usd.toFixed(5)}`;
  if (usd < 1) return `$${usd.toFixed(4)}`;
  return `$${usd.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export async function sha256Hex(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(String(text)));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

const MONTHS = {
  jan: 1, january: 1, feb: 2, february: 2, mar: 3, march: 3, apr: 4, april: 4, may: 5,
  jun: 6, june: 6, jul: 7, july: 7, aug: 8, august: 8, sep: 9, sept: 9, september: 9,
  oct: 10, october: 10, nov: 11, november: 11, dec: 12, december: 12,
};

// "November 30, 2026" -> "2026-11-30"
export function isoDate(text) {
  const m = String(text || '').match(/([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})/);
  if (!m) return null;
  const mo = MONTHS[m[1].toLowerCase()];
  if (!mo) return null;
  return `${m[3]}-${String(mo).padStart(2, '0')}-${String(m[2]).padStart(2, '0')}`;
}

export function dayAfter(iso) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

// "<= 200k", "> 200k", "up to 100,000", "over 100,000", "(<272K context length)"
export function parseThreshold(text) {
  const s = String(text || '').toLowerCase().replace(/,/g, '');
  const num = (v, unit) => Math.round(Number(v) * (unit === 'k' ? 1000 : unit === 'm' ? 1_000_000 : 1));
  let m = s.match(/(?:<=|≤|up to)\s*([\d.]+)\s*([km]?)/);
  if (m) return { max_prompt_tokens: num(m[1], m[2]) };
  m = s.match(/(?:<)\s*([\d.]+)\s*([km]?)/);
  if (m) return { max_prompt_tokens: num(m[1], m[2]) - 1 };
  m = s.match(/(?:>=|≥)\s*([\d.]+)\s*([km]?)/);
  if (m) return { min_prompt_tokens: num(m[1], m[2]) };
  m = s.match(/(?:>|over|above|more than)\s*([\d.]+)\s*([km]?)/);
  if (m) return { min_prompt_tokens: num(m[1], m[2]) + 1 };
  return null;
}

export function band(rates, extra = {}) {
  const out = {
    min_prompt_tokens: null,
    max_prompt_tokens: null,
    valid_from: null,
    valid_until: null,
  };
  for (const f of RATE_FIELDS) out[f] = rates[f] ?? null;
  return { ...out, ...extra };
}
