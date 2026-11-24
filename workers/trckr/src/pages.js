// NouGen//Trckr pages: the OAuth steps (same three-step rail as NouGen//Eye) and the live board,
// in Trckr's own look: gold for money and radar green for live tracking on black glass, every price with its source.
import { fmtUsd } from './pricing/core.js';

export const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

// 12-hour Eastern, the fleet's clock: "Nov 24, 1:42 AM EST".
const ET = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
const ET_TIME = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit', second: '2-digit' });
export const eastern = (iso) => (iso ? ET.format(new Date(iso)).replace(/,(?=[^,]*$)/, '') : '');
const clock = (iso) => (iso ? ET_TIME.format(new Date(iso)) : '');

const tok = (n) => {
  if (n === null || n === undefined) return '0';
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e4) return `${(n / 1e3).toFixed(1)}K`;
  return n.toLocaleString('en-US');
};
const rate = (v) => (v === null || v === undefined ? '<span class="na">n/a</span>' : `$${Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`);
const pct = (v) => (v === null || v === undefined ? 'n/a' : `${(v * 100).toFixed(1)}%`);
const money = (v) => (v === null || v === undefined ? '<span class="na">unpriced</span>' : esc(fmtUsd(v)));

const CSS = `
:root{color-scheme:dark;--gold:#f5c451;--gold2:#ffdd8a;--ink:#f6f1e4;--muted:#a89f8a;--dim:#6f6857;--line:rgba(245,196,81,.2);--glass:rgba(255,255,255,.035);--ok:#7be0a6;--track:#2ee6a6;--track2:#8bf5d2;--trackline:rgba(46,230,166,.28);--warn:#ffb86b;--bad:#ff7a7a}
*{box-sizing:border-box}
html{background:#070604}
body{margin:0;min-height:100vh;font:15px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:var(--ink);
background:radial-gradient(70% 45% at 50% -5%,rgba(245,196,81,.17),transparent 70%),radial-gradient(40% 30% at 100% 100%,rgba(245,196,81,.06),transparent 70%),#070604}
a{color:var(--gold2)}
.num,td.n,th.n{font-variant-numeric:tabular-nums}
.mark{width:52px;height:52px;border-radius:16px;display:grid;place-items:center;border:1px solid var(--line);font-weight:800;font-size:13px;letter-spacing:.06em;color:var(--gold);
background:radial-gradient(circle at 30% 20%,rgba(245,196,81,.35),rgba(245,196,81,.04) 70%);box-shadow:inset 0 0 0 1px rgba(255,255,255,.04),0 8px 30px rgba(245,196,81,.12)}
.btn{display:flex;align-items:center;justify-content:center;gap:12px;width:100%;min-height:52px;margin:0 0 12px;padding:0 20px;border-radius:14px;font:inherit;font-weight:650;text-decoration:none;cursor:pointer;border:1px solid var(--line);background:rgba(255,255,255,.04);color:var(--ink)}
.btn.primary{background:linear-gradient(180deg,var(--gold2),var(--gold));border-color:var(--gold);color:#1d1503;box-shadow:0 8px 28px rgba(245,196,81,.25)}
.btn:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--gold);outline-offset:3px}
.na{color:var(--dim)}
`;

