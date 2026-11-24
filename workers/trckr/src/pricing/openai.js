// OpenAI: https://developers.openai.com/api/docs/pricing (Markdown via content negotiation,
// HTML hydration props as the fallback). Standard tier only; Batch, Flex, Priority, fine-tuning
// and multimodal tables describe other SKUs and are skipped, not flattened.
import { band, dollars, gateBand, normalizeModel, parseThreshold } from './core.js';
import { merge } from './anthropic.js';

const cell = (row, ...names) => {
  for (const n of names) if (row[n] !== undefined) return row[n];
  return undefined;
};

function rate(v) {
  if (v === undefined) return undefined;
  const t = String(v).trim();
  if (t === '-' || t === '' || /^n\/?a$/i.test(t)) return null;
  const d = dollars(t);
  return d === null ? undefined : d; // undefined = unreadable cell, the row is rejected
}

function markdown(text, out, rejected) {
  let tier = 'standard';
  let headers = null;
  let skip = false;
  for (const line of String(text).split(/\r?\n/)) {
    const t = line.trim();
    const tierHead = t.match(/^(?:#{1,4}\s*)?(Standard|Batch|Flex|Priority|Fast|Ultrafast)(?:\s+pricing(?:\s+data)?)?\s*$/i);
    if (tierHead) tier = tierHead[1].toLowerCase();
    if (/^#{1,4}\s*fine-?tuning/i.test(t) || /^fine-?tuning$/i.test(t)) skip = true;
    if (/^#{1,4}\s*(?:text tokens|models|cyber models|specialized models)/i.test(t)) skip = false;
    if (!t.startsWith('|')) {
      headers = null;
      continue;
    }
    const cells = t.replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
    if (cells.every((c) => /^:?-{3,}:?$/.test(c))) continue;
    if (!headers) {
      headers = cells;
      continue;
    }
    if (tier !== 'standard' || skip || cells.length !== headers.length) continue;
    const row = Object.fromEntries(headers.map((h, i) => [h, cells[i]]));
    if ('Modality' in row || 'Training' in row) continue;
    const name = row.Model;
    if (!name) continue;
    const short = {
      input: rate(cell(row, 'Short context input', 'Input')),
      cache_read: rate(cell(row, 'Short context cached input', 'Cached input')),
      cache_write_5m: rate(cell(row, 'Short context cache writes', 'Cache writes')),
      output: rate(cell(row, 'Short context output', 'Output')),
    };
    if (Object.values(short).some((v) => v === undefined && v !== null)) {
      // An unreadable cell is not a missing price; reject the row instead of half-reading it.
      if (short.input === undefined || short.output === undefined) continue;
      for (const k of Object.keys(short)) if (short[k] === undefined) short[k] = null;
    }
    short.cache_write_1h = null;
    const id = normalizeModel(name);
    const why = gateBand(short);
    if (why) {
      rejected.push({ model: id, reason: why });
      continue;
    }
    const qual = name.match(/\(([^)]*)\)/);
    const limit = qual ? parseThreshold(qual[1]) : null;
    const bands = [band(short, limit || {})];
    const long = {
      input: rate(row['Long context input']),
      cache_read: rate(row['Long context cached input']),
      cache_write_5m: rate(row['Long context cache writes']),
      output: rate(row['Long context output']),
      cache_write_1h: null,
    };
    if (long.input && long.output && limit && limit.max_prompt_tokens) {
      for (const k of Object.keys(long)) if (long[k] === undefined) long[k] = null;
      if (!gateBand(long)) bands.push(band(long, { min_prompt_tokens: limit.max_prompt_tokens + 1 }));
    }
    out.push({ provider: 'openai', model: id, display: name.replace(/\s*\(.*\)$/, ''), notes: null, bands });
  }
}

// Hydration rows: [0,"name"],[0,input],[0,cached],[0,write],[0,output]. Only the five-cell
// shape is read; its column order is the one the Python resolver has verified since August.
function hydration(text, out, rejected) {
  const unescaped = String(text)
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');
  const num = '([0-9]+(?:\\.[0-9]+)?|null|"-")';
  const re = new RegExp(`\\[0,\\s*"([^"]+?)"\\]\\s*,\\s*\\[0,\\s*${num}\\]\\s*,\\s*\\[0,\\s*${num}\\]\\s*,\\s*\\[0,\\s*${num}\\]\\s*,\\s*\\[0,\\s*([0-9]+(?:\\.[0-9]+)?)\\]\\]`, 'g');
  const v = (x) => (x === 'null' || x === '"-"' ? null : Number(x));
  for (const m of unescaped.matchAll(re)) {
    const rates = { input: v(m[2]), cache_read: v(m[3]), cache_write_5m: v(m[4]), output: v(m[5]), cache_write_1h: null };
    const id = normalizeModel(m[1]);
    if (out.some((x) => x.model === id)) continue;
    const why = gateBand(rates);
    if (why) {
      rejected.push({ model: id, reason: why });
      continue;
    }
    const qual = m[1].match(/\(([^)]*)\)/);
    out.push({
      provider: 'openai',
      model: id,
      display: m[1].replace(/\s*\(.*\)$/, ''),
      notes: null,
      bands: [band(rates, (qual && parseThreshold(qual[1])) || {})],
    });
  }
}

export function parseOpenAI(text) {
  const models = [];
  const rejected = [];
  if (!text) return { models, rejected };
  markdown(text, models, rejected);
  if (!models.length) hydration(text, models, rejected);
  return { models: merge(models), rejected };
}
