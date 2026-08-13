-- Support bounded per-client Scanner API credentials without scanning the
-- complete durable job history for every reservation.
CREATE INDEX IF NOT EXISTS idx_b3s_scanner_jobs_client_created
    ON b3s_scanner_jobs(client_id, created_at DESC);