const AUTH_CSS = `
body{display:grid;place-items:center;padding:24px 16px}
main{width:100%;max-width:440px;padding:26px 24px 30px;border:1px solid var(--line);border-radius:26px;text-align:center;background:linear-gradient(160deg,rgba(255,255,255,.07),rgba(255,255,255,.015));backdrop-filter:blur(22px);box-shadow:0 24px 70px rgba(0,0,0,.6)}
main .mark{margin:0 auto 16px}
h1{margin:0 0 20px;font-size:23px;line-height:1.28;font-weight:650}
p{margin:0 0 22px;color:var(--muted)}
.who{display:inline-block;margin:0 0 18px;padding:6px 14px;border:1px solid var(--line);border-radius:999px}
ul{margin:0 0 26px;padding:0;list-style:none;text-align:left}
li{position:relative;padding:9px 0 9px 32px;border-top:1px solid rgba(255,255,255,.06)}
li::before{content:"";position:absolute;left:4px;top:14px;width:12px;height:7px;border-left:2px solid var(--gold);border-bottom:2px solid var(--gold);transform:rotate(-45deg)}
.fine{margin:14px 0 0;font-size:13px}
form{margin:0}
.g{width:30px;height:30px;border-radius:50%;background:#fff;display:grid;place-items:center}
.rail{display:flex;gap:6px;margin:0 0 22px;padding:0;list-style:none;font-size:12px;color:var(--muted)}
.rail li{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px;padding:0;border:0}
.rail li::before{display:none}
.rail span{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;border:1px solid var(--line)}
.rail .now{color:var(--ink)}.rail .now span{background:var(--gold);color:#1d1503;border-color:var(--gold);box-shadow:0 0 18px rgba(245,196,81,.55)}
.rail .done span{color:var(--gold);border-color:var(--gold)}
.lead{margin:0 0 6px;text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.1em;color:var(--gold)}
`;

const ACCESS = [
  'See live API prices read from Anthropic, OpenAI, Google and OpenRouter, each with its source and time',
  'Price any turn for any model, cloud or local, to the nano-dollar',
  'Record your sessions’ turns and read back per-turn, per-session and per-day spend',
  'See cache hit rate and what caching saved',
];
const STEPS = ['Sign in', 'Allow', 'Connected'];
const rail = (step) =>
  `<ol class="rail" aria-label="Step ${step} of 3">${STEPS.map((n, i) => `<li class="${i + 1 < step ? 'done' : i + 1 === step ? 'now' : ''}"${i + 1 === step ? ' aria-current="step"' : ''}><span>${i + 1}</span>${n}</li>`).join('')}</ol>`;
const abilities = () => `<ul>${ACCESS.map((a) => `<li>${esc(a)}</li>`).join('')}</ul>`;

function authPage(title, body, step = 0, head = '') {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">${head}
<meta name="viewport" content="width=device-width, initial-scale=1"><title>${esc(title)}</title><style>${CSS}${AUTH_CSS}</style></head>
<body><main><div class="mark" aria-hidden="true">N//T</div>${step ? rail(step) : ''}${body}</main></body></html>`;
}

const GOOGLE_ICON =
  '<svg width="20" height="20" viewBox="0 0 48 48" aria-hidden="true"><path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z"/><path fill="#FF3D00" d="m6.3 14.7 6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/><path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-7.9l-6.5 5C9.5 39.6 16.2 44 24 44z"/><path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z"/></svg>';

export function loginPage({ app, googleHref }) {
  return authPage(
    'Sign in to NouGen//Trckr',
    `<h1>Log in to NouGen//Trckr to link with ${esc(app)}</h1>
<p class="lead">${esc(app)} will be able to</p>${abilities()}
${
      googleHref
        ? `<a class="btn primary" href="${esc(googleHref)}"><span class="g">${GOOGLE_ICON}</span><span>Continue with Google</span></a>
<p class="fine">No passwords or keys to paste. Only allowed accounts get in.</p>`
        : `<p class="fine">Google sign-in isn't switched on for this Trckr yet. The owner adds the Google client to the Worker once, then this button appears.</p>`
    }`,
    1,
  );
}

export function consentPage({ app, email, token, switchHref }) {
  return authPage(
    `Allow ${app}`,
    `<h1>${esc(app)} would like access to your NouGen//Trckr</h1>
<div class="who">${esc(email)}</div>${abilities()}
<form method="post" action="/oauth/consent"><input type="hidden" name="token" value="${esc(token)}">
<button class="btn primary" name="decision" value="allow">Allow</button>
<button class="btn" name="decision" value="deny">Cancel</button></form>
<p class="fine">Disconnect any time from ${esc(app)}.${switchHref ? ` <a href="${esc(switchHref)}">Use another account</a>` : ''}</p>`,
    2,
  );
}

export function connectedPage({ app, href }) {
  return authPage(
    `Connected to ${app}`,
    `<h1>You're connected</h1><p>NouGen//Trckr is linked with ${esc(app)}. Taking you back now.</p>
<a class="btn primary" href="${esc(href)}">Return to ${esc(app)}</a>`,
    3,
    `<meta http-equiv="refresh" content="2;url=${esc(href)}">`,
  );
}

export function deniedPage() {
  return authPage('Not allowed', `<h1>That account can't use this NouGen//Trckr</h1><p>Go back and sign in with an allowed Google account.</p>`);
}

