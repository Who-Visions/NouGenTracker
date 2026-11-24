// Minimal D1 API over node:sqlite, so tests and the local dev server run the Worker's own SQL.
import { readFileSync, readdirSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

export function createD1(path = ':memory:') {
  const db = new DatabaseSync(path);
  const dir = new URL('../migrations/', import.meta.url);
  for (const f of readdirSync(dir).filter((n) => n.endsWith('.sql')).sort()) db.exec(readFileSync(new URL(f, dir), 'utf8'));
  const statement = (sql, args = []) => ({
    bind: (...a) => statement(sql, a),
    async all() {
      return { results: db.prepare(sql).all(...args).map((r) => ({ ...r })), success: true };
    },
    async first() {
      const r = db.prepare(sql).get(...args);
      return r ? { ...r } : null;
    },
    async run() {
      const r = db.prepare(sql).run(...args);
      return { success: true, meta: { changes: r.changes } };
    },
  });
  return { prepare: (sql) => statement(sql), batch: async (stmts) => Promise.all(stmts.map((s) => s.run())), _db: db };
}
