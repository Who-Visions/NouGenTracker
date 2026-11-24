#!/usr/bin/env node
// Cloud deploy for NouGen//Trckr, run by Cloudflare Workers Builds as the deploy command.
// Workers Builds injects CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID, so nothing here needs a key.
//   1. find the production D1 database (nougen-trckr), creating it when missing
//   2. check wrangler.jsonc pins that database id
//   3. apply migrations to the remote database
//   4. deploy the top level (production) to trckr.nougenai.com
// It prints no secrets. Run it by hand the same way: `node deploy/cf_build.mjs` with those two vars set.
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const DB_NAME = process.env.TRCKR_D1_NAME || 'nougen-trckr';
const WRANGLER = process.env.TRCKR_WRANGLER || 'wrangler@4';

function wrangler(args, { capture = false } = {}) {
  console.log(`> wrangler ${args.join(' ')}`);
  const out = execFileSync('npx', ['--yes', WRANGLER, ...args], {
    cwd: root,
    env: process.env,
    stdio: capture ? ['ignore', 'pipe', 'inherit'] : 'inherit',
    encoding: 'utf8',
  });
  return capture ? out : '';
}

function findDb() {
  const raw = wrangler(['d1', 'list', '--json'], { capture: true });
  const list = JSON.parse(raw.slice(raw.indexOf('[')));
  return list.find((d) => d.name === DB_NAME);
}

if (!process.env.CLOUDFLARE_API_TOKEN) {
  console.error('CLOUDFLARE_API_TOKEN is not set. Workers Builds sets it; by hand, export it first.');
  process.exit(1);
}

let db = findDb();
if (!db) {
  wrangler(['d1', 'create', DB_NAME]);
  db = findDb();
}
if (!db?.uuid) {
  console.error(`D1 database ${DB_NAME} could not be found or created.`);
  process.exit(1);
}
console.log(`D1 ${DB_NAME}: ${db.uuid}`);

const config = readFileSync(join(root, 'wrangler.jsonc'), 'utf8');
if (!config.includes(`"database_id": "${db.uuid}"`)) {
  console.error(`wrangler.jsonc does not pin ${DB_NAME} (${db.uuid}); update its top-level database_id and push.`);
  process.exit(1);
}
wrangler(['d1', 'migrations', 'apply', DB_NAME, '--remote']);
wrangler(['deploy']);
console.log('Trckr deployed: https://trckr.nougenai.com');