// ---- the board ----------------------------------------------------------------------------

const BOARD_CSS = `
.wrap{max-width:1180px;margin:0 auto;padding:22px 16px 60px}
header.top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:26px}
.brand{font-size:20px;font-weight:750;letter-spacing:.01em}.brand b{color:var(--gold)}
.brand small{display:block;font-size:12px;font-weight:500;color:var(--muted);letter-spacing:.04em}
nav{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}
nav a,.pill{display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;border:1px solid var(--line);background:var(--glass);color:var(--ink);text-decoration:none;font-size:13px}
nav a.on{background:var(--gold);color:#1d1503;border-color:var(--gold);font-weight:650}
.live{width:8px;height:8px;border-radius:50%;background:var(--track);box-shadow:0 0 0 0 rgba(46,230,166,.6);animation:pulse 2s infinite}
@keyframes pulse{70%{box-shadow:0 0 0 8px rgba(46,230,166,0)}100%{box-shadow:0 0 0 0 rgba(46,230,166,0)}}
.trk{color:var(--track)}h2.trk{color:var(--track);display:flex;align-items:center;gap:10px}
.stat.trk{border-color:var(--trackline);background:linear-gradient(160deg,rgba(46,230,166,.08),rgba(0,0,0,.28) 60%)}.stat.trk strong{color:var(--track2)}
td.tk{color:var(--track2)}
.hero::after{content:"";position:absolute;right:-90px;top:-90px;width:260px;height:260px;border-radius:50%;background:radial-gradient(circle,rgba(46,230,166,.18),rgba(46,230,166,0) 65%);pointer-events:none}
@media (prefers-reduced-motion:reduce){.live{animation:none}}
.hero{padding:30px 26px;border:1px solid var(--line);border-radius:28px;margin-bottom:22px;background:linear-gradient(135deg,rgba(245,196,81,.1),rgba(255,255,255,.015) 55%);position:relative;overflow:hidden}
.hero h1{margin:0 0 6px;font-size:clamp(26px,4vw,40px);line-height:1.1;font-weight:760;letter-spacing:-.01em}
.hero h1 em{font-style:normal;background:linear-gradient(90deg,var(--gold2),var(--gold));-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{margin:0;color:var(--muted);max-width:640px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(138px,1fr));gap:12px;margin:22px 0 0}
.stat{padding:16px;border-radius:18px;border:1px solid rgba(255,255,255,.07);background:rgba(0,0,0,.28)}
.stat span{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted)}
.stat strong{display:block;font-size:26px;font-weight:720;margin-top:4px;font-variant-numeric:tabular-nums}
.stat small{color:var(--dim);font-size:12px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.14em;color:var(--gold);margin:34px 0 12px;font-weight:650}
.panel{border:1px solid var(--line);border-radius:22px;background:var(--glass);overflow:hidden}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);padding:11px 14px;border-bottom:1px solid var(--line);white-space:nowrap;background:rgba(0,0,0,.25)}
td{padding:10px 14px;border-bottom:1px solid rgba(255,255,255,.05);white-space:nowrap}
tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right}
td.model{font-weight:600}td.model small{display:block;font-weight:400;color:var(--dim);font-size:11.5px}
.tag{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;border:1px solid var(--line);color:var(--gold2);margin-left:6px}
.tag.local{color:var(--ok);border-color:rgba(123,224,166,.3)}.tag.warn{color:var(--warn);border-color:rgba(255,184,107,.35)}.tag.bad{color:var(--bad);border-color:rgba(255,122,122,.35)}
.sources{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.src{padding:16px;border-radius:18px;border:1px solid var(--line);background:var(--glass)}
.src h3{margin:0 0 6px;font-size:16px;display:flex;align-items:center;gap:8px}
.src .dot{width:9px;height:9px;border-radius:50%;background:var(--track);box-shadow:0 0 10px rgba(46,230,166,.6)}.src.off .dot{background:var(--bad)}
.src p{margin:2px 0;color:var(--muted);font-size:12.5px;overflow-wrap:anywhere}
.src code{font-size:11.5px;color:var(--dim)}
details.prov{border-top:1px solid var(--line)}
details.prov summary{cursor:pointer;padding:14px 16px;font-weight:650;list-style:none;display:flex;align-items:center;gap:10px}
details.prov summary::-webkit-details-marker{display:none}
details.prov summary::after{content:"+";margin-left:auto;color:var(--gold)}
details.prov[open] summary::after{content:"\\2212"}
.filter{display:flex;gap:10px;align-items:center;margin:0 0 12px}
.filter input{flex:1;min-height:44px;padding:0 16px;border-radius:14px;border:1px solid var(--line);background:rgba(0,0,0,.35);color:var(--ink);font:inherit}
.empty{padding:26px;text-align:center;color:var(--muted)}
.cta{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}.cta .btn{width:auto;min-height:44px;margin:0}
@media (max-width:560px){.hero{padding:22px 16px}.stat{padding:12px}.stat strong{font-size:21px}nav{margin-left:0}}
footer{margin-top:40px;color:var(--dim);font-size:12.5px;text-align:center}
.sample{margin:0 0 12px;padding:10px 14px;border-radius:12px;border:1px dashed rgba(255,184,107,.45);color:var(--warn);font-size:13px}
.steps{counter-reset:s;margin:0;padding:0;list-style:none}
.steps li{counter-increment:s;position:relative;padding:16px 16px 16px 58px;border-top:1px solid rgba(255,255,255,.06)}
.steps li:first-child{border-top:0}
.steps li::before{content:counter(s);position:absolute;left:16px;top:16px;width:28px;height:28px;border-radius:50%;display:grid;place-items:center;background:var(--gold);color:#1d1503;font-weight:750;font-size:13px}
.steps h3{margin:0 0 6px;font-size:15px}
pre{margin:8px 0 0;padding:12px 14px;border-radius:12px;background:rgba(0,0,0,.45);border:1px solid rgba(255,255,255,.06);overflow-x:auto;font-size:12.5px;color:var(--gold2)}
.url{font-size:clamp(18px,3vw,26px);font-weight:700;color:var(--gold2);word-break:break-all}
`;

