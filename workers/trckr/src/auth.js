// OAuth 2.1 for the connector, same shape as shards.nougenai.com and eye.nougenai.com: open
// dynamic client registration, Google sign-in, an allowlist of accounts, PKCE S256. Stateless:
// clients, codes and tokens are HS256 JWTs signed with TRCKR_SIGNING_SECRET, so nothing to store.
//
// Google sign-in rides the fleet's shared Google OAuth client. Its one registered redirect is Eye's
// callback; Trckr sends Google that callback with state prefixed "trckr~", and Eye relays the browser
// to https://trckr.nougenai.com/oauth/google/callback unchanged (Eye's CALLBACK_RELAYS).
import { connectedPage, consentPage, deniedPage, loginPage } from './pages.js';

const GOOGLE_AUTH = 'https://accounts.google.com/o/oauth2/v2/auth';
const GOOGLE_TOKEN = 'https://oauth2.googleapis.com/token';
export const RELAY_TAG = 'trckr~';
const SESSION_COOKIE = 'trckr_session';

const enc = new TextEncoder();
const b64u = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const b64uJson = (obj) => b64u(enc.encode(JSON.stringify(obj)));
const unb64u = (s) => {
  const bin = atob(String(s).replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((String(s).length + 3) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
};
const TTL = { m: 60, h: 3600, d: 86400, y: 31536000 };
const seconds = (ttl) => Number(ttl.slice(0, -1)) * TTL[ttl.slice(-1)];

export async function sha256b64u(text) {
  return b64u(await crypto.subtle.digest('SHA-256', enc.encode(String(text))));
}

export function redirectAllowed(uri) {
  try {
    const { protocol, hostname } = new URL(uri);
    return protocol === 'https:' || (protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(hostname));
  } catch {
    return false;
  }
}

export function safeNext(next) {
  const value = String(next || '/');
  if (!value.startsWith('/') || value.startsWith('//') || value.startsWith('/\\') || /[\u0000-\u001f]/.test(value)) return '/';
  try {
    const url = new URL(value, 'https://trckr.invalid');
    return url.origin === 'https://trckr.invalid' ? `${url.pathname}${url.search}${url.hash}` : '/';
  } catch {
    return '/';
  }
}

const json = (status, body, headers = {}) =>
  new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', 'cache-control': 'no-store', 'access-control-allow-origin': '*', ...headers },
  });
const html = (status, body, headers = {}) =>
  new Response(body, {
    status,
    headers: {
      'content-type': 'text/html; charset=utf-8',
      'cache-control': 'no-store',
      'content-security-policy': "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self' https:; frame-ancestors 'none'",
      ...headers,
    },
  });

async function readForm(req) {
  const raw = await req.text();
  if ((req.headers.get('content-type') || '').includes('json')) {
    try {
      return JSON.parse(raw);
    } catch {
      return {};
    }
  }
  return Object.fromEntries(new URLSearchParams(raw));
}

const cookieFrom = (req) =>
  (req.headers.get('cookie') || '')
    .split(';')
    .map((c) => c.trim().split('='))
    .find(([k]) => k === SESSION_COOKIE)?.[1] || '';

