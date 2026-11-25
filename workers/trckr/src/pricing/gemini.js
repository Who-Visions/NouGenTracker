// Google Gemini: https://ai.google.dev/gemini-api/docs/pricing.md.txt (Markdoc), HTML fallback
// is not attempted: a page the Markdoc reader cannot read shows as unknown.
// Reads the Standard tab of each model section. Prompt-size tiers ("<= 200k") and dated
// schedules ("through November 30, 2026" / "starting December 1, 2026") become bands, so a
// turn is priced by its own size and its own date.
import { band, dayAfter, detectUnit, dollars, gateBand, isoDate, normalizeModel, parseThreshold } from './core.js';

const FEATURES = [
  ['input', /^input price/i],
  ['output', /^output price/i],
  ['cache_read', /^context caching price/i],
];

function standardTable(body) {
  const tab = body.match(/\{%\s*tab title="Standard"\s*%\}([\s\S]*?)\{%\s*\/tab\s*%\}/);
  if (tab) return tab[1];
  if (/\{%\s*tabs\s*%\}/.test(body)) return null; // tabs without Standard: another SKU
  const table = body.match(/\{%\s*table\s*%\}([\s\S]*?)\{%\s*\/table\s*%\}/);
  return table ? table[1] : null;
}

function rows(table) {
  return table
    .split(/\n\s*---\s*\n/)
    .map((chunk) => {
      const cells = [];
      for (const raw of chunk.split('\n')) {
        const line = raw.trim();
        if (!line || line.startsWith('{%')) continue;
        if (line.startsWith('* ')) cells.push(line.slice(2).trim());
        else if (cells.length) cells[cells.length - 1] += `\n${line}`;
      }
      return cells;
    })
    .filter((c) => c.length >= 3);
}

function entries(paid, unit) {
  const out = [];
  for (const piece of paid.split('\n').map((s) => s.trim()).filter(Boolean)) {
    if (/storage|per hour|\/\s*1,000\b(?!,)|per 1,000 (?:requests|search|grounded)/i.test(piece)) continue;
    const paren = piece.match(/\(([^)]*)\)/);
    if (paren && /audio|image|video/i.test(paren[1]) && !/text/i.test(paren[1])) continue;
    const v = dollars(piece);
    if (v === null) continue;
    const pieceUnit = detectUnit(piece) ?? unit;
    if (pieceUnit === null) continue;
    const th = parseThreshold(piece.replace(/\$[0-9.]+/, '')) || {};
    const until = piece.match(/\bthrough\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})/i);
    const from = piece.match(/\b(?:starting|effective|from|after)\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})/i);
    out.push({
      v: v * pieceUnit,
      min: th.min_prompt_tokens ?? null,
      max: th.max_prompt_tokens ?? null,
      from: from ? isoDate(from[1]) : null,
      until: until ? isoDate(until[1]) : null,
    });
  }
  return out;
}

const key = (e) => `${e.min}|${e.max}|${e.from}|${e.until}`;

// The entry of `list` that applies to the band described by `e`.
function match(list, e) {
  if (!list.length) return null;
  const exact = list.find((x) => key(x) === key(e));
  if (exact) return exact;
  const sameTier = list.filter((x) => x.min === e.min && x.max === e.max);
  const sameWindow = list.filter((x) => x.from === e.from && x.until === e.until);
  const flat = list.filter((x) => x.min === null && x.max === null && x.from === null && x.until === null);
  if (sameTier.length === 1 && sameTier[0].from === null && sameTier[0].until === null) return sameTier[0];
  if (sameWindow.length === 1 && sameWindow[0].min === null && sameWindow[0].max === null) return sameWindow[0];
  if (list.length === 1 && flat.length === 1) return flat[0];
  return null;
}

export function parseGemini(text) {
  const models = [];
  const rejected = [];
  if (!text) return { models, rejected };
  const sections = String(text).split(/\n(?=## )/);
  for (const section of sections) {
    const head = section.match(/^## (.*?)\s*\{%\s*id="([^"]+)"\s*%\}/);
    if (!head) continue;
    const sectionId = head[2];
    if (!/^(gemini|gemma)/.test(sectionId)) continue;
    const table = standardTable(section);
    if (!table) continue;
    const r = rows(table);
    const header = r.find((c) => /^feature$/i.test(c[0]));
    const unit = header ? detectUnit(header[2]) : null;
    const found = {};
    for (const cells of r) {
      for (const [field, re] of FEATURES) {
        if (re.test(cells[0]) && !found[field]) found[field] = entries(cells[2], unit);
      }
    }
    const display = head[1].replace(/\[([^\]]*)\]\([^)]*\)/g, '$1').trim();
    const id = normalizeModel(sectionId);
    if (!found.input?.length || !found.output?.length) {
      if (found.input || found.output) rejected.push({ model: id, reason: 'no paid text input/output price on the page' });
      continue;
    }
    const bands = [];
    for (const e of found.input) {
      const out = match(found.output, e);
      if (!out) {
        rejected.push({ model: id, reason: `no output price matching input tier ${key(e)}` });
        continue;
      }
      const cache = found.cache_read ? match(found.cache_read, e) : null;
      const rates = { input: e.v, output: out.v, cache_read: cache ? cache.v : null, cache_write_5m: null, cache_write_1h: null };
      const why = gateBand(rates);
      if (why) {
        rejected.push({ model: id, reason: why });
        continue;
      }
      // "through Nov 30" ends that day; "starting Dec 1" begins it. Store half-open [from, until).
      const valid_until = e.until ? dayAfter(e.until) : out.until ? dayAfter(out.until) : null;
      bands.push(
        band(rates, {
          min_prompt_tokens: e.min,
          max_prompt_tokens: e.max,
          valid_from: e.from || out.from || null,
          valid_until,
        }),
      );
    }
    if (!bands.length) continue;
    // Only the ids in the section's own model badge line (*[`gemini-x`](/docs/models/...)*), never
    // ids mentioned in callouts such as "requests now route to ...".
    const badge = section.match(/^\s*\*\[`[^\n]*$/m);
    const aliases = badge ? [...badge[0].matchAll(/`((?:gemini|gemma)[a-z0-9.\-]*)`/g)].map((m) => normalizeModel(m[1])) : [];
    models.push({ provider: 'google', model: id, display, notes: null, aliases: [...new Set(aliases)].filter((a) => a !== id), bands });
  }
  return { models, rejected };
}
