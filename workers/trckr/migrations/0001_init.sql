-- NouGen//Trckr D1 schema. Apply with: wrangler d1 migrations apply nougen-trckr --remote

-- One row per distinct page body a provider served. Same bytes + same parser = same id.
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id   TEXT PRIMARY KEY,
  source        TEXT NOT NULL,
  source_url    TEXT NOT NULL,
  page_url      TEXT NOT NULL,
  fetched_at    TEXT NOT NULL,   -- first read of these bytes (ISO, UTC)
  last_seen_at  TEXT NOT NULL,   -- latest read that returned the same bytes
  http_status   INTEGER NOT NULL,
  body_sha256   TEXT NOT NULL,
  parser        TEXT NOT NULL,
  models_json   TEXT NOT NULL,
  rejected_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_source_time ON snapshots (source, fetched_at);

-- Every pool run, kept so a gap in the board is explainable.
CREATE TABLE IF NOT EXISTS fetch_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL,
  at          TEXT NOT NULL,
  ok          INTEGER NOT NULL,
  snapshot_id TEXT,
  attempts_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fetch_log_source_time ON fetch_log (source, at);

-- One row per model request. id = sha256(owner|lane|request_id) so a re-sent turn updates in place.
CREATE TABLE IF NOT EXISTS turns (
  id            TEXT PRIMARY KEY,
  owner         TEXT NOT NULL,
  lane          TEXT NOT NULL,
  session_id    TEXT,
  request_id    TEXT NOT NULL,
  client        TEXT,
  provider      TEXT,
  model         TEXT NOT NULL,
  at            TEXT NOT NULL,
  input_tokens          INTEGER NOT NULL DEFAULT 0,
  output_tokens         INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
  cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens      INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL,   -- priced | partial | local | unpriced | quarantined
  cost_nano     INTEGER,         -- nano-USD, exact; NULL when unpriced
  savings_nano  INTEGER,
  snapshot_id   TEXT,
  source_url    TEXT,
  price_fetched_at TEXT,
  band_json     TEXT,
  reason        TEXT,
  received_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_owner_time ON turns (owner, at);
CREATE INDEX IF NOT EXISTS turns_owner_session ON turns (owner, session_id, at);

-- Hook keys: only the sha256 of the key is stored.
CREATE TABLE IF NOT EXISTS ingest_keys (
  key_hash     TEXT PRIMARY KEY,
  owner        TEXT NOT NULL,
  label        TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  last_used_at TEXT
);