export function createAuth({ base, googleId, googleSecret, allowedEmails, signingSecret, googleRedirect, googleExchange, now = () => Date.now() }) {
  if (!signingSecret) throw new Error('TRCKR_SIGNING_SECRET is not set');
  const allowed = new Set(String(allowedEmails || '').split(',').map((e) => e.trim().toLowerCase()).filter(Boolean));
  const isAllowed = (email) => typeof email === 'string' && allowed.has(email.toLowerCase());
  const redirect = googleRedirect || `${base}/oauth/google/callback`;
  const relayed = !redirect.startsWith(`${base}/`);
  let keyP;
  const key = () =>
    (keyP ||= crypto.subtle
      .digest('SHA-256', enc.encode(`nougen-trckr-oauth:${signingSecret}`))
      .then((raw) => crypto.subtle.importKey('raw', raw, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify'])));

  async function sign(kind, claims, ttl) {
    const iat = Math.floor(now() / 1000);
    const head = b64uJson({ alg: 'HS256', typ: 'JWT' });
    const body = b64uJson({ ...claims, kind, iss: base, iat, exp: iat + seconds(ttl) });
    const sig = await crypto.subtle.sign('HMAC', await key(), enc.encode(`${head}.${body}`));
    return `${head}.${body}.${b64u(sig)}`;
  }
  async function read(kind, jwt) {
    const [head, body, sig] = String(jwt || '').split('.');
    if (!head || !body || !sig) throw new Error('malformed');
    const ok = await crypto.subtle.verify('HMAC', await key(), unb64u(sig), enc.encode(`${head}.${body}`));
    if (!ok) throw new Error('bad signature');
    const h = JSON.parse(new TextDecoder().decode(unb64u(head)));
    if (h.alg !== 'HS256') throw new Error('bad alg');
    const p = JSON.parse(new TextDecoder().decode(unb64u(body)));
    if (p.iss !== base || p.kind !== kind || !(p.exp > now() / 1000)) throw new Error('expired or wrong kind');
    return p;
  }

  const exchange =
    googleExchange ||
    (async (code) => {
      const r = await fetch(GOOGLE_TOKEN, {
        method: 'POST',
        body: new URLSearchParams({ code, client_id: googleId, client_secret: googleSecret, redirect_uri: redirect, grant_type: 'authorization_code' }),
      });
      if (!r.ok) throw new Error(`Google token exchange returned ${r.status}`);
      const idt = (await r.json()).id_token;
      return JSON.parse(new TextDecoder().decode(unb64u(String(idt).split('.')[1])));
    });

  // Until the Google client is set on the Worker, sign-in pages say so instead of sending people to a broken Google screen.
  const googleReady = !!(googleId && (googleSecret || googleExchange));
  const googleUrl = (state) => {
    const g = new URL(GOOGLE_AUTH);
    g.search = new URLSearchParams({
      client_id: googleId || '',
      redirect_uri: redirect,
      response_type: 'code',
      scope: 'openid email',
      state: relayed ? `${RELAY_TAG}${state}` : state,
      prompt: 'select_account',
    });
    return g.href;
  };

  const sessionCookie = async (email) =>
    `${SESSION_COOKIE}=${await sign('session', { email }, '30d')}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000${base.startsWith('https:') ? '; Secure' : ''}`;

  const tokens = async (email) => ({
    access_token: await sign('access', { email }, '1h'),
    refresh_token: await sign('refresh', { email }, '30d'),
    token_type: 'Bearer',
    expires_in: 3600,
  });

  const metadata = {
    '/.well-known/oauth-protected-resource': { resource: base, authorization_servers: [base], bearer_methods_supported: ['header'], resource_name: 'NouGen//Trckr' },
    '/.well-known/oauth-protected-resource/mcp': { resource: `${base}/mcp`, authorization_servers: [base], bearer_methods_supported: ['header'], resource_name: 'NouGen//Trckr' },
    '/.well-known/oauth-authorization-server': {
      issuer: base,
      authorization_endpoint: `${base}/authorize`,
      token_endpoint: `${base}/token`,
      registration_endpoint: `${base}/register`,
      response_types_supported: ['code'],
      grant_types_supported: ['authorization_code', 'refresh_token'],
      code_challenge_methods_supported: ['S256'],
      token_endpoint_auth_methods_supported: ['none'],
      scopes_supported: ['trckr'],
    },
  };
  metadata['/.well-known/openid-configuration'] = metadata['/.well-known/oauth-authorization-server'];

  async function handle(req) {
    const url = new URL(req.url);
    const exact = url.pathname.replace(/\/$/, '') || '/';
    const meta = metadata[exact] || metadata[exact.replace(/\/mcp$/, '')];
    if (meta) return json(200, meta);

    if (url.pathname === '/register' && req.method === 'POST') {
      const body = await readForm(req);
      const uris = Array.isArray(body.redirect_uris) ? body.redirect_uris : [];
      if (!uris.length || !uris.every(redirectAllowed)) return json(400, { error: 'invalid_redirect_uri' });
      return json(201, {
        client_id: await sign('client', { uris, name: String(body.client_name || '').slice(0, 60) }, '10y'),
        redirect_uris: uris,
        client_name: body.client_name || 'mcp-client',
        token_endpoint_auth_method: 'none',
        grant_types: ['authorization_code', 'refresh_token'],
        response_types: ['code'],
      });
    }

    if (url.pathname === '/authorize') {
      const p = url.searchParams;
      let client;
      try {
        client = await read('client', p.get('client_id'));
      } catch {
        return json(400, { error: 'invalid_client' });
      }
      if (!client.uris.includes(p.get('redirect_uri'))) return json(400, { error: 'invalid_redirect_uri' });
      if (!p.get('code_challenge') || p.get('code_challenge_method') !== 'S256') return json(400, { error: 'invalid_request' });
      const app = client.name || new URL(p.get('redirect_uri')).hostname;
      const flow = { redirect: p.get('redirect_uri'), state: p.get('state') || '', challenge: p.get('code_challenge'), app };
      const google = googleUrl(await sign('state', flow, '10m'));
      let known = null;
      try {
        known = (await read('session', cookieFrom(req))).email;
      } catch {}
      if (isAllowed(known)) {
        return html(200, consentPage({ app, email: known, token: await sign('consent', { ...flow, email: known }, '10m'), switchHref: google }));
      }
      return html(googleReady ? 200 : 503, loginPage({ app, googleHref: googleReady ? google : null }));
    }

    if (url.pathname === '/oauth/google/callback') {
      const given = url.searchParams.get('state') || '';
      const raw = given.startsWith(RELAY_TAG) ? given.slice(RELAY_TAG.length) : given;
      let flow;
      let web;
      try {
        flow = await read('state', raw);
      } catch {
        try {
          web = await read('web', raw);
        } catch {
          return json(400, { error: 'invalid_state' });
        }
      }
      let identity = {};
      try {
        identity = await exchange(url.searchParams.get('code'));
      } catch {}
      if (!identity.email_verified || !isAllowed(identity.email)) return html(403, deniedPage());
      const cookie = await sessionCookie(identity.email);
      if (web) return new Response(null, { status: 302, headers: { location: safeNext(web.next), 'cache-control': 'no-store', 'set-cookie': cookie } });
      const token = await sign('consent', { app: flow.app, email: identity.email, challenge: flow.challenge, redirect: flow.redirect, state: flow.state }, '10m');
      return html(200, consentPage({ app: flow.app || 'This app', email: identity.email, token }), { 'set-cookie': cookie });
    }

    if (url.pathname === '/oauth/consent' && req.method === 'POST') {
      const body = await readForm(req);
      let flow;
      try {
        flow = await read('consent', body.token);
      } catch {
        return json(400, { error: 'invalid_request' });
      }
      const back = new URL(flow.redirect);
      if (flow.state) back.searchParams.set('state', flow.state);
      if (body.decision === 'allow') {
        back.searchParams.set('code', await sign('code', { email: flow.email, challenge: flow.challenge, redirect: flow.redirect }, '5m'));
        return html(200, connectedPage({ app: flow.app || 'the app', href: back.href }));
      }
      back.searchParams.set('error', 'access_denied');
      return new Response(null, { status: 302, headers: { location: back.href } });
    }

    if (url.pathname === '/token' && req.method === 'POST') {
      const body = await readForm(req);
      try {
        if (body.grant_type === 'refresh_token') {
          const { email } = await read('refresh', body.refresh_token);
          if (!isAllowed(email)) throw new Error('not allowed');
          return json(200, await tokens(email));
        }
        const code = await read('code', body.code);
        if ((await sha256b64u(body.code_verifier)) !== code.challenge || body.redirect_uri !== code.redirect) throw new Error('bad proof');
        return json(200, await tokens(code.email));
      } catch {
        return json(400, { error: 'invalid_grant' });
      }
    }

    if (url.pathname === '/login') {
      if (!googleReady) return html(503, loginPage({ app: 'the board', googleHref: null }));
      return new Response(null, { status: 302, headers: { location: googleUrl(await sign('web', { next: safeNext(url.searchParams.get('next')) }, '10m')), 'cache-control': 'no-store' } });
    }
    if (url.pathname === '/logout') {
      return new Response(null, { status: 302, headers: { location: '/', 'set-cookie': `${SESSION_COOKIE}=; Path=/; Max-Age=0` } });
    }
    return null;
  }

  // The signed-in allowed account for a bearer token, or null.
  async function verify(req) {
    const [scheme, jwt] = (req.headers.get('authorization') || '').split(' ');
    if (scheme !== 'Bearer' || !jwt) return null;
    try {
      const { email } = await read('access', jwt);
      return isAllowed(email) ? email.toLowerCase() : null;
    } catch {
      return null;
    }
  }

  async function webSession(req) {
    try {
      const { email } = await read('session', cookieFrom(req));
      return isAllowed(email) ? email.toLowerCase() : null;
    } catch {
      return null;
    }
  }

  const challengeFor = (pathname = '/') => `Bearer resource_metadata="${base}/.well-known/oauth-protected-resource${pathname.startsWith('/mcp') ? '/mcp' : ''}"`;
  return { handle, verify, webSession, challengeFor, isAllowed, sign, read, owners: [...allowed] };
}
