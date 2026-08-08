-- Harden the Vault capture and exact-source lineage journals against
-- direct SQL forgery, concurrent forks, parent mutation, and TRUNCATE bypasses.
-- This migration is additive because migration 017 may already be checksummed.

CREATE FUNCTION b3s_history.evidence_vault_canonical_json(value jsonb)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
DECLARE
    kind text;
    rendered text;
BEGIN
    kind := jsonb_typeof(value);
    IF kind = 'object' THEN
        SELECT '{' || COALESCE(
            string_agg(
                to_jsonb(entry.key)::text || ':' ||
                    b3s_history.evidence_vault_canonical_json(entry.value),
                ',' ORDER BY entry.key
            ),
            ''
        ) || '}'
        INTO rendered
        FROM jsonb_each(value) AS entry;
        RETURN rendered;
    ELSIF kind = 'array' THEN
        SELECT '[' || COALESCE(
            string_agg(
                b3s_history.evidence_vault_canonical_json(entry.value),
                ',' ORDER BY entry.ordinality
            ),
            ''
        ) || ']'
        INTO rendered
        FROM jsonb_array_elements(value) WITH ORDINALITY AS entry(value, ordinality);
        RETURN rendered;
    END IF;
    RETURN value::text;
END;
$$;

CREATE FUNCTION b3s_history.evidence_vault_canonical_fingerprint(
    namespace text,
    payload jsonb
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT encode(
        sha256(
            convert_to(
                b3s_history.evidence_vault_canonical_json(
                    jsonb_build_object(
                        'canonical_json_version',
                        'evidence-vault-canonical-json-v1',
                        'namespace', namespace,
                        'payload', payload
                    )
                ),
                'UTF8'
            )
        ),
        'hex'
    );
$$;

DO $$
DECLARE
    constraint_row record;
    definition text;
BEGIN
    FOR constraint_row IN
        SELECT constraints.conname,
               pg_get_constraintdef(constraints.oid) AS definition
        FROM pg_constraint AS constraints
        WHERE constraints.conrelid =
            'b3s_history.evidence_vault_operation_plans'::regclass
          AND constraints.contype = 'c'
    LOOP
        definition := constraint_row.definition;
        IF position('output_kind' IN definition) > 0
           AND position('no_delta' IN definition) > 0
           AND (
               position('completed_at' IN definition) > 0
               OR position('candidate_overlay' IN definition) > 0
           ) THEN
            EXECUTE format(
                'ALTER TABLE b3s_history.evidence_vault_operation_plans '
                'DROP CONSTRAINT %I',
                constraint_row.conname
            );
        END IF;
    END LOOP;
END;
$$;

ALTER TABLE b3s_history.evidence_vault_operation_plans
    ADD CONSTRAINT evidence_vault_operation_completed_output_check CHECK (
        status <> 'completed'
        OR (
            completed_at IS NOT NULL
            AND (
                candidate_packet_fingerprint IS NOT NULL
                OR result_payload ->> 'output_kind' IN (
                    'no_delta',
                    'material_delta_only'
                )
            )
        )
    ),
    ADD CONSTRAINT evidence_vault_operation_output_binding_check CHECK (
        (
            result_payload IS NULL
            AND candidate_packet_fingerprint IS NULL
        )
        OR (
            result_payload ->> 'output_kind' IN (
                'no_delta',
                'material_delta_only'
            )
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
    );

CREATE FUNCTION b3s_history.evidence_vault_brand_lock_key(
    workspace_id uuid,
    brand_id uuid
)
RETURNS bigint
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT (
        'x' || substr(
            encode(
                sha256(
                    convert_to(
                        workspace_id::text || ':brand:' || brand_id::text,
                        'UTF8'
                    )
                ),
                'hex'
            ),
            1,
            16
        )
    )::bit(64)::bigint;
$$;

CREATE FUNCTION b3s_history.validate_evidence_vault_packet_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    expected_fingerprint text;
    expected_resolution_fingerprint text;
    resolution_schema text;
    exact_artifact jsonb;
BEGIN
    IF NEW.packet_kind IN (
        'canonical_v1',
        'operational_source_v2',
        'operational_reviewed_v2'
    ) THEN
        IF NEW.schema_version IS DISTINCT FROM
                'evidence-vault-candidate-packet-v1'
           OR NEW.manifest ->> 'schema_version' IS DISTINCT FROM
                'evidence-vault-candidate-packet-v1' THEN
            RAISE EXCEPTION
                'evidence Vault candidate packet schema is invalid';
        END IF;
        expected_fingerprint :=
            b3s_history.evidence_vault_canonical_fingerprint(
                'evidence-vault-candidate-packet-fingerprint-v1',
                jsonb_build_object(
                    'manifest', NEW.manifest,
                    'candidate_tiles', NEW.candidate_tiles
                )
            );
        IF NEW.packet_fingerprint IS DISTINCT FROM expected_fingerprint THEN
            RAISE EXCEPTION
                'evidence Vault candidate packet fingerprint is invalid';
        END IF;
        IF NEW.packet_kind IN (
            'operational_source_v2',
            'operational_reviewed_v2'
        ) AND NEW.packet_payload IS DISTINCT FROM jsonb_build_object(
            'manifest', NEW.manifest,
            'candidate_tiles', NEW.candidate_tiles,
            'candidate_packet_fingerprint', NEW.packet_fingerprint
        ) THEN
            RAISE EXCEPTION
                'evidence Vault source packet columns differ from its payload';
        END IF;
    ELSIF NEW.packet_kind = 'operational_v2' THEN
        IF NEW.schema_version IS DISTINCT FROM
                'evidence-vault-operational-memory-packet-v2'
           OR NEW.packet_payload ->> 'schema_version' IS DISTINCT FROM
                'evidence-vault-operational-memory-packet-v2' THEN
            RAISE EXCEPTION
                'evidence Vault operational packet schema is invalid';
        END IF;
        expected_fingerprint :=
            b3s_history.evidence_vault_canonical_fingerprint(
                'evidence-vault-operational-memory-packet-v2',
                NEW.packet_payload - 'candidate_packet_fingerprint'
            );
        IF NEW.packet_fingerprint IS DISTINCT FROM expected_fingerprint
           OR NEW.packet_payload ->> 'candidate_packet_fingerprint'
                IS DISTINCT FROM NEW.packet_fingerprint THEN
            RAISE EXCEPTION
                'evidence Vault operational packet fingerprint is invalid';
        END IF;
    END IF;

    resolution_schema := NEW.reference_resolution ->> 'schema_version';
    IF NEW.packet_kind = 'operational_source_v2' THEN
        IF resolution_schema IS NULL OR resolution_schema NOT IN (
            'evidence-vault-operational-source-resolution-v1',
            'evidence-vault-exact-relation-source-resolution-v1',
            'evidence-vault-coverage-supplement-source-resolution-v1',
            'evidence-vault-composite-group-reopen-source-resolution-v1'
        ) THEN
            RAISE EXCEPTION
                'evidence Vault source resolution schema is invalid';
        END IF;
        expected_resolution_fingerprint :=
            b3s_history.evidence_vault_canonical_fingerprint(
                'evidence-vault-operational-source-resolution-v1',
                NEW.reference_resolution
            );
    ELSIF NEW.packet_kind = 'operational_reviewed_v2' THEN
        IF resolution_schema IS DISTINCT FROM
                'evidence-vault-operational-reviewed-resolution-v1' THEN
            RAISE EXCEPTION
                'evidence Vault reviewed resolution schema is invalid';
        END IF;
        expected_resolution_fingerprint :=
            b3s_history.evidence_vault_canonical_fingerprint(
                'evidence-vault-operational-reviewed-resolution-v1',
                NEW.reference_resolution
            );
    ELSIF NEW.packet_kind = 'operational_v2' THEN
        IF resolution_schema IS DISTINCT FROM
                'evidence-vault-operational-storage-resolution-v1' THEN
            RAISE EXCEPTION
                'evidence Vault storage resolution schema is invalid';
        END IF;
        expected_resolution_fingerprint :=
            b3s_history.evidence_vault_canonical_fingerprint(
                'evidence-vault-operational-storage-resolution-v1',
                NEW.reference_resolution - 'reference_resolution_fingerprint'
            );
        IF NEW.reference_resolution ->> 'reference_resolution_fingerprint'
            IS DISTINCT FROM NEW.reference_resolution_fingerprint THEN
            RAISE EXCEPTION
                'evidence Vault storage resolution identity is invalid';
        END IF;
    END IF;
    IF expected_resolution_fingerprint IS NOT NULL
       AND NEW.reference_resolution_fingerprint IS DISTINCT FROM
            expected_resolution_fingerprint THEN
        RAISE EXCEPTION
            'evidence Vault packet resolution fingerprint is invalid';
    END IF;

    IF NEW.packet_kind = 'operational_source_v2'
       AND NEW.reference_resolution ->> 'source_kind' =
            'exact_relation_supplement' THEN
        exact_artifact := NEW.reference_resolution -> 'artifact';
        IF exact_artifact ->> 'schema_version' IS DISTINCT FROM
                'evidence-vault-exact-relation-supplement-v1'
           OR exact_artifact ->> 'artifact_fingerprint' IS NULL
           OR exact_artifact ->> 'artifact_fingerprint' IS DISTINCT FROM
                NEW.reference_resolution ->> 'artifact_fingerprint'
           OR exact_artifact ->> 'artifact_fingerprint' IS DISTINCT FROM
                b3s_history.evidence_vault_canonical_fingerprint(
                    'evidence-vault-exact-relation-supplement-v1',
                    exact_artifact - 'artifact_fingerprint'
                ) THEN
            RAISE EXCEPTION
                'evidence Vault exact source artifact fingerprint is invalid';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER evidence_vault_packets_validate_insert
BEFORE INSERT
ON b3s_history.evidence_vault_canonical_memory_packets
FOR EACH ROW
EXECUTE FUNCTION b3s_history.validate_evidence_vault_packet_insert();

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_canonical_memory_packets AS packets
        WHERE (
            packets.packet_kind IN (
                'canonical_v1',
                'operational_source_v2',
                'operational_reviewed_v2'
            )
            AND (
                packets.schema_version IS DISTINCT FROM
                    'evidence-vault-candidate-packet-v1'
                OR packets.manifest ->> 'schema_version' IS DISTINCT FROM
                    'evidence-vault-candidate-packet-v1'
                OR packets.packet_fingerprint IS DISTINCT FROM
                    b3s_history.evidence_vault_canonical_fingerprint(
                        'evidence-vault-candidate-packet-fingerprint-v1',
                        jsonb_build_object(
                            'manifest', packets.manifest,
                            'candidate_tiles', packets.candidate_tiles
                        )
                    )
            )
        ) OR (
            packets.packet_kind IN (
                'operational_source_v2',
                'operational_reviewed_v2'
            )
            AND (
                packets.packet_payload IS DISTINCT FROM jsonb_build_object(
                    'manifest', packets.manifest,
                    'candidate_tiles', packets.candidate_tiles,
                    'candidate_packet_fingerprint', packets.packet_fingerprint
                )
                OR packets.reference_resolution ->> 'schema_version' IS NULL
                OR (
                    packets.packet_kind = 'operational_source_v2'
                    AND packets.reference_resolution ->> 'schema_version'
                        NOT IN (
                            'evidence-vault-operational-source-resolution-v1',
                            'evidence-vault-exact-relation-source-resolution-v1',
                            'evidence-vault-coverage-supplement-source-resolution-v1',
                            'evidence-vault-composite-group-reopen-source-resolution-v1'
                        )
                )
                OR (
                    packets.packet_kind = 'operational_reviewed_v2'
                    AND packets.reference_resolution ->> 'schema_version'
                        IS DISTINCT FROM
                            'evidence-vault-operational-reviewed-resolution-v1'
                )
                OR packets.reference_resolution_fingerprint IS DISTINCT FROM
                    b3s_history.evidence_vault_canonical_fingerprint(
                        CASE packets.packet_kind
                            WHEN 'operational_source_v2' THEN
                                'evidence-vault-operational-source-resolution-v1'
                            ELSE
                                'evidence-vault-operational-reviewed-resolution-v1'
                        END,
                        packets.reference_resolution
                    )
            )
        ) OR (
            packets.packet_kind = 'operational_v2'
            AND (
                packets.schema_version IS DISTINCT FROM
                    'evidence-vault-operational-memory-packet-v2'
                OR packets.packet_payload ->> 'schema_version'
                    IS DISTINCT FROM
                        'evidence-vault-operational-memory-packet-v2'
                OR packets.packet_payload ->> 'candidate_packet_fingerprint'
                    IS DISTINCT FROM packets.packet_fingerprint
                OR packets.packet_fingerprint IS DISTINCT FROM
                    b3s_history.evidence_vault_canonical_fingerprint(
                        'evidence-vault-operational-memory-packet-v2',
                        packets.packet_payload - 'candidate_packet_fingerprint'
                    )
                OR packets.reference_resolution ->> 'schema_version'
                    IS DISTINCT FROM
                        'evidence-vault-operational-storage-resolution-v1'
                OR packets.reference_resolution ->>
                    'reference_resolution_fingerprint' IS DISTINCT FROM
                    packets.reference_resolution_fingerprint
                OR packets.reference_resolution_fingerprint IS DISTINCT FROM
                    b3s_history.evidence_vault_canonical_fingerprint(
                        'evidence-vault-operational-storage-resolution-v1',
                        packets.reference_resolution -
                            'reference_resolution_fingerprint'
                    )
            )
        ) OR (
            packets.packet_kind = 'operational_source_v2'
            AND packets.reference_resolution ->> 'source_kind' =
                'exact_relation_supplement'
            AND (
                packets.reference_resolution #>> '{artifact,schema_version}'
                    IS DISTINCT FROM
                        'evidence-vault-exact-relation-supplement-v1'
                OR packets.reference_resolution #>>
                    '{artifact,artifact_fingerprint}' IS NULL
                OR packets.reference_resolution #>>
                    '{artifact,artifact_fingerprint}' IS DISTINCT FROM
                    packets.reference_resolution ->> 'artifact_fingerprint'
                OR packets.reference_resolution #>>
                    '{artifact,artifact_fingerprint}' IS DISTINCT FROM
                    b3s_history.evidence_vault_canonical_fingerprint(
                        'evidence-vault-exact-relation-supplement-v1',
                        (packets.reference_resolution -> 'artifact') -
                            'artifact_fingerprint'
                    )
            )
        )
    ) THEN
        RAISE EXCEPTION
            'existing Evidence Vault packet journal failed hardening validation';
    END IF;
END;
$$;

CREATE UNIQUE INDEX uq_b3s_vault_exact_source_artifact_per_brand
    ON b3s_history.evidence_vault_canonical_memory_packets (
        brand_id,
        (reference_resolution ->> 'artifact_fingerprint')
    )
    WHERE packet_kind = 'operational_source_v2'
      AND reference_resolution ->> 'source_kind' =
          'exact_relation_supplement'
      AND length(reference_resolution ->> 'artifact_fingerprint') = 64;

CREATE OR REPLACE FUNCTION b3s_history.validate_evidence_vault_capture_watermark_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    workspace_id uuid;
    predecessor_sequence bigint;
    expected_fingerprint text;
BEGIN
    SELECT brands.workspace_id
    INTO workspace_id
    FROM b3s_history.brands
    WHERE brands.id = NEW.brand_id;
    IF workspace_id IS NULL THEN
        RAISE EXCEPTION 'capture watermark brand does not exist';
    END IF;
    PERFORM pg_advisory_xact_lock(
        b3s_history.evidence_vault_brand_lock_key(workspace_id, NEW.brand_id)
    );

    expected_fingerprint := b3s_history.evidence_vault_canonical_fingerprint(
        'evidence-vault-capture-watermark-event-v1',
        jsonb_build_object(
            'schema_version', NEW.event_schema_version,
            'brand_id', NEW.brand_id::text,
            'capture_id', NEW.capture_id::text,
            'capture_sequence', NEW.capture_sequence,
            'previous_event_id',
                CASE
                    WHEN NEW.previous_event_id IS NULL THEN NULL
                    ELSE to_jsonb(NEW.previous_event_id::text)
                END,
            'previous_event_fingerprint',
                CASE
                    WHEN NEW.previous_event_fingerprint IS NULL THEN NULL
                    ELSE to_jsonb(NEW.previous_event_fingerprint)
                END,
            'capture_content_hash', NEW.capture_content_hash,
            'capture_observation_hash', NEW.capture_observation_hash,
            'append_origin', NEW.append_origin
        )
    );
    IF NEW.event_fingerprint <> expected_fingerprint THEN
        RAISE EXCEPTION 'capture watermark fingerprint does not match its content';
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

CREATE OR REPLACE FUNCTION b3s_history.validate_evidence_vault_lineage_binding_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    workspace_id uuid;
    current_capture_sequence bigint;
    prior_capture_sequence bigint;
    prior_origin_sequence bigint;
BEGIN
    SELECT brands.workspace_id
    INTO workspace_id
    FROM b3s_history.brands
    WHERE brands.id = NEW.brand_id;
    IF workspace_id IS NULL THEN
        RAISE EXCEPTION 'lineage binding brand does not exist';
    END IF;
    PERFORM pg_advisory_xact_lock(
        b3s_history.evidence_vault_brand_lock_key(workspace_id, NEW.brand_id)
    );

    IF EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_operational_source_capture_lineage_bindings
             AS existing
        WHERE existing.id = NEW.id
          AND existing.brand_id = NEW.brand_id
          AND existing.operational_source_packet_id =
                NEW.operational_source_packet_id
          AND existing.watermark_event_id = NEW.watermark_event_id
          AND existing.capture_id = NEW.capture_id
          AND existing.capture_sequence = NEW.capture_sequence
          AND existing.provenance = NEW.provenance
          AND existing.lineage_artifact_schema_version =
                NEW.lineage_artifact_schema_version
          AND existing.lineage_artifact_fingerprint =
                NEW.lineage_artifact_fingerprint
          AND existing.lineage_export_identity = NEW.lineage_export_identity
          AND existing.lineage_export_fingerprint =
                NEW.lineage_export_fingerprint
          AND existing.replay_origin_sequence = NEW.replay_origin_sequence
          AND existing.member_set_fingerprint = NEW.member_set_fingerprint
          AND existing.binding_fingerprint = NEW.binding_fingerprint
          AND existing.authority = NEW.authority
          AND existing.production_runtime_effect =
                NEW.production_runtime_effect
          AND existing.scanner_runtime_effect = NEW.scanner_runtime_effect
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

CREATE FUNCTION b3s_history.lock_evidence_vault_lineage_member_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    workspace_id uuid;
BEGIN
    SELECT brands.workspace_id
    INTO workspace_id
    FROM b3s_history.brands
    WHERE brands.id = NEW.brand_id;
    IF workspace_id IS NULL THEN
        RAISE EXCEPTION 'lineage member brand does not exist';
    END IF;
    PERFORM pg_advisory_xact_lock(
        b3s_history.evidence_vault_brand_lock_key(workspace_id, NEW.brand_id)
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER evidence_vault_source_capture_lineage_members_lock_insert
BEFORE INSERT
ON b3s_history.evidence_vault_operational_source_capture_lineage_members
FOR EACH ROW
EXECUTE FUNCTION b3s_history.lock_evidence_vault_lineage_member_insert();

CREATE FUNCTION b3s_history.validate_evidence_vault_capture_watermark_row()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    stored_capture_hash text;
    stored_observation_hash text;
    stored_brand_id uuid;
    expected_fingerprint text;
BEGIN
    SELECT captures.content_hash, scan_runs.metadata ->> 'observation_hash',
           captures.brand_id
    INTO stored_capture_hash, stored_observation_hash, stored_brand_id
    FROM b3s_history.captures
    JOIN b3s_history.scan_runs ON scan_runs.id = captures.scan_run_id
    WHERE captures.id = NEW.capture_id;

    IF stored_capture_hash IS NULL
       OR stored_observation_hash IS NULL
       OR stored_brand_id <> NEW.brand_id
       OR stored_capture_hash <> NEW.capture_content_hash
       OR stored_observation_hash <> NEW.capture_observation_hash THEN
        RAISE EXCEPTION
            'capture watermark is not bound to the durable capture observation';
    END IF;

    expected_fingerprint := b3s_history.evidence_vault_canonical_fingerprint(
        'evidence-vault-capture-watermark-event-v1',
        jsonb_build_object(
            'schema_version', NEW.event_schema_version,
            'brand_id', NEW.brand_id::text,
            'capture_id', NEW.capture_id::text,
            'capture_sequence', NEW.capture_sequence,
            'previous_event_id',
                CASE
                    WHEN NEW.previous_event_id IS NULL THEN NULL
                    ELSE to_jsonb(NEW.previous_event_id::text)
                END,
            'previous_event_fingerprint',
                CASE
                    WHEN NEW.previous_event_fingerprint IS NULL THEN NULL
                    ELSE to_jsonb(NEW.previous_event_fingerprint)
                END,
            'capture_content_hash', NEW.capture_content_hash,
            'capture_observation_hash', NEW.capture_observation_hash,
            'append_origin', NEW.append_origin
        )
    );
    IF expected_fingerprint <> NEW.event_fingerprint THEN
        RAISE EXCEPTION 'capture watermark fingerprint is invalid';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER evidence_vault_capture_watermark_row_validate
AFTER INSERT
ON b3s_history.evidence_vault_capture_watermark_events
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION b3s_history.validate_evidence_vault_capture_watermark_row();

CREATE FUNCTION b3s_history.validate_evidence_vault_lineage_binding_content(
    p_binding_id uuid
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    binding record;
    packet record;
    watermark record;
    brand_domain text;
    exact_groups jsonb;
    exact_group jsonb;
    members_payload jsonb;
    expected_member_set_fingerprint text;
    expected_binding_fingerprint text;
    member_count integer;
    distinct_group_count integer;
    role_count integer;
BEGIN
    SELECT *
    INTO binding
    FROM b3s_history.evidence_vault_operational_source_capture_lineage_bindings
    WHERE id = p_binding_id;
    IF binding.id IS NULL THEN
        RAISE EXCEPTION 'lineage binding disappeared before validation';
    END IF;

    SELECT packets.packet_kind, packets.packet_fingerprint,
           packets.reference_resolution, packets.brand_id
    INTO packet
    FROM b3s_history.evidence_vault_canonical_memory_packets AS packets
    WHERE packets.id = binding.operational_source_packet_id;

    SELECT events.event_fingerprint, events.capture_id,
           events.capture_sequence, events.brand_id
    INTO watermark
    FROM b3s_history.evidence_vault_capture_watermark_events AS events
    WHERE events.id = binding.watermark_event_id;

    SELECT brands.canonical_domain
    INTO brand_domain
    FROM b3s_history.brands
    WHERE brands.id = binding.brand_id;

    IF packet.packet_kind IS DISTINCT FROM 'operational_source_v2'
       OR packet.brand_id IS DISTINCT FROM binding.brand_id
       OR packet.reference_resolution ->> 'source_kind'
            IS DISTINCT FROM 'exact_relation_supplement'
       OR watermark.brand_id IS DISTINCT FROM binding.brand_id
       OR watermark.capture_id IS DISTINCT FROM binding.capture_id
       OR watermark.capture_sequence IS DISTINCT FROM binding.capture_sequence THEN
        RAISE EXCEPTION
            'lineage binding is not attached to one exact source and watermark';
    END IF;

    SELECT jsonb_agg(groups.value ORDER BY groups.ordinality)
    INTO exact_groups
    FROM jsonb_array_elements(
        COALESCE(
            packet.reference_resolution #> '{artifact,groups}',
            '[]'::jsonb
        )
    ) WITH ORDINALITY AS groups(value, ordinality)
    WHERE groups.value ->> 'tile_id' = 'C7';

    IF exact_groups IS NULL OR jsonb_array_length(exact_groups) <> 1 THEN
        RAISE EXCEPTION 'lineage exact source must contain one C7 group';
    END IF;
    exact_group := exact_groups -> 0;
    IF exact_group ->> 'decision_rule' IS DISTINCT FROM 'all_of'
       OR jsonb_array_length(COALESCE(exact_group -> 'relations', '[]'::jsonb)) <> 2
       OR exact_group ->> 'group_id' IS NULL THEN
        RAISE EXCEPTION 'lineage C7 group contract is incomplete';
    END IF;

    SELECT count(*), count(DISTINCT members.composite_group_id),
           count(DISTINCT members.channel_role),
           jsonb_agg(
               jsonb_build_object(
                   'group_id', members.composite_group_id,
                   'relation_id', members.relation_id,
                   'evidence_id', members.evidence_id,
                   'source_identity_id', members.source_identity_id,
                   'evidence_fingerprint', members.evidence_fingerprint,
                   'channel_role', members.channel_role,
                   'evidence_ref', members.evidence_ref,
                   'source_ref', members.source_ref,
                   'evidence_quote', members.evidence_quote
               ) ORDER BY members.relation_id
           )
    INTO member_count, distinct_group_count, role_count, members_payload
    FROM b3s_history.evidence_vault_operational_source_capture_lineage_members
         AS members
    WHERE members.binding_id = binding.id;

    IF member_count <> 2 OR distinct_group_count <> 1 OR role_count <> 2 THEN
        RAISE EXCEPTION
            'lineage binding requires exactly two independent C7 members';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_operational_source_capture_lineage_members
             AS members
        JOIN b3s_history.evidence_records AS evidence
          ON evidence.id = members.evidence_record_id
         AND evidence.capture_id = members.capture_id
        LEFT JOIN LATERAL (
            SELECT relations.value
            FROM jsonb_array_elements(exact_group -> 'relations') AS relations(value)
            WHERE relations.value ->> 'relation_id' = members.relation_id
        ) AS relation ON true
        WHERE members.binding_id = binding.id
          AND (
              members.brand_id IS DISTINCT FROM binding.brand_id
              OR members.operational_source_packet_id IS DISTINCT FROM
                    binding.operational_source_packet_id
              OR members.capture_id IS DISTINCT FROM binding.capture_id
              OR members.composite_group_id IS DISTINCT FROM
                    exact_group ->> 'group_id'
              OR relation.value IS NULL
              OR members.evidence_fingerprint IS DISTINCT FROM
                    relation.value ->> 'evidence_fingerprint'
              OR members.evidence_id IS DISTINCT FROM
                    relation.value ->> 'evidence_id'
              OR members.source_identity_id IS DISTINCT FROM
                    relation.value ->> 'source_identity_id'
              OR members.channel_role IS DISTINCT FROM
                    relation.value ->> 'channel_role'
              OR members.evidence_ref IS DISTINCT FROM
                    relation.value ->> 'ref'
              OR members.source_ref IS DISTINCT FROM
                    relation.value ->> 'ref'
              OR members.evidence_quote IS DISTINCT FROM
                    relation.value ->> 'literal_quote'
              OR evidence.evidence_ref IS DISTINCT FROM members.evidence_ref
              OR evidence.url IS DISTINCT FROM relation.value ->> 'url'
              OR evidence.source_class IS DISTINCT FROM
                    relation.value ->> 'source_class'
              OR position(members.evidence_quote IN evidence.content) = 0
          )
    ) THEN
        RAISE EXCEPTION
            'lineage member does not match its exact source or capture evidence';
    END IF;

    IF (SELECT array_agg(channel_role ORDER BY channel_role)
        FROM b3s_history.evidence_vault_operational_source_capture_lineage_members
        WHERE binding_id = binding.id)
       IS DISTINCT FROM ARRAY['external_social_profile', 'owned_web']::text[] THEN
        RAISE EXCEPTION 'lineage member channel roles are invalid';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_operational_source_capture_lineage_members
             AS members
        WHERE members.binding_id = binding.id
          AND members.member_fingerprint <>
              b3s_history.evidence_vault_canonical_fingerprint(
                  'evidence-vault-source-capture-lineage-member-v1',
                  jsonb_build_object(
                      'group_id', members.composite_group_id,
                      'relation_id', members.relation_id,
                      'evidence_id', members.evidence_id,
                      'source_identity_id', members.source_identity_id,
                      'evidence_fingerprint', members.evidence_fingerprint,
                      'channel_role', members.channel_role,
                      'evidence_ref', members.evidence_ref,
                      'source_ref', members.source_ref,
                      'evidence_quote', members.evidence_quote
                  )
              )
    ) THEN
        RAISE EXCEPTION 'lineage member fingerprint is invalid';
    END IF;

    expected_member_set_fingerprint :=
        b3s_history.evidence_vault_canonical_fingerprint(
            'evidence-vault-source-capture-lineage-member-set-v1',
            members_payload
        );
    IF binding.member_set_fingerprint <> expected_member_set_fingerprint THEN
        RAISE EXCEPTION 'lineage member-set fingerprint is invalid';
    END IF;

    expected_binding_fingerprint :=
        b3s_history.evidence_vault_canonical_fingerprint(
            'evidence-vault-source-capture-lineage-binding-v1',
            jsonb_build_object(
                'schema_version',
                    'evidence-vault-source-capture-lineage-binding-v1',
                'brand_identity', brand_domain,
                'source_candidate_packet_fingerprint', packet.packet_fingerprint,
                'watermark_event_fingerprint', watermark.event_fingerprint,
                'capture_id', binding.capture_id::text,
                'capture_sequence', binding.capture_sequence,
                'provenance', binding.provenance,
                'lineage_artifact_fingerprint',
                    binding.lineage_artifact_fingerprint,
                'lineage_export_identity', binding.lineage_export_identity,
                'lineage_export_fingerprint', binding.lineage_export_fingerprint,
                'replay_origin_sequence', binding.replay_origin_sequence,
                'member_set_fingerprint', binding.member_set_fingerprint
            )
        );
    IF binding.binding_fingerprint <> expected_binding_fingerprint THEN
        RAISE EXCEPTION 'lineage binding fingerprint is invalid';
    END IF;
    RETURN;
END;
$$;


CREATE FUNCTION b3s_history.validate_evidence_vault_lineage_binding_row()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM b3s_history.validate_evidence_vault_lineage_binding_content(NEW.id);
    RETURN NEW;
END;
$$;

CREATE FUNCTION b3s_history.validate_evidence_vault_lineage_member_set()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM b3s_history.validate_evidence_vault_lineage_binding_content(
        COALESCE(NEW.binding_id, OLD.binding_id)
    );
    RETURN COALESCE(NEW, OLD);
END;
$$;

DO $$
DECLARE
    stored_binding record;
BEGIN
    IF EXISTS (
        WITH ordered AS (
            SELECT events.*,
                   row_number() OVER (
                       PARTITION BY events.brand_id
                       ORDER BY events.capture_sequence
                   ) AS expected_sequence,
                   lag(events.id) OVER (
                       PARTITION BY events.brand_id
                       ORDER BY events.capture_sequence
                   ) AS expected_previous_id,
                   lag(events.event_fingerprint) OVER (
                       PARTITION BY events.brand_id
                       ORDER BY events.capture_sequence
                   ) AS expected_previous_fingerprint
            FROM b3s_history.evidence_vault_capture_watermark_events AS events
        )
        SELECT 1
        FROM ordered
        JOIN b3s_history.captures
          ON captures.id = ordered.capture_id
         AND captures.brand_id = ordered.brand_id
        JOIN b3s_history.scan_runs
          ON scan_runs.id = captures.scan_run_id
        WHERE ordered.capture_sequence <> ordered.expected_sequence
           OR ordered.previous_event_id IS DISTINCT FROM
                ordered.expected_previous_id
           OR ordered.previous_event_fingerprint IS DISTINCT FROM
                ordered.expected_previous_fingerprint
           OR ordered.capture_content_hash IS DISTINCT FROM
                captures.content_hash
           OR ordered.capture_observation_hash IS DISTINCT FROM
                scan_runs.metadata ->> 'observation_hash'
           OR ordered.event_fingerprint IS DISTINCT FROM
                b3s_history.evidence_vault_canonical_fingerprint(
                    'evidence-vault-capture-watermark-event-v1',
                    jsonb_build_object(
                        'schema_version', ordered.event_schema_version,
                        'brand_id', ordered.brand_id::text,
                        'capture_id', ordered.capture_id::text,
                        'capture_sequence', ordered.capture_sequence,
                        'previous_event_id',
                            CASE
                                WHEN ordered.previous_event_id IS NULL THEN NULL
                                ELSE to_jsonb(ordered.previous_event_id::text)
                            END,
                        'previous_event_fingerprint',
                            CASE
                                WHEN ordered.previous_event_fingerprint IS NULL
                                    THEN NULL
                                ELSE to_jsonb(
                                    ordered.previous_event_fingerprint
                                )
                            END,
                        'capture_content_hash', ordered.capture_content_hash,
                        'capture_observation_hash',
                            ordered.capture_observation_hash,
                        'append_origin', ordered.append_origin
                    )
                )
    ) THEN
        RAISE EXCEPTION
            'existing capture watermark journal failed hardening validation';
    END IF;

    FOR stored_binding IN
        SELECT id
        FROM b3s_history.evidence_vault_operational_source_capture_lineage_bindings
        ORDER BY brand_id, capture_sequence, id
    LOOP
        PERFORM b3s_history.validate_evidence_vault_lineage_binding_content(
            stored_binding.id
        );
    END LOOP;
END;
$$;

CREATE CONSTRAINT TRIGGER evidence_vault_lineage_binding_row_validate
AFTER INSERT
ON b3s_history.evidence_vault_operational_source_capture_lineage_bindings
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION b3s_history.validate_evidence_vault_lineage_binding_row();

CREATE CONSTRAINT TRIGGER evidence_vault_lineage_member_set_validate
AFTER INSERT OR UPDATE OR DELETE
ON b3s_history.evidence_vault_operational_source_capture_lineage_members
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION b3s_history.validate_evidence_vault_lineage_member_set();

CREATE FUNCTION b3s_history.protect_evidence_vault_capture_parent()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD IS DISTINCT FROM NEW THEN
        RAISE EXCEPTION 'capture is immutable lineage input';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER evidence_vault_capture_parent_immutable
BEFORE UPDATE
ON b3s_history.captures
FOR EACH ROW
EXECUTE FUNCTION b3s_history.protect_evidence_vault_capture_parent();

CREATE FUNCTION b3s_history.protect_evidence_vault_scan_parent()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.workspace_id IS DISTINCT FROM NEW.workspace_id
       OR OLD.brand_id IS DISTINCT FROM NEW.brand_id
       OR OLD.source_scan_id IS DISTINCT FROM NEW.source_scan_id
       OR OLD.request_payload IS DISTINCT FROM NEW.request_payload
       OR OLD.metadata ->> 'observation_hash'
            IS DISTINCT FROM NEW.metadata ->> 'observation_hash' THEN
        RAISE EXCEPTION
            'scan identity, request, and observation hash are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER evidence_vault_scan_parent_immutable
BEFORE UPDATE
ON b3s_history.scan_runs
FOR EACH ROW
EXECUTE FUNCTION b3s_history.protect_evidence_vault_scan_parent();

CREATE FUNCTION b3s_history.protect_evidence_vault_evidence_parent()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    parent_brand_id uuid;
    parent_workspace_id uuid;
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.capture_id IS DISTINCT FROM NEW.capture_id THEN
        RAISE EXCEPTION 'capture evidence cannot change its capture parent';
    END IF;
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'capture evidence is immutable';
    END IF;
    SELECT captures.brand_id, brands.workspace_id
    INTO parent_brand_id, parent_workspace_id
    FROM b3s_history.captures
    JOIN b3s_history.brands ON brands.id = captures.brand_id
    WHERE captures.id = NEW.capture_id;
    IF parent_workspace_id IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(
            b3s_history.evidence_vault_brand_lock_key(
                parent_workspace_id,
                parent_brand_id
            )
        );
    END IF;
    IF EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_capture_watermark_events AS events
        WHERE events.capture_id = NEW.capture_id
    ) THEN
        RAISE EXCEPTION
            'watermarked capture evidence is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER evidence_vault_evidence_parent_immutable
