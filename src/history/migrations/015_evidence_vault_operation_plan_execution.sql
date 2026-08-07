ALTER TABLE b3s_history.evidence_vault_canonical_memory_packets
    DROP CONSTRAINT IF EXISTS evidence_vault_canonical_memory_packets_packet_kind_check;

ALTER TABLE b3s_history.evidence_vault_canonical_memory_packets
    ADD CONSTRAINT evidence_vault_canonical_memory_packets_packet_kind_check
    CHECK (
        packet_kind IN (
            'canonical_v1',
            'operational_source_v2',
            'operational_reviewed_v2',
            'operational_v2'
        )
    );

CREATE TABLE b3s_history.evidence_vault_operation_plans (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL
        REFERENCES b3s_history.workspaces(id) ON DELETE CASCADE,
    brand_id uuid NOT NULL
        REFERENCES b3s_history.brands(id) ON DELETE CASCADE,
    scan_run_id uuid NOT NULL UNIQUE
        REFERENCES b3s_history.scan_runs(id) ON DELETE CASCADE,
    observation_hash text NOT NULL CHECK (
        observation_hash ~ '^[0-9a-f]{64}$'
    ),
    operation_plan_fingerprint text NOT NULL CHECK (
        operation_plan_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    canonical_memory_version text CHECK (
        canonical_memory_version IS NULL
        OR canonical_memory_version ~ '^[0-9a-f]{64}$'
    ),
    mode text NOT NULL CHECK (
        mode IN ('baseline', 'incremental_refresh', 'diagnostic_full')
    ),
    status text NOT NULL CHECK (
        status IN (
            'pending', 'not_required', 'claimed', 'running',
            'result_persisted', 'completed', 'superseded',
            'failed_retryable'
        )
    ),
    plan_payload jsonb NOT NULL CHECK (
        jsonb_typeof(plan_payload) = 'object'
    ),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner text CHECK (
        lease_owner IS NULL OR length(lease_owner) BETWEEN 1 AND 200
    ),
    lease_token uuid,
    lease_generation integer NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_expires_at timestamptz,
    claimed_at timestamptz,
    started_at timestamptz,
    heartbeat_at timestamptz,
    result_fingerprint text CHECK (
        result_fingerprint IS NULL
        OR result_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    result_payload jsonb CHECK (
        result_payload IS NULL OR jsonb_typeof(result_payload) = 'object'
    ),
    result_persisted_at timestamptz,
    candidate_packet_fingerprint text CHECK (
        candidate_packet_fingerprint IS NULL
        OR candidate_packet_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    completed_at timestamptz,
    superseded_at timestamptz,
    last_error text NOT NULL DEFAULT '' CHECK (length(last_error) <= 4000),
    authority boolean NOT NULL DEFAULT false CHECK (authority = false),
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
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (status IN ('claimed', 'running')) =
        (
            lease_owner IS NOT NULL
            AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
    ),
    CHECK (
        (
            result_fingerprint IS NULL
            AND result_payload IS NULL
            AND result_persisted_at IS NULL
        )
        OR (
            result_fingerprint IS NOT NULL
            AND result_payload IS NOT NULL
            AND result_persisted_at IS NOT NULL
        )
    ),
    CHECK (
        status NOT IN ('result_persisted', 'completed')
        OR result_payload IS NOT NULL
    ),
    CHECK (
        status IN ('result_persisted', 'completed', 'superseded')
        OR result_payload IS NULL
    ),
    CHECK (
        status <> 'completed'
        OR (
            completed_at IS NOT NULL
            AND (
                candidate_packet_fingerprint IS NOT NULL
                OR result_payload ->> 'output_kind' = 'no_delta'
            )
        )
    ),
    CHECK (
        (status = 'superseded') = (superseded_at IS NOT NULL)
    ),
    CHECK (
        (
            result_payload IS NULL
            AND candidate_packet_fingerprint IS NULL
        )
        OR (
            result_payload ->> 'output_kind' = 'no_delta'
            AND candidate_packet_fingerprint IS NULL
        )
        OR (
            result_payload ->> 'output_kind' = 'candidate_overlay'
            AND (
                candidate_packet_fingerprint IS NULL
                OR candidate_packet_fingerprint =
                    result_payload ->> 'candidate_packet_fingerprint'
            )
        )
    )
);

CREATE INDEX idx_b3s_vault_operation_plans_claimable
    ON b3s_history.evidence_vault_operation_plans (
        status, lease_expires_at, created_at, id
    )
    WHERE status IN (
        'pending', 'not_required', 'failed_retryable',
        'claimed', 'running'
    );

CREATE OR REPLACE FUNCTION b3s_history.guard_vault_operation_plan_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.brand_id IS DISTINCT FROM OLD.brand_id
       OR NEW.scan_run_id IS DISTINCT FROM OLD.scan_run_id
       OR NEW.observation_hash IS DISTINCT FROM OLD.observation_hash
       OR NEW.operation_plan_fingerprint IS DISTINCT FROM OLD.operation_plan_fingerprint
       OR NEW.canonical_memory_version IS DISTINCT FROM OLD.canonical_memory_version
       OR NEW.mode IS DISTINCT FROM OLD.mode
       OR NEW.plan_payload IS DISTINCT FROM OLD.plan_payload
       OR NEW.authority IS DISTINCT FROM OLD.authority
       OR NEW.authority_scope IS DISTINCT FROM OLD.authority_scope
       OR NEW.production_runtime_effect IS DISTINCT FROM OLD.production_runtime_effect
       OR NEW.scanner_runtime_effect IS DISTINCT FROM OLD.scanner_runtime_effect
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'Evidence Vault operation plan identity is immutable';
    END IF;
    IF OLD.result_payload IS NOT NULL AND (
        NEW.result_payload IS DISTINCT FROM OLD.result_payload
        OR NEW.result_fingerprint IS DISTINCT FROM OLD.result_fingerprint
        OR NEW.result_persisted_at IS DISTINCT FROM OLD.result_persisted_at
    ) THEN
        RAISE EXCEPTION 'Evidence Vault operation result is immutable';
    END IF;
    IF (
        OLD.candidate_packet_fingerprint IS NOT NULL
        OR OLD.status = 'completed'
    ) AND NEW.candidate_packet_fingerprint IS DISTINCT FROM OLD.candidate_packet_fingerprint THEN
        RAISE EXCEPTION 'Evidence Vault operation output reference is immutable';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_b3s_vault_operation_plan_identity
BEFORE UPDATE ON b3s_history.evidence_vault_operation_plans
FOR EACH ROW
EXECUTE FUNCTION b3s_history.guard_vault_operation_plan_identity();

COMMENT ON TABLE b3s_history.evidence_vault_operation_plans IS
    'Vault-only leased execution state for one immutable persisted scan plan. It grants no memory or scoring authority.';


CREATE TABLE b3s_history.evidence_vault_operational_relation_reviews (
    id uuid PRIMARY KEY,
    brand_id uuid NOT NULL
        REFERENCES b3s_history.brands(id) ON DELETE CASCADE,
    source_packet_id uuid NOT NULL
        REFERENCES b3s_history.evidence_vault_canonical_memory_packets(id)
        ON DELETE RESTRICT,
    source_packet_fingerprint text NOT NULL CHECK (
        source_packet_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    relation_id text NOT NULL CHECK (relation_id ~ '^[0-9a-f]{64}$'),
    decision text NOT NULL CHECK (decision IN ('accept', 'reject')),
    reviewer_id text NOT NULL CHECK (length(reviewer_id) BETWEEN 1 AND 200),
    rationale text NOT NULL CHECK (length(rationale) BETWEEN 1 AND 2000),
    review_request_fingerprint text NOT NULL CHECK (
        review_request_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    authority boolean NOT NULL DEFAULT true CHECK (authority = true),
    authority_scope text NOT NULL DEFAULT 'relation_review' CHECK (
        authority_scope = 'relation_review'
    ),
    production_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        production_runtime_effect = false
    ),
    scanner_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        scanner_runtime_effect = false
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (brand_id, source_packet_id, relation_id),
    UNIQUE (brand_id, review_request_fingerprint, relation_id)
);

CREATE INDEX idx_b3s_vault_operational_relation_reviews_source
    ON b3s_history.evidence_vault_operational_relation_reviews (
        brand_id, source_packet_fingerprint, relation_id
    );

COMMENT ON TABLE b3s_history.evidence_vault_operational_relation_reviews IS
    'Human authority events for model-proposed Vault relation candidates; events have no direct runtime or scoring effect.';


CREATE OR REPLACE FUNCTION b3s_history.guard_vault_operational_relation_review()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Evidence Vault operational relation review is immutable';
END
$$;

CREATE TRIGGER trg_b3s_vault_operational_relation_review_immutable
BEFORE UPDATE OR DELETE
ON b3s_history.evidence_vault_operational_relation_reviews
FOR EACH ROW
EXECUTE FUNCTION b3s_history.guard_vault_operational_relation_review();
