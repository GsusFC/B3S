-- Durable control-plane records for the B3S Scanner API.
--
-- The immutable report remains the source of truth for completed scans. This
-- table persists the asynchronous job envelope and idempotency reservation so
-- clients receive an honest terminal state after a process restart instead of
-- an unknown scan or an accidental duplicate.

CREATE TABLE IF NOT EXISTS b3s_scanner_jobs (
    scan_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT '',
    request_json TEXT NOT NULL DEFAULT '{}',
    status_json TEXT NOT NULL DEFAULT '{}',
    client_id TEXT NOT NULL DEFAULT '',
    idempotency_key_hash TEXT UNIQUE,
    request_fingerprint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_b3s_scanner_jobs_state_updated
    ON b3s_scanner_jobs(state, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_b3s_scanner_jobs_created
    ON b3s_scanner_jobs(created_at DESC);
