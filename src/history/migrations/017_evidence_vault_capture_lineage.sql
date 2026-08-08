-- Durable capture-sequence watermark and exact operational-source replay lineage.
--
-- This migration intentionally does not backfill captures created before it was
-- applied.  A legacy report capture may enter the chain only through the strict
-- historical-report seed/export replay repository path.

ALTER TABLE b3s_history.captures
    ADD CONSTRAINT captures_brand_id_id_key UNIQUE (brand_id, id);

ALTER TABLE b3s_history.evidence_records
    ADD CONSTRAINT evidence_records_capture_id_id_key UNIQUE (capture_id, id);

ALTER TABLE b3s_history.evidence_vault_canonical_memory_packets
    ADD CONSTRAINT evidence_vault_packets_brand_id_id_key UNIQUE (brand_id, id);

CREATE TABLE b3s_history.evidence_vault_capture_watermark_events (
    id uuid PRIMARY KEY,
    brand_id uuid NOT NULL
        REFERENCES b3s_history.brands(id) ON DELETE RESTRICT,
    capture_id uuid NOT NULL,
    capture_sequence bigint NOT NULL CHECK (capture_sequence > 0),
    previous_event_id uuid,
    previous_event_fingerprint text,
    capture_content_hash text NOT NULL CHECK (
        capture_content_hash ~ '^[0-9a-f]{64}$'
    ),
    capture_observation_hash text NOT NULL CHECK (
        capture_observation_hash ~ '^[0-9a-f]{64}$'
    ),
    append_origin text NOT NULL CHECK (
        append_origin IN (
            'capture_observation_commit',
            'report_derived_candidate_capture_replay'
        )
    ),
    lineage_export_identity text,
    lineage_export_fingerprint text CHECK (
        lineage_export_fingerprint IS NULL
        OR lineage_export_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    lineage_export_ordinal bigint CHECK (
        lineage_export_ordinal IS NULL OR lineage_export_ordinal > 0
    ),
    event_schema_version text NOT NULL DEFAULT
        'evidence-vault-capture-watermark-event-v1' CHECK (
            event_schema_version = 'evidence-vault-capture-watermark-event-v1'
        ),
    event_fingerprint text NOT NULL CHECK (
        event_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    authority boolean NOT NULL DEFAULT false CHECK (authority = false),
    production_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        production_runtime_effect = false
    ),
    scanner_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        scanner_runtime_effect = false
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (brand_id, id),
    UNIQUE (brand_id, capture_id),
    UNIQUE (brand_id, capture_sequence),
    UNIQUE (brand_id, previous_event_id),
    UNIQUE (brand_id, event_fingerprint),
    UNIQUE (brand_id, id, event_fingerprint),
    UNIQUE (brand_id, id, capture_id, capture_sequence),
    FOREIGN KEY (brand_id, capture_id)
        REFERENCES b3s_history.captures (brand_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (
        brand_id, previous_event_id, previous_event_fingerprint
    ) REFERENCES b3s_history.evidence_vault_capture_watermark_events (
        brand_id, id, event_fingerprint
    ) ON DELETE RESTRICT,
    CHECK (
        (capture_sequence = 1
            AND previous_event_id IS NULL
            AND previous_event_fingerprint IS NULL)
        OR (capture_sequence > 1
            AND previous_event_id IS NOT NULL
            AND previous_event_fingerprint IS NOT NULL)
    ),
    CHECK (
        (append_origin = 'capture_observation_commit'
            AND lineage_export_identity IS NULL
            AND lineage_export_fingerprint IS NULL
            AND lineage_export_ordinal IS NULL)
        OR (append_origin = 'report_derived_candidate_capture_replay'
            AND length(lineage_export_identity) BETWEEN 1 AND 300
            AND lineage_export_fingerprint IS NOT NULL
            AND lineage_export_ordinal = capture_sequence)
    )
);

CREATE INDEX idx_b3s_vault_capture_watermark_current
    ON b3s_history.evidence_vault_capture_watermark_events (
        brand_id, capture_sequence DESC
    );

CREATE TABLE b3s_history.evidence_vault_operational_source_capture_lineage_bindings (
    id uuid PRIMARY KEY,
    brand_id uuid NOT NULL
        REFERENCES b3s_history.brands(id) ON DELETE RESTRICT,
    operational_source_packet_id uuid NOT NULL,
    watermark_event_id uuid NOT NULL,
    capture_id uuid NOT NULL,
    capture_sequence bigint NOT NULL CHECK (capture_sequence > 0),
    provenance text NOT NULL CHECK (
        provenance = 'report_derived_candidate_capture'
    ),
    lineage_artifact_schema_version text NOT NULL CHECK (
        lineage_artifact_schema_version =
            'evidence-vault-lineage-seed-export-v2'
    ),
    lineage_artifact_fingerprint text NOT NULL CHECK (
        lineage_artifact_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    lineage_export_identity text NOT NULL CHECK (
        length(lineage_export_identity) BETWEEN 1 AND 300
    ),
    lineage_export_fingerprint text NOT NULL CHECK (
        lineage_export_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    replay_origin_sequence bigint NOT NULL CHECK (
        replay_origin_sequence > 0
        AND replay_origin_sequence <= capture_sequence
    ),
    member_set_fingerprint text NOT NULL CHECK (
        member_set_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    binding_fingerprint text NOT NULL CHECK (
        binding_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    authority boolean NOT NULL DEFAULT false CHECK (authority = false),
    production_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        production_runtime_effect = false
    ),
    scanner_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        scanner_runtime_effect = false
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (brand_id, id),
    UNIQUE (
        brand_id, id, capture_id, operational_source_packet_id
    ),
    UNIQUE (
        brand_id, operational_source_packet_id, capture_sequence
    ),
    UNIQUE (
        brand_id, operational_source_packet_id, watermark_event_id
    ),
    UNIQUE (brand_id, binding_fingerprint),
    FOREIGN KEY (brand_id, operational_source_packet_id)
        REFERENCES b3s_history.evidence_vault_canonical_memory_packets (
            brand_id, id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (
        brand_id, watermark_event_id, capture_id, capture_sequence
    ) REFERENCES b3s_history.evidence_vault_capture_watermark_events (
        brand_id, id, capture_id, capture_sequence
    ) ON DELETE RESTRICT
);

CREATE INDEX idx_b3s_vault_source_capture_lineage_current
    ON b3s_history.evidence_vault_operational_source_capture_lineage_bindings (
        brand_id, operational_source_packet_id, capture_sequence DESC
    );

CREATE TABLE b3s_history.evidence_vault_operational_source_capture_lineage_members (
    id uuid PRIMARY KEY,
    brand_id uuid NOT NULL
        REFERENCES b3s_history.brands(id) ON DELETE RESTRICT,
    binding_id uuid NOT NULL,
    operational_source_packet_id uuid NOT NULL,
    capture_id uuid NOT NULL,
    evidence_record_id uuid NOT NULL,
    composite_group_id text NOT NULL CHECK (
        composite_group_id ~ '^[0-9a-f]{64}$'
    ),
    relation_id text NOT NULL CHECK (relation_id ~ '^[0-9a-f]{64}$'),
    evidence_id text NOT NULL CHECK (evidence_id ~ '^[0-9a-f]{64}$'),
    source_identity_id text NOT NULL CHECK (
        source_identity_id ~ '^[0-9a-f]{64}$'
    ),
    evidence_fingerprint text NOT NULL CHECK (
        evidence_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    channel_role text NOT NULL CHECK (
        channel_role IN ('owned_web', 'external_social_profile')
    ),
    evidence_ref text NOT NULL CHECK (length(evidence_ref) BETWEEN 1 AND 500),
    source_ref text NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 1000),
    evidence_quote text NOT NULL CHECK (
        length(evidence_quote) BETWEEN 1 AND 20000
    ),
    member_fingerprint text NOT NULL CHECK (
        member_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    authority boolean NOT NULL DEFAULT false CHECK (authority = false),
    production_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        production_runtime_effect = false
    ),
    scanner_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        scanner_runtime_effect = false
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (brand_id, id),
    UNIQUE (binding_id, relation_id),
    UNIQUE (binding_id, evidence_record_id),
    UNIQUE (binding_id, member_fingerprint),
    FOREIGN KEY (
        brand_id, binding_id, capture_id, operational_source_packet_id
    ) REFERENCES b3s_history.evidence_vault_operational_source_capture_lineage_bindings (
        brand_id, id, capture_id, operational_source_packet_id
    ) ON DELETE RESTRICT,
    FOREIGN KEY (capture_id, evidence_record_id)
        REFERENCES b3s_history.evidence_records (capture_id, id)
        ON DELETE RESTRICT
);

CREATE FUNCTION b3s_history.validate_evidence_vault_capture_watermark_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    predecessor_sequence bigint;
BEGIN
    IF EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_capture_watermark_events AS existing
        WHERE existing.brand_id = NEW.brand_id
          AND existing.id = NEW.id
          AND existing.capture_id = NEW.capture_id
          AND existing.capture_sequence = NEW.capture_sequence
          AND existing.event_fingerprint = NEW.event_fingerprint
    ) THEN
        RETURN NEW;
    END IF;

    IF NEW.capture_sequence = 1 THEN
        IF EXISTS (
            SELECT 1
            FROM b3s_history.evidence_vault_capture_watermark_events AS existing
            WHERE existing.brand_id = NEW.brand_id
        ) THEN
            RAISE EXCEPTION
                'capture watermark sequence 1 requires an empty brand journal';
        END IF;
    ELSE
        SELECT previous.capture_sequence
        INTO predecessor_sequence
        FROM b3s_history.evidence_vault_capture_watermark_events AS previous
        WHERE previous.brand_id = NEW.brand_id
          AND previous.id = NEW.previous_event_id
          AND previous.event_fingerprint = NEW.previous_event_fingerprint;
        IF predecessor_sequence IS NULL
           OR predecessor_sequence <> NEW.capture_sequence - 1 THEN
            RAISE EXCEPTION
                'capture watermark predecessor must be sequence N-1';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION b3s_history.validate_evidence_vault_lineage_binding_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_capture_sequence bigint;
    prior_capture_sequence bigint;
    prior_origin_sequence bigint;
BEGIN
    IF EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_operational_source_capture_lineage_bindings
             AS existing
        WHERE existing.brand_id = NEW.brand_id
          AND existing.operational_source_packet_id =
              NEW.operational_source_packet_id
          AND existing.capture_sequence = NEW.capture_sequence
          AND existing.capture_id = NEW.capture_id
          AND existing.binding_fingerprint = NEW.binding_fingerprint
          AND existing.lineage_artifact_fingerprint =
              NEW.lineage_artifact_fingerprint
    ) THEN
        RETURN NEW;
    END IF;

    SELECT max(events.capture_sequence)
    INTO current_capture_sequence
    FROM b3s_history.evidence_vault_capture_watermark_events AS events
    WHERE events.brand_id = NEW.brand_id;
    IF current_capture_sequence IS NULL
       OR NEW.capture_sequence <> current_capture_sequence THEN
        RAISE EXCEPTION
            'a new lineage checkpoint must bind the current capture watermark';
    END IF;

    SELECT previous.capture_sequence, previous.replay_origin_sequence
    INTO prior_capture_sequence, prior_origin_sequence
    FROM b3s_history.evidence_vault_operational_source_capture_lineage_bindings
         AS previous
    WHERE previous.brand_id = NEW.brand_id
      AND previous.operational_source_packet_id =
          NEW.operational_source_packet_id
    ORDER BY previous.capture_sequence DESC
    LIMIT 1;

    IF prior_capture_sequence IS NULL THEN
        IF NEW.replay_origin_sequence <> NEW.capture_sequence THEN
            RAISE EXCEPTION
                'first lineage checkpoint origin must equal its capture sequence';
        END IF;
    ELSIF NEW.capture_sequence <> prior_capture_sequence + 1
          OR NEW.replay_origin_sequence <> prior_origin_sequence THEN
        RAISE EXCEPTION
            'lineage checkpoints must be contiguous with a stable origin';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER evidence_vault_capture_watermark_events_validate_insert
BEFORE INSERT
ON b3s_history.evidence_vault_capture_watermark_events
FOR EACH ROW
EXECUTE FUNCTION b3s_history.validate_evidence_vault_capture_watermark_insert();

CREATE TRIGGER evidence_vault_source_capture_lineage_bindings_validate_insert
BEFORE INSERT
ON b3s_history.evidence_vault_operational_source_capture_lineage_bindings
FOR EACH ROW
EXECUTE FUNCTION b3s_history.validate_evidence_vault_lineage_binding_insert();

CREATE FUNCTION b3s_history.reject_evidence_vault_capture_lineage_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'evidence Vault capture watermark and lineage records are append-only';
END;
$$;

CREATE TRIGGER evidence_vault_capture_watermark_events_append_only
BEFORE UPDATE OR DELETE
ON b3s_history.evidence_vault_capture_watermark_events
FOR EACH ROW
EXECUTE FUNCTION b3s_history.reject_evidence_vault_capture_lineage_mutation();

CREATE TRIGGER evidence_vault_source_capture_lineage_bindings_append_only
BEFORE UPDATE OR DELETE
ON b3s_history.evidence_vault_operational_source_capture_lineage_bindings
FOR EACH ROW
EXECUTE FUNCTION b3s_history.reject_evidence_vault_capture_lineage_mutation();

CREATE TRIGGER evidence_vault_source_capture_lineage_members_append_only
BEFORE UPDATE OR DELETE
ON b3s_history.evidence_vault_operational_source_capture_lineage_members
FOR EACH ROW
EXECUTE FUNCTION b3s_history.reject_evidence_vault_capture_lineage_mutation();

COMMENT ON TABLE b3s_history.evidence_vault_capture_watermark_events IS
    'Append-only, brand-serialized capture commit sequence. Caller timestamps are diagnostic only and never select the head.';

COMMENT ON TABLE b3s_history.evidence_vault_operational_source_capture_lineage_bindings IS
    'Non-authoritative replay checkpoints binding one exact operational source and capture watermark event. Readiness requires a contiguous sequence through the current head.';
