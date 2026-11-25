// OpenRouter: https://openrouter.ai/api/v1/models. Published per-token USD strings; the free
// lanes publish "0". Covers the open-weight and free routes the fleet drafts on.
import { band, gateBand, normalizeModel } from './core.js';

const perM = (v) => {
  if (v === undefined || v === null || v === '') return null;
  const n = Number(v);
  if (!Number.isFinite(n) || n < 0) return undefined;
  // Per-token strings like "0.000003" -> 3 USD/MTok, computed on the decimal string so no float drift.
  const [w, f = ''] = String(v).split('.');
  const digits = (w + f.padEnd(6, '0').slice(0, 6) + '.' + f.slice(6)).replace(/^0+(?=\d)/, '');
  return Number(digits);
};

export function parseOpenRouter(text) {
  const models = [];
  const rejected = [];
  let doc;
  try {
    doc = JSON.parse(text);
  } catch {
    return { models, rejected: [{ model: '*', reason: 'response is not JSON' }] };
  }
  for (const m of doc?.data || []) {
    const p = m.pricing || {};
    const rates = {
      input: perM(p.prompt),
      output: perM(p.completion),
      cache_read: perM(p.input_cache_read),
      cache_write_5m: perM(p.input_cache_write),
      cache_write_1h: null,
    };
    const id = normalizeModel(m.id);
    if (Object.values(rates).some((v) => v === undefined) || Number(p.request || 0) > 0) {
      rejected.push({ model: id, reason: 'unreadable or per-request pricing' });
      continue;
    }
    if (rates.input < 0 || rates.output < 0 || rates.input === -1) {
      rejected.push({ model: id, reason: 'variable pricing' });
      continue;
    }
    const why = gateBand(rates);
    if (why) {
      rejected.push({ model: id, reason: why });
      continue;
    }
    models.push({ provider: 'openrouter', model: id, display: m.name || m.id, notes: m.id.endsWith(':free') ? 'free lane' : null, bands: [band(rates)] });
  }
  return { models, rejected };
}