BEFORE INSERT OR UPDATE OR DELETE
ON b3s_history.evidence_records
FOR EACH ROW
EXECUTE FUNCTION b3s_history.protect_evidence_vault_evidence_parent();

CREATE FUNCTION b3s_history.protect_evidence_vault_brand_parent()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.workspace_id IS DISTINCT FROM NEW.workspace_id
       OR OLD.canonical_domain IS DISTINCT FROM NEW.canonical_domain THEN
        RAISE EXCEPTION 'brand workspace and canonical domain are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER evidence_vault_brand_parent_immutable
BEFORE UPDATE
ON b3s_history.brands
FOR EACH ROW
EXECUTE FUNCTION b3s_history.protect_evidence_vault_brand_parent();

CREATE FUNCTION b3s_history.reject_evidence_vault_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'append-only Evidence Vault journals cannot be truncated';
END;
$$;

CREATE TRIGGER evidence_claim_tile_review_packets_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_claim_tile_review_packets
FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();

CREATE TRIGGER evidence_vault_canonical_packets_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_vault_canonical_memory_packets
FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();

CREATE TRIGGER evidence_vault_canonical_promotions_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_vault_canonical_memory_promotion_events
FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();

CREATE TRIGGER evidence_vault_canonical_scores_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_vault_canonical_score_evaluations
FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();

CREATE TRIGGER evidence_scoring_recovery_supplements_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_scoring_recovery_supplement_packets
FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();

CREATE TRIGGER evidence_vault_relation_reviews_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_vault_operational_relation_reviews
FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();

CREATE TRIGGER evidence_vault_capture_watermarks_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_vault_capture_watermark_events
FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();

CREATE TRIGGER evidence_vault_lineage_bindings_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_vault_operational_source_capture_lineage_bindings
FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();

CREATE TRIGGER evidence_vault_lineage_members_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_vault_operational_source_capture_lineage_members
FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();
