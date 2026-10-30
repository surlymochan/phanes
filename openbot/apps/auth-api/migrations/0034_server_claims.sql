-- One-time claims that an operator makes to sign a new server in for a user. A server redeems a
-- claim once for a session of that user. Only the hash of a claim is stored.
-- A new table only: the deployed Worker does not read it.
CREATE TABLE server_claims (
  claim_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  redeemed_at INTEGER,
  session_id TEXT
);

CREATE INDEX server_claims_expires_at ON server_claims(expires_at);
