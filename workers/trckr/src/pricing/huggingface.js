// Hugging Face Inference Providers: https://router.huggingface.co/v1/models. Each model lists the
// providers that serve it, each with its own USD per 1M token price. A turn names its route the
// way the router does, "model:provider" (e.g. deepseek-ai/DeepSeek-V4.1-Flash:fireworks-ai). A bare
// model id is priced only when exactly one live provider serves it: with several, which one ran is
// unknown, so the turn stays unpriced rather than guessed.
import { band, gateBand, normalizeModel } from './core.js';

const rate = (v) => (v === undefined || v === null || v === '' ? undefined : Number.isFinite(Number(v)) && Number(v) >= 0 ? Number(v) : undefined);

export function parseHuggingFace(text) {
  const models = [];
  const rejected = [];
  let doc;
  try {
    doc = JSON.parse(text);
  } catch {
    return { models, rejected: [{ model: '*', reason: 'response is not JSON' }] };
  }
  for (const m of doc?.data || []) {
    if (!m?.id) continue;
    const base = normalizeModel(m.id);
    const live = (m.providers || []).filter((p) => p?.provider && (p.status || 'live') === 'live');
    const priced = [];
    for (const p of live) {
      const id = `${base}:${normalizeModel(p.provider)}`;
      const rates = { input: rate(p.pricing?.input), output: rate(p.pricing?.output), cache_read: null, cache_write_5m: null, cache_write_1h: null };
      if (rates.input === undefined || rates.output === undefined) {
        rejected.push({ model: id, reason: 'no per-token price published' });
        continue;
      }
      const why = gateBand(rates);
      if (why) {
        rejected.push({ model: id, reason: why });
        continue;
      }
      priced.push({ provider: 'huggingface', model: id, display: `${m.id} via ${p.provider_name || p.provider}`, notes: p.is_free ? 'free' : null, aliases: [], bands: [band(rates)] });
    }
    if (priced.length === 1 && live.length === 1) priced[0].aliases.push(base);
    models.push(...priced);
  }
  return { models, rejected };
}
