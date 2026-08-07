ALTER TABLE b3s_history.evidence_vault_canonical_memory_packets
    ADD COLUMN packet_kind text NOT NULL DEFAULT 'canonical_v1' CHECK (
        packet_kind IN ('canonical_v1', 'operational_source_v2', 'operational_reviewed_v2', 'operational_v2')
    ),
    ADD COLUMN packet_payload jsonb CHECK (
        packet_payload IS NULL OR jsonb_typeof(packet_payload) = 'object'
    ),
    ADD COLUMN accepted_memory_candidate_version text CHECK (
        accepted_memory_candidate_version IS NULL
        OR accepted_memory_candidate_version ~ '^[0-9a-f]{64}$'
    ),
    ADD COLUMN candidate_overlay_version text CHECK (
        candidate_overlay_version IS NULL
        OR candidate_overlay_version ~ '^[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT evidence_vault_operational_packet_fields CHECK (
        packet_kind <> 'operational_v2'
        OR (
            packet_payload IS NOT NULL
            AND accepted_memory_candidate_version IS NOT NULL
            AND candidate_overlay_version IS NOT NULL
        )
    );

ALTER TABLE b3s_history.evidence_vault_canonical_memory_promotion_events
    ADD COLUMN adoption_kind text NOT NULL DEFAULT 'human_promotion_v1' CHECK (
        adoption_kind IN ('human_promotion_v1', 'operational_v2')
    ),
    ADD COLUMN adopted_by text CHECK (
        adopted_by IS NULL OR adopted_by IN ('policy', 'human')
    ),
    ADD COLUMN actor_id text CHECK (
        actor_id IS NULL OR length(actor_id) BETWEEN 1 AND 200
    ),
    ADD COLUMN event_payload jsonb CHECK (
        event_payload IS NULL OR jsonb_typeof(event_payload) = 'object'
    ),
    ADD CONSTRAINT evidence_vault_operational_adoption_fields CHECK (
        adoption_kind <> 'operational_v2'
        OR (
            adopted_by IS NOT NULL
            AND actor_id IS NOT NULL
            AND event_payload IS NOT NULL
        )
    );

DO $$
DECLARE
    sequence_constraint text;
BEGIN
    SELECT constraint_name
    INTO sequence_constraint
    FROM information_schema.table_constraints
    WHERE table_schema = 'b3s_history'
      AND table_name = 'evidence_vault_canonical_memory_promotion_events'
      AND constraint_type = 'UNIQUE'
      AND constraint_name IN (
          SELECT conname
          FROM pg_constraint
          WHERE conrelid = (
              'b3s_history.evidence_vault_canonical_memory_promotion_events'
          )::regclass
            AND contype = 'u'
            AND pg_get_constraintdef(oid) = 'UNIQUE (brand_id, sequence)'
      );
    IF sequence_constraint IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE b3s_history.evidence_vault_canonical_memory_promotion_events DROP CONSTRAINT %I',
            sequence_constraint
        );
    END IF;
END
$$;

CREATE UNIQUE INDEX uq_b3s_vault_v1_promotion_sequence
    ON b3s_history.evidence_vault_canonical_memory_promotion_events (
        brand_id, sequence
    )
    WHERE adoption_kind = 'human_promotion_v1';

CREATE UNIQUE INDEX uq_b3s_vault_v2_adoption_sequence
    ON b3s_history.evidence_vault_canonical_memory_promotion_events (
        brand_id, sequence
    )
    WHERE adoption_kind = 'operational_v2';

ALTER TABLE b3s_history.evidence_vault_canonical_score_evaluations
    ADD COLUMN evaluation_kind text NOT NULL DEFAULT 'canonical_v1' CHECK (
        evaluation_kind IN ('canonical_v1', 'operational_v2')
    ),
    ADD COLUMN authority_coverage jsonb CHECK (
        authority_coverage IS NULL
        OR jsonb_typeof(authority_coverage) = 'object'
    ),
    ADD COLUMN evaluation_payload jsonb CHECK (
        evaluation_payload IS NULL
        OR jsonb_typeof(evaluation_payload) = 'object'
    ),
    ADD CONSTRAINT evidence_vault_operational_evaluation_fields CHECK (
        evaluation_kind <> 'operational_v2'
        OR (authority_coverage IS NOT NULL AND evaluation_payload IS NOT NULL)
    );

CREATE INDEX idx_b3s_vault_operational_packets_created
    ON b3s_history.evidence_vault_canonical_memory_packets (
        brand_id, created_at DESC, id DESC
    )
    WHERE packet_kind = 'operational_v2';

CREATE INDEX idx_b3s_vault_operational_adoptions_current
    ON b3s_history.evidence_vault_canonical_memory_promotion_events (
        brand_id, sequence DESC
    )
    WHERE adoption_kind = 'operational_v2';

COMMENT ON COLUMN b3s_history.evidence_vault_canonical_memory_packets.packet_payload IS
    'Exact immutable v2 accepted-memory plus candidate-overlay packet. It reuses the canonical packet store rather than creating another ledger.';

COMMENT ON COLUMN b3s_history.evidence_vault_canonical_memory_promotion_events.event_payload IS
    'Exact append-only v2 adoption event. Accepted authority is derived from this journal; candidate packets remain immutable and non-authoritative.';

COMMENT ON COLUMN b3s_history.evidence_vault_canonical_score_evaluations.evaluation_payload IS
    'Exact immutable v2 deterministic evaluation including authority coverage and calculation reuse identity.';
