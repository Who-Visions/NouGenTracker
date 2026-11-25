-- When each snapshot became the current one for its source. Page content stays deduplicated in
-- snapshots (same bytes, same id); this timeline records every switch, so a provider that goes
-- A -> B -> A resolves to A again, now and for any later time, while history before B stays A.
CREATE TABLE IF NOT EXISTS snapshot_reads (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  from_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshot_reads_source_time ON snapshot_reads (source, from_at);
INSERT INTO snapshot_reads (source, snapshot_id, from_at)
  SELECT source, snapshot_id, fetched_at FROM snapshots
   WHERE NOT EXISTS (SELECT 1 FROM snapshot_reads r WHERE r.snapshot_id = snapshots.snapshot_id);
