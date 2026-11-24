// Anthropic: https://platform.claude.com/docs/en/about-claude/pricing.md (Markdown on request).
// Reads the "Model pricing" table by header name. Every column is published explicitly
// (base input, 5m and 1h cache writes, cache hits, output), so nothing is derived.
import { band, detectUnit, dollars, gateBand, normalizeModel, parseThreshold } from './core.js';

const COLS = {
  input: (h) => h.includes('base input'),
  cache_write_5m: (h) => h.includes('5m cache'),
  cache_write_1h: (h) => h.includes('1h cache'),
  cache_read: (h) => h.includes('cache hit'),
  output: (h) => h.includes('output'),
};

const strip = (cell) =>
  String(cell)
    .replace(/<sup>.*?<\/sup>/gi, '')
    .replace(/<[^>]+>/g, '')
    .trim();

export function parseAnthropic(text) {
  const models = [];
  const rejected = [];
  if (!text) return { models, rejected };
  const pageUnit = detectUnit(text);
  let headers = null;
  for (const line of String(text).split(/\r?\n/)) {
    const t = line.trim();
    if (!t.startsWith('|')) {
      headers = null;
      continue;
    }
    const cells = t.replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
    if (cells.every((c) => /^:?-{3,}:?$/.test(c))) continue;
    const low = cells.map((c) => c.toLowerCase());
    if (low.some((c) => c.includes('base input'))) {
      headers = low;
      continue;
    }
    if (!headers || cells.length < headers.length) continue;
    const nameCell = cells[0];
    const display = nameCell.replace(/\(\[[^\]]*\]\([^)]*\)\)/g, '').trim(); // "([retired...](url))"
    const notes = [];
    const link = nameCell.match(/\(\[([^\]]*)\]/);
    if (link) notes.push(link[1]);
    const qual = display.match(/\(([^)]*)\)/);
    const base = display.replace(/\s*\(.*\)\s*$/, '').trim();
    if (!/^claude\b/i.test(base)) continue;
    const rates = {};
    for (const [field, test] of Object.entries(COLS)) {
      const i = headers.findIndex(test);
      if (i < 0) {
        rates[field] = null;
        continue;
      }
      const cell = strip(cells[i]);
      const unit = detectUnit(cell) ?? pageUnit;
      const v = dollars(cell);
      rates[field] = v === null || unit === null ? null : v * unit;
    }
    const id = normalizeModel(base);
    const why = gateBand(rates);
    if (why) {
      rejected.push({ model: id, reason: why });
      continue;
    }
    const threshold = qual ? parseThreshold(qual[1]) : null;
    models.push({
      provider: 'anthropic',
      model: id,
      display: base,
      notes: notes.join('; ') || null,
      bands: [band(rates, threshold || {})],
    });
  }
  return { models: merge(models), rejected };
}

// Tiered rows ("Claude Haiku 5.5 (for prompts up to ...)" / "(... over ...)") share one model.
export function merge(models) {
  const by = new Map();
  for (const m of models) {
    const prev = by.get(`${m.provider}:${m.model}`);
    if (prev) prev.bands.push(...m.bands);
    else by.set(`${m.provider}:${m.model}`, { ...m, bands: [...m.bands] });
  }
  return [...by.values()];
}