function boardPage(title, body, { refresh } = {}) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">${refresh ? `<meta http-equiv="refresh" content="${refresh}">` : ''}
<title>${esc(title)}</title><meta name="description" content="Per-turn token and cost tracking with live provider price pools.">
<style>${CSS}${BOARD_CSS}</style></head><body><div class="wrap">${body}
<footer>NouGen//Trckr by Who Visions LLC. Prices are read from each provider's own page and shown with that page and the time it was read. A price no page publishes shows as n/a, never estimated.</footer>
</div><script src="/assets/trckr.js" defer></script></body></html>`;
}

function topbar(active, user) {
  const link = (href, label) => `<a href="${href}"${active === href ? ' class="on" aria-current="page"' : ''}>${label}</a>`;
  return `<header class="top"><div class="mark" aria-hidden="true">N//T</div>
<div class="brand">NouGen<b>//</b>Trckr<small>EVERY TURN, PRICED FROM THE SOURCE</small></div>
<nav>${link('/', 'Board')}${link('/connect', 'Connect')}${user ? `<span class="pill"><span class="live" aria-hidden="true"></span>${esc(user)}</span><a href="/logout">Sign out</a>` : '<a href="/login">Sign in</a>'}</nav></header>`;
}

function bandLabel(b) {
  const parts = [];
  if (b.min_prompt_tokens !== null) parts.push(`prompts over ${tok(b.min_prompt_tokens - 1)}`);
  if (b.max_prompt_tokens !== null) parts.push(`prompts up to ${tok(b.max_prompt_tokens)}`);
  if (b.valid_from) parts.push(`from ${b.valid_from}`);
  if (b.valid_until) parts.push(`until ${b.valid_until}`);
  return parts.join(', ');
}

function priceRows(models, today) {
  const rows = [];
  for (const m of models) {
    const current = m.bands.filter((b) => !b.valid_until || b.valid_until > today);
    current.forEach((b, i) => {
      const label = bandLabel(b);
      rows.push(`<tr data-q="${esc(`${m.model} ${m.display} ${(m.aliases || []).join(' ')}`.toLowerCase())}">
