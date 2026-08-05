CREATE TABLE b3s_history.evidence_vault_canonical_score_evaluations (
    id uuid PRIMARY KEY,
    brand_id uuid NOT NULL
        REFERENCES b3s_history.brands(id) ON DELETE RESTRICT,
    promotion_event_id uuid NOT NULL,
    canonical_memory_version text NOT NULL CHECK (
        canonical_memory_version ~ '^[0-9a-f]{64}$'
    ),
    evaluation_identity text NOT NULL CHECK (
        evaluation_identity ~ '^[0-9a-f]{64}$'
    ),
    score_input_fingerprint text NOT NULL CHECK (
        score_input_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    derived_tile_state_fingerprint text NOT NULL CHECK (
        derived_tile_state_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    rubric_version text NOT NULL,
    tile_contract_registry_fingerprint text NOT NULL CHECK (
        tile_contract_registry_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    reducer_policy_fingerprint text NOT NULL CHECK (
        reducer_policy_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    aggregation_policy_fingerprint text NOT NULL CHECK (
        aggregation_policy_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    schema_version text NOT NULL,
    score integer NOT NULL CHECK (score BETWEEN 0 AND 100),
    component_breakdown jsonb NOT NULL CHECK (
        jsonb_typeof(component_breakdown) = 'array'
        AND jsonb_array_length(component_breakdown) = 10
    ),
    base_average numeric(5, 2) NOT NULL CHECK (
        base_average BETWEEN 0 AND 10
    ),
    magnetism_capped boolean NOT NULL,
    reused_from_evaluation_identity text,
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
    created_at timestamptz NOT NULL,
    UNIQUE (brand_id, evaluation_identity),
    UNIQUE (brand_id, canonical_memory_version),
    FOREIGN KEY (brand_id, promotion_event_id)
        REFERENCES b3s_history.evidence_vault_canonical_memory_promotion_events (
            brand_id, id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (brand_id, reused_from_evaluation_identity)
        REFERENCES b3s_history.evidence_vault_canonical_score_evaluations (
            brand_id, evaluation_identity
        ) ON DELETE RESTRICT,
    CHECK (
        reused_from_evaluation_identity IS NULL
        OR reused_from_evaluation_identity <> evaluation_identity
    )
);

CREATE INDEX idx_b3s_vault_canonical_scores_input
    ON b3s_history.evidence_vault_canonical_score_evaluations (
        brand_id, score_input_fingerprint, created_at, id
    );

CREATE FUNCTION b3s_history.reject_vault_canonical_score_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'evidence Vault canonical score evaluations are append-only';
END;
$$;

CREATE TRIGGER evidence_vault_canonical_scores_append_only
BEFORE UPDATE OR DELETE
ON b3s_history.evidence_vault_canonical_score_evaluations
FOR EACH ROW
EXECUTE FUNCTION
    b3s_history.reject_vault_canonical_score_mutation();

COMMENT ON TABLE b3s_history.evidence_vault_canonical_score_evaluations IS
    'Immutable deterministic evaluations derived only from promoted b3s-vault canonical memory; scanner and production scores remain independent.';
