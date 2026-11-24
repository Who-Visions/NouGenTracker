-- Per-session dollar caps that Trckr enforces. A session registers a cap and a stop rule at start;
-- every check prices its real token usage at list price. At warn_pct the spending session itself is
-- told to wrap up; at the cap it is told to stop and an 'over' event goes to the coordinator's alert hook.
CREATE TABLE IF NOT EXISTS budgets (
  owner       TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  label       TEXT,
  cap_nano    INTEGER NOT NULL,          -- nano-USD
  warn_pct    INTEGER NOT NULL DEFAULT 80,
  stop_rule   TEXT,
  state       TEXT NOT NULL DEFAULT 'ok', -- ok | warn | over | closed
  spent_nano  INTEGER NOT NULL DEFAULT 0,
  unpriced    INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  checked_at  TEXT,
  warned_at   TEXT,
  over_at     TEXT,
  PRIMARY KEY (owner, session_id)
);
CREATE INDEX IF NOT EXISTS budgets_state ON budgets (owner, state);

-- Threshold crossings. Only 'over' events are pushed to TRCKR_ALERT_URL; 'warn' goes to the session itself.
CREATE TABLE IF NOT EXISTS budget_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  owner       TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  at          TEXT NOT NULL,
  kind        TEXT NOT NULL,              -- set | warn | over | closed
  spent_nano  INTEGER NOT NULL,
  cap_nano    INTEGER NOT NULL,
  notified_at TEXT
);
CREATE INDEX IF NOT EXISTS budget_events_pending ON budget_events (kind, notified_at);