<td class="model">${i === 0 ? esc(m.display) : ''}${i === 0 && m.notes ? `<span class="tag">${esc(m.notes)}</span>` : ''}${b.quarantined ? '<span class="tag bad">held: big move</span>' : ''}<small>${i === 0 ? esc(m.model) : ''}${label ? `${i === 0 ? ' · ' : ''}${esc(label)}` : ''}</small></td>
<td class="n">${rate(b.input)}</td><td class="n">${rate(b.output)}</td><td class="n">${rate(b.cache_read)}</td><td class="n">${rate(b.cache_write_5m)}</td><td class="n">${rate(b.cache_write_1h)}</td></tr>`);
    });
  }
  return rows.join('');
}

export function boardView({ base, user, pools, usage, turns, days, sample }) {
  const today = new Date().toISOString().slice(0, 10);
  const live = pools.filter((p) => p.snapshot);
  const modelCount = live.reduce((n, p) => n + p.snapshot.models.length, 0);
  const t = usage?.today;
  const stats = user
    ? `<div class="stats">
<div class="stat"><span>Spend today</span><strong>${money(t?.cost_usd ?? 0)}</strong><small>Eastern day</small></div>
<div class="stat trk"><span>Turns today</span><strong>${tok(t?.turns ?? 0)}</strong><small>${tok(t?.local ?? 0)} local, ${tok(t?.unpriced ?? 0)} unpriced</small></div>
<div class="stat trk"><span>Tokens today</span><strong>${tok(t?.total_tokens ?? 0)}</strong><small>${tok(t?.output ?? 0)} out</small></div>
<div class="stat trk"><span>Cache hit rate</span><strong>${pct(t?.cache_hit_rate)}</strong><small>target 95%</small></div>
<div class="stat"><span>Cache saved</span><strong>${money(t?.cache_savings_usd ?? 0)}</strong><small>vs. fresh input</small></div>
</div>`
    : `<div class="stats">
<div class="stat trk"><span>Providers read</span><strong>${live.length} of ${pools.length}</strong><small>from their own pages</small></div>
<div class="stat"><span>Models priced</span><strong>${tok(modelCount)}</strong><small>with cache tiers</small></div>
<div class="stat"><span>Last read</span><strong style="font-size:18px">${esc(live.length ? clock(live.map((p) => p.snapshot.last_seen_at).sort().pop()) : 'not yet')}</strong><small>Eastern</small></div>
<div class="stat"><span>Money math</span><strong>nano-$</strong><small>exact, no float</small></div>
</div><div class="cta"><a class="btn primary" href="/connect">Connect your sessions</a><a class="btn" href="/login">Sign in for your turns</a></div>`;

  const feed = user
    ? `<h2 class="trk"><span class="live" aria-hidden="true"></span>Live turns</h2><div class="panel scroll">${
        turns.length
          ? `<table><thead><tr><th>Time</th><th>Lane</th><th>Model</th><th class="n">In</th><th class="n">Cache read</th><th class="n">Cache write</th><th class="n">Out</th><th class="n">Hit</th><th class="n">Cost</th><th>Priced from</th></tr></thead><tbody>${turns
              .map((r) => {
                const read = r.input_tokens + r.cache_read_tokens + r.cache_write_5m_tokens + r.cache_write_1h_tokens;
                const tag = r.status === 'local' ? '<span class="tag local">self-hosted</span>' : r.status === 'priced' ? '' : `<span class="tag ${r.status === 'partial' ? 'warn' : 'bad'}">${esc(r.status)}</span>`;
                return `<tr><td class="n">${esc(clock(r.at))}</td><td>${esc(r.lane)}</td><td class="model">${esc(r.model)}${tag}</td>
<td class="n tk">${tok(r.input_tokens)}</td><td class="n tk">${tok(r.cache_read_tokens)}</td><td class="n tk">${tok(r.cache_write_5m_tokens + r.cache_write_1h_tokens)}</td><td class="n tk">${tok(r.output_tokens)}</td>
<td class="n">${read ? pct(r.cache_read_tokens / read) : 'n/a'}</td><td class="n"><strong>${money(r.cost_usd)}</strong></td>
<td>${r.source_url ? `<a href="${esc(r.source_url)}" rel="noopener">${esc(new URL(r.source_url).hostname)}</a> <span class="na">${esc(clock(r.price_fetched_at))}</span>` : `<span class="na">${esc(r.reason || (r.status === 'local' ? 'no API bill' : ''))}</span>`}</td></tr>`;
              })
              .join('')}</tbody></table>`
          : `<div class="empty">No turns yet. Add the hook from <a href="/connect">Connect</a> and every model request lands here as it happens.</div>`
      }</div>
