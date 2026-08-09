-- Final cumulative landing hardening for the dormant Evidence Vault v2 stack.
--
-- Earlier migrations are immutable and may already be journaled.  This migration
-- closes cross-tenant relation-review identity, operational packet shape, and
-- operation-plan append-only gaps without enabling scanner or production runtime.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_operational_relation_reviews AS reviews
        LEFT JOIN b3s_history.evidence_vault_canonical_memory_packets AS packets
          ON packets.id = reviews.source_packet_id
        WHERE packets.id IS NULL
           OR packets.brand_id IS DISTINCT FROM reviews.brand_id
           OR packets.packet_fingerprint IS DISTINCT FROM
                reviews.source_packet_fingerprint
           OR packets.packet_kind IS DISTINCT FROM 'operational_source_v2'
    ) THEN
        RAISE EXCEPTION
            'operational relation-review source identity preflight failed';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_canonical_memory_packets AS packets
        WHERE packets.packet_kind IN (
            'operational_source_v2',
            'operational_reviewed_v2',
            'operational_v2'
        )
          AND (
              packets.packet_payload IS NULL
              OR jsonb_typeof(packets.packet_payload) <> 'object'
          )
    ) THEN
        RAISE EXCEPTION
            'operational packet payload preflight failed';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_operation_plans AS plans
        WHERE (
            plans.completed_at IS NOT NULL
            AND plans.status <> 'completed'
        ) OR (
            plans.candidate_packet_fingerprint IS NOT NULL
            AND plans.status <> 'completed'
        )
    ) THEN
        RAISE EXCEPTION
            'operation plan lifecycle preflight failed';
    END IF;
END;
$$;

ALTER TABLE b3s_history.evidence_vault_canonical_memory_packets
    ADD CONSTRAINT evidence_vault_packets_source_identity_key
        UNIQUE (brand_id, id, packet_fingerprint, packet_kind),
    ADD CONSTRAINT evidence_vault_all_operational_packet_payload_check CHECK (
        packet_kind NOT IN (
            'operational_source_v2',
            'operational_reviewed_v2',
            'operational_v2'
        )
        OR (
            packet_payload IS NOT NULL
            AND jsonb_typeof(packet_payload) = 'object'
        )
    );

