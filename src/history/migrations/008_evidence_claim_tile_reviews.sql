CREATE TABLE b3s_history.evidence_claim_tile_review_events (
    id uuid PRIMARY KEY,
    brand_id uuid NOT NULL REFERENCES b3s_history.brands(id) ON DELETE RESTRICT,
    subject_type text NOT NULL CHECK (
        subject_type = 'claim_tile_mapping'
    ),
    subject_id text NOT NULL CHECK (subject_id ~ '^[0-9a-f]{64}$'),
    case_id text NOT NULL CHECK (length(case_id) BETWEEN 1 AND 300),
    mapping_id text NOT NULL CHECK (
        mapping_id ~ '^[0-9a-f]{64}$'
        AND mapping_id = subject_id
    ),
    mapping_series_id text NOT NULL CHECK (
        mapping_series_id ~ '^[0-9a-f]{64}$'
    ),
    source_evidence_id text NOT NULL CHECK (
        source_evidence_id ~ '^[0-9a-f]{64}$'
    ),
    claim_variant_id text NOT NULL CHECK (
        claim_variant_id ~ '^[0-9a-f]{64}$'
    ),
    component_key text NOT NULL CHECK (
        length(component_key) BETWEEN 1 AND 200
    ),
    tile_id text NOT NULL CHECK (length(tile_id) BETWEEN 1 AND 200),
    tile_key text NOT NULL CHECK (length(tile_key) BETWEEN 1 AND 200),
    polarity text NOT NULL CHECK (
        polarity IN ('supports', 'weakens', 'insufficient_evidence')
    ),
    sequence bigint NOT NULL CHECK (sequence > 0),
    decision text NOT NULL CHECK (
        decision IN ('accepted', 'disputed', 'rejected', 'revoked')
    ),
    supersedes_event_id uuid,
    schema_version text NOT NULL,
    policy_version text NOT NULL,
    evaluator_version text NOT NULL CHECK (
        length(evaluator_version) BETWEEN 1 AND 200
    ),
    reviewer text NOT NULL CHECK (length(reviewer) BETWEEN 1 AND 200),
    actor_id text NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 200),
    reason_code text NOT NULL CHECK (
        reason_code ~ '^[a-z0-9][a-z0-9_]{0,99}$'
    ),
    rationale text NOT NULL CHECK (length(rationale) BETWEEN 1 AND 2000),
    idempotency_key_hash text NOT NULL CHECK (
        length(idempotency_key_hash) = 64
    ),
    request_fingerprint text NOT NULL CHECK (
        length(request_fingerprint) = 64
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
    UNIQUE (brand_id, subject_type, subject_id, id),
    UNIQUE (brand_id, subject_type, subject_id, sequence),
    UNIQUE (brand_id, idempotency_key_hash),
    FOREIGN KEY (
        brand_id, subject_type, subject_id, supersedes_event_id
    ) REFERENCES b3s_history.evidence_claim_tile_review_events (
        brand_id, subject_type, subject_id, id
    ) ON DELETE RESTRICT,
    FOREIGN KEY (brand_id, mapping_id)
        REFERENCES b3s_history.evidence_claim_tile_mappings (
            brand_id, mapping_id
        ) ON DELETE RESTRICT,
    CHECK (
        (sequence = 1 AND supersedes_event_id IS NULL)
        OR (sequence > 1 AND supersedes_event_id IS NOT NULL)
    ),
    CHECK (supersedes_event_id IS NULL OR supersedes_event_id <> id)
);

CREATE INDEX idx_b3s_claim_tile_review_subject
    ON b3s_history.evidence_claim_tile_review_events (
        brand_id, subject_type, subject_id, sequence DESC
    );

CREATE INDEX idx_b3s_claim_tile_review_created
    ON b3s_history.evidence_claim_tile_review_events (
        brand_id, created_at DESC, id DESC
    );
