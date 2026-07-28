CREATE TABLE b3s_history.evidence_ledger_shadow_states (
    brand_id uuid PRIMARY KEY REFERENCES b3s_history.brands(id) ON DELETE CASCADE,
    latest_capture_id uuid REFERENCES b3s_history.captures(id) ON DELETE SET NULL,
    schema_version text NOT NULL,
    policy_version text NOT NULL,
    mode text NOT NULL CHECK (mode = 'shadow'),
    runtime_effect boolean NOT NULL DEFAULT false CHECK (runtime_effect = false),
    state_fingerprint text NOT NULL CHECK (length(state_fingerprint) = 64),
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    computed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE b3s_history.evidence_ledger_shadow_entries (
    id uuid PRIMARY KEY,
    brand_id uuid NOT NULL REFERENCES b3s_history.brands(id) ON DELETE CASCADE,
    evidence_fingerprint text NOT NULL CHECK (length(evidence_fingerprint) = 64),
    locator_hash text NOT NULL CHECK (length(locator_hash) = 64),
    source_class text NOT NULL,
    evidence_type text NOT NULL,
    canonical_url text NOT NULL DEFAULT '',
    source_domain text NOT NULL DEFAULT '',
    content_hash text NOT NULL CHECK (length(content_hash) = 64),
    state text NOT NULL CHECK (
        state IN (
            'observed',
            'repeated',
            'validation_candidate',
            'not_reacquired',
            'stale_candidate',
            'changed_candidate'
        )
    ),
    reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    age_days integer NOT NULL CHECK (age_days >= 0),
    ttl_days integer NOT NULL CHECK (ttl_days > 0),
    observation_count integer NOT NULL CHECK (observation_count > 0),
    qualified_observation_count integer NOT NULL CHECK (qualified_observation_count >= 0),
    present_in_latest boolean NOT NULL,
    identity_matches jsonb NOT NULL DEFAULT '[]'::jsonb,
    locator_variant_count integer NOT NULL CHECK (locator_variant_count > 0),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (brand_id, evidence_fingerprint)
);

CREATE TABLE b3s_history.evidence_ledger_shadow_observations (
    ledger_entry_id uuid NOT NULL
        REFERENCES b3s_history.evidence_ledger_shadow_entries(id) ON DELETE CASCADE,
    capture_id uuid NOT NULL REFERENCES b3s_history.captures(id) ON DELETE CASCADE,
    source_report_id text NOT NULL,
    observed_at timestamptz NOT NULL,
    identity_match text NOT NULL DEFAULT '',
    acquisition_state text NOT NULL DEFAULT 'unknown',
    invalid boolean NOT NULL DEFAULT false,
    PRIMARY KEY (ledger_entry_id, capture_id)
);

CREATE INDEX idx_b3s_ledger_shadow_brand_state
    ON b3s_history.evidence_ledger_shadow_entries (brand_id, state);
CREATE INDEX idx_b3s_ledger_shadow_locator
    ON b3s_history.evidence_ledger_shadow_entries (brand_id, locator_hash);
CREATE INDEX idx_b3s_ledger_shadow_observation_capture
    ON b3s_history.evidence_ledger_shadow_observations (capture_id);