${days?.length ? `<h2>By day (Eastern)</h2><div class="panel scroll"><table><thead><tr><th>Day</th><th class="n">Turns</th><th class="n">Tokens</th><th class="n">Hit rate</th><th class="n">Saved</th><th class="n">Spend</th></tr></thead><tbody>${days
            .map((d) => `<tr><td>${esc(d.key)}</td><td class="n">${tok(d.turns)}</td><td class="n">${tok(d.total_tokens)}</td><td class="n">${pct(d.cache_hit_rate)}</td><td class="n">${money(d.cache_savings_usd)}</td><td class="n"><strong>${money(d.cost_usd)}</strong></td></tr>`)
            .join('')}</tbody></table></div>` : ''}`
    : '';

  const sources = `<h2 class="trk"><span class="live" aria-hidden="true"></span>Price pools</h2><div class="sources">${pools
    .map((p) =>
      p.snapshot
        ? `<div class="src"><h3><span class="dot" aria-hidden="true"></span>${esc(p.label)}</h3>
<p>${p.snapshot.models.length} models · read ${esc(eastern(p.snapshot.last_seen_at))}</p>
<p>Unchanged since ${esc(eastern(p.snapshot.fetched_at))}</p>
<p><a href="${esc(p.snapshot.page_url)}" rel="noopener">${esc(p.snapshot.source_url)}</a></p>
<p><code>snapshot ${esc(p.snapshot.snapshot_id)} · sha256 ${esc(p.snapshot.body_sha256.slice(0, 12))}</code></p></div>`
        : `<div class="src off"><h3><span class="dot" aria-hidden="true"></span>${esc(p.label)}</h3>
<p>Not read yet${p.last ? ` (last try ${esc(eastern(p.last.at))})` : ''}. Its prices show as unknown until the page answers.</p>
<p><a href="${esc(p.page)}" rel="noopener">${esc(p.page)}</a></p></div>`,
    )
    .join('')}</div>`;

  const board = `<h2>Live price board <span class="na" style="text-transform:none;letter-spacing:0">USD per 1M tokens</span></h2>
<div class="filter"><input id="q" type="search" placeholder="Filter models: opus, gpt-5, gemini, free" aria-label="Filter models"></div>
<div class="panel">${live
    .map(
      (p, i) => `<details class="prov"${i === 0 ? ' open' : ''}><summary>${esc(p.label)} <span class="tag">${p.snapshot.models.length}</span></summary><div class="scroll"><table>
<thead><tr><th>Model</th><th class="n">Input</th><th class="n">Output</th><th class="n">Cache read</th><th class="n">Cache write 5m</th><th class="n">Cache write 1h</th></tr></thead>
<tbody>${priceRows(p.snapshot.models, today)}</tbody></table></div></details>`,
    )
    .join('') || '<div class="empty">No provider page has been read yet.</div>'}</div>`;

  return boardPage(
    'NouGen//Trckr',
    `${topbar('/', user)}${sample ? `<div class="sample">${esc(sample)}</div>` : ''}
