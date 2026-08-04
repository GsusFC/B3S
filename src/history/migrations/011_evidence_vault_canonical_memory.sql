CREATE TABLE b3s_history.evidence_vault_canonical_memory_packets (
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
    parent_canonical_memory_version text CHECK (
        parent_canonical_memory_version IS NULL
        OR parent_canonical_memory_version ~ '^[0-9a-f]{64}$'
    ),
    reference_resolution_fingerprint text NOT NULL CHECK (
        reference_resolution_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    reference_resolution jsonb NOT NULL CHECK (
        jsonb_typeof(reference_resolution) = 'object'
    ),
    manifest jsonb NOT NULL CHECK (
        jsonb_typeof(manifest) = 'object'
    ),
    candidate_tiles jsonb NOT NULL CHECK (
        jsonb_typeof(candidate_tiles) = 'array'
        AND jsonb_array_length(candidate_tiles) = 80
    ),
    authority_state text NOT NULL DEFAULT 'pending_review' CHECK (
        authority_state = 'pending_review'
    ),
    authority boolean NOT NULL DEFAULT false CHECK (authority = false),
    production_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        production_runtime_effect = false
    ),
    scanner_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        scanner_runtime_effect = false
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (brand_id, packet_fingerprint),
    UNIQUE (
        brand_id,
        packet_fingerprint,
        reference_resolution_fingerprint
    )
);

CREATE INDEX idx_b3s_vault_canonical_packets_created
    ON b3s_history.evidence_vault_canonical_memory_packets (
        brand_id, created_at DESC, id DESC
    );

CREATE FUNCTION b3s_history.reject_vault_canonical_packet_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'evidence Vault canonical-memory packets are append-only';
END;
$$;

CREATE TRIGGER evidence_vault_canonical_packets_append_only
BEFORE UPDATE OR DELETE
ON b3s_history.evidence_vault_canonical_memory_packets
FOR EACH ROW
EXECUTE FUNCTION
    b3s_history.reject_vault_canonical_packet_mutation();

CREATE TABLE b3s_history.evidence_vault_canonical_memory_promotion_events (
    id uuid PRIMARY KEY,
    brand_id uuid NOT NULL
        REFERENCES b3s_history.brands(id) ON DELETE RESTRICT,
    event_type text NOT NULL CHECK (
        event_type = 'canonical_memory_promotion'
    ),
    sequence bigint NOT NULL CHECK (sequence > 0),
    previous_event_id uuid,
    brand_identity text NOT NULL CHECK (
        length(brand_identity) BETWEEN 1 AND 300
    ),
    candidate_packet_fingerprint text NOT NULL CHECK (
        candidate_packet_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    reference_resolution_fingerprint text NOT NULL CHECK (
        reference_resolution_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    promotion_policy_fingerprint text NOT NULL CHECK (
        promotion_policy_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    parent_canonical_memory_version text CHECK (
        parent_canonical_memory_version IS NULL
        OR parent_canonical_memory_version ~ '^[0-9a-f]{64}$'
    ),
    promoted_canonical_memory_version text NOT NULL CHECK (
        promoted_canonical_memory_version ~ '^[0-9a-f]{64}$'
    ),
    decision text NOT NULL CHECK (decision = 'promote'),
    reviewer_id text NOT NULL CHECK (
        length(reviewer_id) BETWEEN 1 AND 200
    ),
    reviewed_at timestamptz NOT NULL,
    rationale text NOT NULL CHECK (length(rationale) BETWEEN 1 AND 4000),
    schema_version text NOT NULL,
    idempotency_key_hash text NOT NULL CHECK (
        idempotency_key_hash ~ '^[0-9a-f]{64}$'
    ),
    request_fingerprint text NOT NULL CHECK (
        request_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    authority boolean NOT NULL DEFAULT true CHECK (authority = true),
    authority_scope text NOT NULL DEFAULT 'b3s-vault' CHECK (
        authority_scope = 'b3s-vault'
    ),
    production_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        production_runtime_effect = false
    ),
    scanner_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        scanner_runtime_effect = false
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (brand_id, id),
    UNIQUE (brand_id, sequence),
    UNIQUE (brand_id, idempotency_key_hash),
    UNIQUE (brand_id, candidate_packet_fingerprint),
    FOREIGN KEY (
        brand_id,
        candidate_packet_fingerprint,
        reference_resolution_fingerprint
    ) REFERENCES b3s_history.evidence_vault_canonical_memory_packets (
        brand_id,
        packet_fingerprint,
        reference_resolution_fingerprint
    ) ON DELETE RESTRICT,
    FOREIGN KEY (brand_id, previous_event_id)
        REFERENCES b3s_history.evidence_vault_canonical_memory_promotion_events (
            brand_id, id
        ) ON DELETE RESTRICT,
    CHECK (
        (sequence = 1
            AND previous_event_id IS NULL
            AND parent_canonical_memory_version IS NULL)
        OR (sequence > 1
            AND previous_event_id IS NOT NULL
            AND parent_canonical_memory_version IS NOT NULL)
    ),
    CHECK (previous_event_id IS NULL OR previous_event_id <> id)
);

CREATE INDEX idx_b3s_vault_canonical_promotions_current
    ON b3s_history.evidence_vault_canonical_memory_promotion_events (
        brand_id, sequence DESC
    );

CREATE FUNCTION b3s_history.reject_vault_canonical_promotion_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'evidence Vault canonical-memory promotions are append-only';
END;
$$;

CREATE TRIGGER evidence_vault_canonical_promotions_append_only
BEFORE UPDATE OR DELETE
ON b3s_history.evidence_vault_canonical_memory_promotion_events
FOR EACH ROW
EXECUTE FUNCTION
    b3s_history.reject_vault_canonical_promotion_mutation();

COMMENT ON TABLE b3s_history.evidence_vault_canonical_memory_packets IS
    'Immutable pending-review candidate packets. Authority is derived only from the separate promotion journal.';

COMMENT ON TABLE b3s_history.evidence_vault_canonical_memory_promotion_events IS
    'Append-only human promotions with authority restricted to b3s-vault and no scanner or production runtime effect.';
