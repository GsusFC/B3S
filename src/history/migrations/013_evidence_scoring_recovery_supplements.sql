CREATE TABLE b3s_history.evidence_scoring_recovery_supplement_packets (
    id uuid PRIMARY KEY,
    brand_id uuid NOT NULL
        REFERENCES b3s_history.brands(id) ON DELETE RESTRICT,
    packet_fingerprint text NOT NULL CHECK (
        packet_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    schema_version text NOT NULL,
    brand_identity text NOT NULL CHECK (
        length(brand_identity) BETWEEN 1 AND 300
    ),
    base_candidate_set_fingerprint text NOT NULL CHECK (
        base_candidate_set_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    evidence_identity_state_fingerprint text NOT NULL CHECK (
        evidence_identity_state_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    rubric_version text NOT NULL CHECK (
        length(rubric_version) BETWEEN 1 AND 200
    ),
    manifest jsonb NOT NULL CHECK (
        jsonb_typeof(manifest) = 'object'
    ),
    candidates jsonb NOT NULL CHECK (
        jsonb_typeof(candidates) = 'array'
        AND jsonb_array_length(candidates) > 0
    ),
    authority boolean NOT NULL DEFAULT false CHECK (authority = false),
    runtime_effect boolean NOT NULL DEFAULT false CHECK (
        runtime_effect = false
    ),
    automatic_scoring_effect boolean NOT NULL DEFAULT false CHECK (
        automatic_scoring_effect = false
    ),
    production_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        production_runtime_effect = false
    ),
    scanner_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        scanner_runtime_effect = false
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (brand_id, packet_fingerprint)
);

CREATE INDEX idx_b3s_recovery_supplement_packets_created
    ON b3s_history.evidence_scoring_recovery_supplement_packets (
        brand_id, created_at DESC, id DESC
    );

CREATE FUNCTION b3s_history.reject_recovery_supplement_packet_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'evidence scoring recovery supplement packets are append-only';
END;
$$;

CREATE TRIGGER evidence_scoring_recovery_supplements_append_only
BEFORE UPDATE OR DELETE
ON b3s_history.evidence_scoring_recovery_supplement_packets
FOR EACH ROW
EXECUTE FUNCTION
    b3s_history.reject_recovery_supplement_packet_mutation();

COMMENT ON TABLE b3s_history.evidence_scoring_recovery_supplement_packets IS
    'Immutable, non-authoritative direct evidence-to-tile candidates that supplement mapper omissions and remain exact-review gated.';