<section class="hero"><h1>Every turn, <em>priced from the source.</em></h1>
<p>Live token and cost tracking for every model your sessions run: Claude, GPT, Gemini, OpenRouter lanes and local Ollama. Prices come straight off each provider's own page, with the link and the time it was read.</p>${stats}</section>
${feed}${sources}${board}`,
    { refresh: user ? 30 : 0 },
  );
}

export function connectView({ base, user }) {
  const mcp = `${base}/mcp`;
  const steps = [
    ['Claude (claude.ai, Desktop, mobile)', `Settings, Connectors, Add custom connector. Paste the URL below, then sign in with Google.`, mcp],
    ['Claude Code', 'One line in a terminal:', `claude mcp add --transport http nougen-trckr ${mcp}`],
    ['ChatGPT', 'Settings, Apps and Connectors, Advanced, Developer mode on, Create. Paste the URL and choose OAuth.', mcp],
    ['Codex CLI', 'Add it, then sign in:', `codex mcp add nougen-trckr --url ${mcp}\ncodex mcp login nougen-trckr`],
    ['Gemini CLI', 'In ~/.gemini/settings.json:', JSON.stringify({ mcpServers: { 'nougen-trckr': { httpUrl: mcp } } }, null, 2)],
    ['Cursor', 'In ~/.cursor/mcp.json:', JSON.stringify({ mcpServers: { 'nougen-trckr': { url: mcp } } }, null, 2)],
    [
      'Every turn, automatically (Claude Code hook)',
      'Mint a key with the trckr_ingest_key tool, store it in Keymaker as TRCKR_INGEST_KEY, then add the Stop hook. It reads each new turn from the session transcript and posts its usage. Ollama and other local runs post to the same endpoint with provider "ollama".',
      `# ~/.claude/settings.json\n${JSON.stringify({ hooks: { Stop: [{ hooks: [{ type: 'command', command: 'python3 ~/.claude/hooks/trckr_stop_hook.py' }] }] } }, null, 2)}\n\n# any runtime, one turn:\ncurl -s ${base}/v1/turns -H "authorization: Bearer $TRCKR_INGEST_KEY" \\\n  -H "content-type: application/json" \\\n  -d '{"lane":"blade-ollama","request_id":"r1","provider":"ollama","model":"qwen3:32b","input_tokens":4000,"output_tokens":900}'`,
    ],
  ];
  return boardPage(
    'Connect NouGen//Trckr',
    `${topbar('/connect', user)}
<section class="hero"><h1>Connect <em>NouGen//Trckr</em></h1><p>One URL for every client. Sign in with Google; only allowed accounts get in.</p>
<p class="url" style="margin-top:16px">${esc(mcp)}</p></section>
<h2>Set up</h2><div class="panel"><ol class="steps">${steps
      .map(([h, p, code]) => `<li><h3>${esc(h)}</h3><p style="margin:0;color:var(--muted)">${esc(p)}</p><pre>${esc(code)}</pre></li>`)
      .join('')}</ol></div>
<h2>Tools</h2><div class="panel scroll"><table><thead><tr><th>Tool</th><th>What it does</th></tr></thead><tbody>${TOOL_SUMMARY.map(([n, d]) => `<tr><td class="model">${esc(n)}</td><td style="white-space:normal">${esc(d)}</td></tr>`).join('')}</tbody></table></div>`,
  );
}

export const TOOL_SUMMARY = [
  ['trckr_prices', 'Live price board for a provider or a search, each price with its source page and read time'],
  ['trckr_price', 'The exact band that applies to one model at a prompt size and date'],
  ['trckr_turn_cost', 'Price one turn from its token counts, without saving it'],
  ['trckr_record_turn', 'Save one turn (idempotent on lane + request id) and return its cost'],
  ['trckr_session', 'Per-turn ledger and totals for one session'],
  ['trckr_usage', 'Spend, tokens, cache hit rate and savings grouped by model, provider, lane, session, client or day'],
  ['trckr_pools', 'Health of every price pool: last read, source, snapshot hash, failures'],
  ['trckr_refresh', 'Re-read the provider pages now (unchanged pages write nothing)'],
  ['trckr_ingest_key', 'Mint a hook key for automatic per-turn capture (shown once, stored hashed)'],
];
