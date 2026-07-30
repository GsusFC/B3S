ALTER TABLE b3s_history.evidence_claim_tile_review_events
    ADD COLUMN review_packet_fingerprint text;

ALTER TABLE b3s_history.evidence_claim_tile_review_events
    ADD CONSTRAINT evidence_claim_tile_review_packet_fingerprint_sha256
    CHECK (
        review_packet_fingerprint IS NULL
        OR review_packet_fingerprint ~ '^[0-9a-f]{64}$'
    );

ALTER TABLE b3s_history.evidence_claim_tile_review_events
    ADD CONSTRAINT evidence_claim_tile_review_v2_requires_packet
    CHECK (
        schema_version <> 'evidence-claim-tile-review-event-v2'
        OR review_packet_fingerprint IS NOT NULL
    );

COMMENT ON COLUMN
    b3s_history.evidence_claim_tile_review_events.review_packet_fingerprint
IS
    'Immutable SHA-256 binding to the exact manifest, candidates, and schema reviewed. NULL only for legacy v1 events.';
