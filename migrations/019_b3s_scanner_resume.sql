-- Exact-resume action reservations for the local Scanner API control plane.

CREATE TABLE IF NOT EXISTS b3s_scanner_resume_actions (
    action_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    idempotency_key_hash TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('accepted', 'running', 'completed', 'failed', 'interrupted')),
    request_json TEXT NOT NULL,
    status_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_b3s_scanner_resume_actions_client_key
    ON b3s_scanner_resume_actions(client_id, idempotency_key_hash);

CREATE UNIQUE INDEX IF NOT EXISTS idx_b3s_scanner_resume_actions_one_active
    ON b3s_scanner_resume_actions(scan_id)
    WHERE state IN ('accepted', 'running');

CREATE INDEX IF NOT EXISTS idx_b3s_scanner_resume_actions_scan_updated
    ON b3s_scanner_resume_actions(scan_id, updated_at DESC);
