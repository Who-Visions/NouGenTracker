import { readFileSync } from 'node:fs';
import { createD1 } from '../dev/d1shim.mjs';
import { createStore } from '../src/store.js';

export const fixture = (name) => readFileSync(new URL(`./fixtures/${name}`, import.meta.url), 'utf8');

// Serves saved copies of the providers' own pages; anything else answers 404 (= unreadable).
export const FIXTURE_PAGES = {
  'https://platform.claude.com/docs/en/about-claude/pricing.md': 'anthropic_pricing_live_2026-11-24.md',
  'https://developers.openai.com/api/docs/pricing?latest-pricing=standard': 'openai_standard_catalog.md',
  'https://ai.google.dev/gemini-api/docs/pricing.md.txt': 'gemini_pricing.md',
};

export function fixtureFetch(pages = FIXTURE_PAGES) {
  return async (url) => {
    const f = pages[url];
    if (!f) return new Response('', { status: 404 });
    return new Response(typeof f === 'function' ? f() : fixture(f), { status: 200 });
  };
}

export function clock(iso) {
  let t = new Date(iso).getTime();
  const now = () => new Date(t);
  now.set = (v) => (t = new Date(v).getTime());
  return now;
}

export function freshStore() {
  return createStore(createD1());
}
