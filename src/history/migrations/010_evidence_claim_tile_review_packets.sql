CREATE TABLE b3s_history.evidence_claim_tile_review_packets (
    id uuid PRIMARY KEY,
    brand_id uuid NOT NULL
        REFERENCES b3s_history.brands(id) ON DELETE RESTRICT,
    packet_kind text NOT NULL CHECK (packet_kind = 'claim_tile'),
    packet_fingerprint text NOT NULL CHECK (
        packet_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    candidate_fingerprint text NOT NULL CHECK (
        candidate_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    schema_version text NOT NULL,
    packet_schema_version text NOT NULL,
    candidate_schema_version text NOT NULL,
    ledger_state_fingerprint text NOT NULL CHECK (
        ledger_state_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    mapping_series_id text NOT NULL CHECK (
        mapping_series_id ~ '^[0-9a-f]{64}$'
    ),
    rubric_version text NOT NULL,
    candidate_count integer NOT NULL CHECK (candidate_count >= 0),
    manifest jsonb NOT NULL CHECK (
        jsonb_typeof(manifest) = 'object'
    ),
    candidates jsonb NOT NULL CHECK (
        jsonb_typeof(candidates) = 'array'
        AND jsonb_array_length(candidates) = candidate_count
    ),
    runtime_effect boolean NOT NULL DEFAULT false CHECK (
        runtime_effect = false
    ),
    authority boolean NOT NULL DEFAULT false CHECK (authority = false),
    automatic_tile_effect boolean NOT NULL DEFAULT false CHECK (
        automatic_tile_effect = false
    ),
    automatic_scoring_effect boolean NOT NULL DEFAULT false CHECK (
        automatic_scoring_effect = false
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (brand_id, packet_fingerprint)
);

CREATE INDEX idx_b3s_claim_tile_review_packets_created
    ON b3s_history.evidence_claim_tile_review_packets (
        brand_id, created_at DESC, id DESC
    );

CREATE FUNCTION b3s_history.reject_claim_tile_review_packet_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'evidence claim-to-tile review packets are append-only';
END;
$$;

CREATE TRIGGER evidence_claim_tile_review_packets_append_only
BEFORE UPDATE OR DELETE
ON b3s_history.evidence_claim_tile_review_packets
FOR EACH ROW
EXECUTE FUNCTION
    b3s_history.reject_claim_tile_review_packet_mutation();

ALTER TABLE b3s_history.evidence_claim_tile_review_events
    ADD CONSTRAINT evidence_claim_tile_review_registered_packet_fk
    FOREIGN KEY (brand_id, review_packet_fingerprint)
    REFERENCES b3s_history.evidence_claim_tile_review_packets (
        brand_id, packet_fingerprint
    )
    ON DELETE RESTRICT
    NOT VALID;

COMMENT ON TABLE b3s_history.evidence_claim_tile_review_packets IS
    'Append-only private reviewer packets reconstructed from immutable report snapshots. They remain shadow-only and have no tile or scoring authority.';
