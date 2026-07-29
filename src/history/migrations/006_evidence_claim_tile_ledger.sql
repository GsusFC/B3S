CREATE TABLE b3s_history.evidence_claim_tile_ledger_states (
    brand_id uuid PRIMARY KEY REFERENCES b3s_history.brands(id) ON DELETE CASCADE,
    latest_capture_id uuid REFERENCES b3s_history.captures(id) ON DELETE SET NULL,
    schema_version text NOT NULL,
    policy_version text NOT NULL,
    mapping_version text NOT NULL,
    mode text NOT NULL CHECK (mode = 'shadow'),
    runtime_effect boolean NOT NULL DEFAULT false CHECK (runtime_effect = false),
    authority boolean NOT NULL DEFAULT false CHECK (authority = false),
    state_fingerprint text NOT NULL CHECK (state_fingerprint ~ '^[0-9a-f]{64}$'),
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    computed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE b3s_history.evidence_claim_tile_mapping_series (
    id uuid PRIMARY KEY,
    brand_id uuid NOT NULL REFERENCES b3s_history.brands(id) ON DELETE CASCADE,
    mapping_series_id text NOT NULL CHECK (
        mapping_series_id ~ '^[0-9a-f]{64}$'
    ),
    mapping_version text NOT NULL,
    mapping_policy_version text NOT NULL,
    identity_policy_version text NOT NULL,
    claim_memory_policy_version text NOT NULL,
    source_registry_fingerprint text NOT NULL,
    pipeline_version text NOT NULL,
    rubric_version text NOT NULL,
    prompt_version text NOT NULL,
    evaluator_model text NOT NULL,
    claim_projection_version text NOT NULL,
    identity_projection_version text NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    report_count integer NOT NULL CHECK (report_count > 0),
    runtime_effect boolean NOT NULL DEFAULT false CHECK (runtime_effect = false),
    authority boolean NOT NULL DEFAULT false CHECK (authority = false),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (brand_id, mapping_series_id)
);

CREATE TABLE b3s_history.evidence_claim_tile_mappings (
    id uuid PRIMARY KEY,
    brand_id uuid NOT NULL REFERENCES b3s_history.brands(id) ON DELETE CASCADE,
    mapping_series_id text NOT NULL CHECK (
        mapping_series_id ~ '^[0-9a-f]{64}$'
    ),
    mapping_id text NOT NULL CHECK (mapping_id ~ '^[0-9a-f]{64}$'),
    source_evidence_id text NOT NULL CHECK (
        source_evidence_id ~ '^[0-9a-f]{64}$'
    ),
    claim_evidence_id text NOT NULL CHECK (
        claim_evidence_id ~ '^[0-9a-f]{64}$'
    ),
    claim_slot_id text NOT NULL CHECK (claim_slot_id ~ '^[0-9a-f]{64}$'),
    claim_variant_id text NOT NULL CHECK (
        claim_variant_id ~ '^[0-9a-f]{64}$'
    ),
    component_key text NOT NULL,
    tile_id text NOT NULL,
    tile_key text NOT NULL,
    polarity text NOT NULL CHECK (
        polarity IN ('supports', 'weakens', 'insufficient_evidence')
    ),
    source_class text NOT NULL,
    identity_status text NOT NULL,
    source_independence_status text NOT NULL,
    state text NOT NULL CHECK (
        state IN ('observed', 'repeated', 'not_reacquired')
    ),
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    observation_count integer NOT NULL CHECK (observation_count > 0),
    present_in_series_latest boolean NOT NULL,
    present_in_latest_report boolean NOT NULL,
    source_evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    match_methods jsonb NOT NULL DEFAULT '[]'::jsonb,
    quote_hashes jsonb NOT NULL DEFAULT '[]'::jsonb,
    runtime_effect boolean NOT NULL DEFAULT false CHECK (runtime_effect = false),
    authority boolean NOT NULL DEFAULT false CHECK (authority = false),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (brand_id, mapping_id),
    FOREIGN KEY (brand_id, mapping_series_id)
        REFERENCES b3s_history.evidence_claim_tile_mapping_series (
            brand_id, mapping_series_id
        ) ON DELETE CASCADE
);

CREATE TABLE b3s_history.evidence_claim_tile_mapping_observations (
    mapping_entry_id uuid NOT NULL
        REFERENCES b3s_history.evidence_claim_tile_mappings(id) ON DELETE CASCADE,
    capture_id uuid NOT NULL REFERENCES b3s_history.captures(id) ON DELETE CASCADE,
    source_report_id text NOT NULL,
    claim_occurrence_id text NOT NULL CHECK (
        claim_occurrence_id ~ '^[0-9a-f]{64}$'
    ),
    observed_at timestamptz NOT NULL,
    tile_state text NOT NULL CHECK (
        tile_state IN ('ok', 'no', 'sin_evidencia')
    ),
    quote_hash text NOT NULL CHECK (quote_hash ~ '^[0-9a-f]{64}$'),
    match_method text NOT NULL CHECK (
        match_method IN ('explicit_evidence_ref', 'unique_literal_quote')
    ),
    duplicate_observation_count integer NOT NULL CHECK (
        duplicate_observation_count > 0
    ),
    PRIMARY KEY (mapping_entry_id, capture_id)
);

CREATE INDEX idx_b3s_claim_tile_series_brand
    ON b3s_history.evidence_claim_tile_mapping_series (
        brand_id, last_seen_at DESC
    );
CREATE INDEX idx_b3s_claim_tile_mapping_brand_state
    ON b3s_history.evidence_claim_tile_mappings (
        brand_id, state, mapping_series_id
    );
CREATE INDEX idx_b3s_claim_tile_mapping_source
    ON b3s_history.evidence_claim_tile_mappings (
        brand_id, source_evidence_id
    );
CREATE INDEX idx_b3s_claim_tile_mapping_claim
    ON b3s_history.evidence_claim_tile_mappings (
        brand_id, claim_variant_id
    );
CREATE INDEX idx_b3s_claim_tile_observation_capture
    ON b3s_history.evidence_claim_tile_mapping_observations (capture_id);
