-- Worker-held settings. The OAuth signing secret is minted here on first boot when no
-- TRCKR_SIGNING_SECRET is set, so a cloud deploy needs no secret pasted by hand.
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  created_at TEXT NOT NULL
);