ALTER TABLE b3s_history.evidence_vault_operational_relation_reviews
    ADD COLUMN source_packet_kind text NOT NULL
        DEFAULT 'operational_source_v2'
        CHECK (source_packet_kind = 'operational_source_v2'),
    ADD CONSTRAINT evidence_vault_relation_review_source_identity_fk
        FOREIGN KEY (
            brand_id,
            source_packet_id,
            source_packet_fingerprint,
            source_packet_kind
        )
        REFERENCES b3s_history.evidence_vault_canonical_memory_packets (
            brand_id,
            id,
            packet_fingerprint,
            packet_kind
        )
        ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION b3s_history.guard_vault_operation_plan_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'Evidence Vault operation plans are append-only';
    END IF;
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.brand_id IS DISTINCT FROM OLD.brand_id
       OR NEW.scan_run_id IS DISTINCT FROM OLD.scan_run_id
       OR NEW.observation_hash IS DISTINCT FROM OLD.observation_hash
       OR NEW.operation_plan_fingerprint IS DISTINCT FROM
            OLD.operation_plan_fingerprint
       OR NEW.canonical_memory_version IS DISTINCT FROM
            OLD.canonical_memory_version
       OR NEW.mode IS DISTINCT FROM OLD.mode
       OR NEW.plan_payload IS DISTINCT FROM OLD.plan_payload
       OR NEW.authority IS DISTINCT FROM OLD.authority
       OR NEW.authority_scope IS DISTINCT FROM OLD.authority_scope
       OR NEW.production_runtime_effect IS DISTINCT FROM
            OLD.production_runtime_effect
       OR NEW.scanner_runtime_effect IS DISTINCT FROM
            OLD.scanner_runtime_effect
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION
            'Evidence Vault operation plan identity is immutable';
    END IF;
    IF OLD.result_payload IS NOT NULL AND (
        NEW.result_payload IS DISTINCT FROM OLD.result_payload
        OR NEW.result_fingerprint IS DISTINCT FROM OLD.result_fingerprint
        OR NEW.result_persisted_at IS DISTINCT FROM OLD.result_persisted_at
    ) THEN
        RAISE EXCEPTION 'Evidence Vault operation result is immutable';
    END IF;
    IF NEW.candidate_packet_fingerprint IS DISTINCT FROM
        OLD.candidate_packet_fingerprint
       AND NOT (
           OLD.status = 'result_persisted'
           AND NEW.status = 'completed'
           AND OLD.candidate_packet_fingerprint IS NULL
           AND NEW.candidate_packet_fingerprint IS NOT NULL
       ) THEN
        RAISE EXCEPTION
            'Evidence Vault operation output reference transition is invalid';
    END IF;
    IF OLD.status IN ('completed', 'superseded') THEN
        RAISE EXCEPTION
            'terminal Evidence Vault operation plans are immutable';
    END IF;
    IF NOT (
        (OLD.status IN ('pending', 'not_required')
            AND NEW.status IN (
                'claimed', 'failed_retryable', 'superseded'
            ))
        OR (OLD.status = 'failed_retryable'
            AND NEW.status IN ('claimed', 'superseded'))
        OR (OLD.status = 'claimed'
            AND NEW.status IN (
                'claimed', 'running', 'failed_retryable', 'superseded'
            ))
        OR (OLD.status = 'running'
            AND NEW.status IN (
                'claimed', 'running', 'result_persisted',
                'failed_retryable', 'superseded'
            ))
        OR (OLD.status = 'result_persisted'
            AND NEW.status IN ('completed', 'superseded'))
    ) THEN
        RAISE EXCEPTION
            'Evidence Vault operation plan status transition is invalid';
    END IF;
    IF OLD.result_payload IS NULL AND (
        NEW.result_payload IS NOT NULL
        OR NEW.result_fingerprint IS NOT NULL
        OR NEW.result_persisted_at IS NOT NULL
    ) AND (
        OLD.status <> 'running'
        OR NEW.status NOT IN ('result_persisted', 'superseded')
        OR OLD.lease_owner IS NULL
        OR OLD.lease_token IS NULL
        OR OLD.lease_expires_at IS NULL
        OR OLD.lease_expires_at <= clock_timestamp()
        OR NEW.result_payload IS NULL
        OR NEW.result_fingerprint IS NULL
        OR NEW.result_persisted_at IS NULL
    ) THEN
        RAISE EXCEPTION
            'Evidence Vault operation result transition is invalid';
    END IF;
    IF NEW.completed_at IS DISTINCT FROM OLD.completed_at
       AND NOT (
           OLD.status = 'result_persisted'
           AND NEW.status = 'completed'
           AND OLD.completed_at IS NULL
           AND NEW.completed_at IS NOT NULL
       ) THEN
        RAISE EXCEPTION
            'Evidence Vault operation completion timestamp transition is invalid';
    END IF;
    IF NEW.superseded_at IS DISTINCT FROM OLD.superseded_at
       AND NOT (
           NEW.status = 'superseded'
           AND OLD.superseded_at IS NULL
           AND NEW.superseded_at IS NOT NULL
       ) THEN
        RAISE EXCEPTION
            'Evidence Vault operation supersession timestamp transition is invalid';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER trg_b3s_vault_operation_plan_identity
ON b3s_history.evidence_vault_operation_plans;

CREATE TRIGGER trg_b3s_vault_operation_plan_identity
BEFORE UPDATE OR DELETE
ON b3s_history.evidence_vault_operation_plans
FOR EACH ROW
EXECUTE FUNCTION b3s_history.guard_vault_operation_plan_identity();

CREATE TRIGGER evidence_vault_operation_plans_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_vault_operation_plans
FOR EACH STATEMENT
EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();

COMMENT ON CONSTRAINT evidence_vault_relation_review_source_identity_fk
ON b3s_history.evidence_vault_operational_relation_reviews IS
    'A review is inseparable from the exact brand, source packet, and packet fingerprint it adjudicates.';
COMMENT ON CONSTRAINT evidence_vault_all_operational_packet_payload_check
ON b3s_history.evidence_vault_canonical_memory_packets IS
    'Every operational source, reviewed source, and memory packet stores its complete typed payload.';
