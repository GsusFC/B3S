-- Harden migration 025's non-authoritative SV9 shadow ledger without
-- changing its storage-only boundary.  PostgreSQL validates structural ledger
-- integrity here; it deliberately does not claim to reproduce the Python SV9
-- kernel's canonical JSON/fingerprint contract.  A future trusted writer must
-- first run validate_sv9_assessment_output() and independently rederive the
-- operational semantic shadow before it attempts an insert.

CREATE OR REPLACE FUNCTION b3s_history.evidence_vault_sv9_shadow_semantic_tiles_are_valid(
    tiles jsonb
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT jsonb_typeof(tiles) IS NOT DISTINCT FROM 'array'
       AND jsonb_array_length(tiles) IS NOT DISTINCT FROM 80
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(tiles) AS rows(value)
           WHERE jsonb_typeof(rows.value) IS DISTINCT FROM 'object'
              OR NOT b3s_history.evidence_vault_json_has_exact_keys(
                  rows.value,
                  ARRAY['component_key', 'tile_id', 'tile_key', 'assessment_state']
              )
              OR jsonb_typeof(rows.value -> 'component_key')
                    IS DISTINCT FROM 'string'
              OR jsonb_typeof(rows.value -> 'tile_id') IS DISTINCT FROM 'string'
              OR jsonb_typeof(rows.value -> 'tile_key') IS DISTINCT FROM 'string'
              OR jsonb_typeof(rows.value -> 'assessment_state')
                    IS DISTINCT FROM 'string'
              OR NOT (
                  (rows.value ->> 'assessment_state') IS NOT DISTINCT FROM 'ok'
                  OR (rows.value ->> 'assessment_state') IS NOT DISTINCT FROM 'no'
                  OR (rows.value ->> 'assessment_state') IS NOT DISTINCT FROM 'sin_evidencia'
                  OR (rows.value ->> 'assessment_state') IS NOT DISTINCT FROM 'contradiction'
              )
       )
       AND (
           SELECT count(DISTINCT rows.value ->> 'tile_id')
           FROM jsonb_array_elements(tiles) AS rows(value)
       ) IS NOT DISTINCT FROM 80;
$$;

CREATE OR REPLACE FUNCTION b3s_history.evidence_vault_sv9_shadow_verification_is_valid(
    requirements jsonb
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT jsonb_typeof(requirements) IS NOT DISTINCT FROM 'object'
       AND b3s_history.evidence_vault_json_has_exact_keys(
           requirements, ARRAY['schema_version', 'tile_count', 'tiles', 'counts']
       )
       AND (requirements ->> 'schema_version') IS NOT DISTINCT FROM
           'evidence-vault-operational-verification-requirements-v1'
       AND jsonb_typeof(requirements -> 'tile_count') IS NOT DISTINCT FROM 'number'
       AND (requirements ->> 'tile_count') IS NOT DISTINCT FROM '80'
       AND jsonb_typeof(requirements -> 'tiles') IS NOT DISTINCT FROM 'array'
       AND jsonb_array_length(requirements -> 'tiles') IS NOT DISTINCT FROM 80
       AND jsonb_typeof(requirements -> 'counts') IS NOT DISTINCT FROM 'object'
       AND b3s_history.evidence_vault_json_has_exact_keys(
           requirements -> 'counts',
           ARRAY['pending', 'verified', 'disputed', 'stale', 'unverifiable']
       )
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(requirements -> 'tiles') AS rows(value)
           WHERE jsonb_typeof(rows.value) IS DISTINCT FROM 'object'
              OR NOT b3s_history.evidence_vault_json_has_exact_keys(
                  rows.value,
                  ARRAY['tile_id', 'verification_requirement', 'verification_state']
              )
              OR jsonb_typeof(rows.value -> 'tile_id') IS DISTINCT FROM 'string'
              OR jsonb_typeof(rows.value -> 'verification_requirement')
                    IS DISTINCT FROM 'string'
              OR jsonb_typeof(rows.value -> 'verification_state')
                    IS DISTINCT FROM 'string'
              OR NOT (
                  (rows.value ->> 'verification_requirement') IS NOT DISTINCT FROM 'ordinary'
                  OR (rows.value ->> 'verification_requirement')
                        IS NOT DISTINCT FROM 'owned_web_plus_external_social'
                  OR (rows.value ->> 'verification_requirement')
                        IS NOT DISTINCT FROM 'human_required'
              )
              OR NOT (
                  (rows.value ->> 'verification_state') IS NOT DISTINCT FROM 'pending'
                  OR (rows.value ->> 'verification_state') IS NOT DISTINCT FROM 'disputed'
                  OR (rows.value ->> 'verification_state') IS NOT DISTINCT FROM 'stale'
                  OR (rows.value ->> 'verification_state') IS NOT DISTINCT FROM 'unverifiable'
              )
       )
       AND (
           SELECT count(DISTINCT rows.value ->> 'tile_id')
           FROM jsonb_array_elements(requirements -> 'tiles') AS rows(value)
       ) IS NOT DISTINCT FROM 80
       -- The adapter emits the stable `verified` count key for compatibility,
       -- but it has no evidence-verification binding and can never set it.
       AND (requirements -> 'counts' ->> 'verified') IS NOT DISTINCT FROM '0'
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_each(requirements -> 'counts') AS counts(key, value)
           WHERE jsonb_typeof(counts.value) IS DISTINCT FROM 'number'
              OR counts.value #>> '{}' !~ '^(0|[1-9][0-9]*)$'
              OR (counts.value #>> '{}') IS DISTINCT FROM (
                  SELECT count(*)::text
                  FROM jsonb_array_elements(requirements -> 'tiles') AS rows(value)
                  WHERE rows.value ->> 'verification_state' IS NOT DISTINCT FROM counts.key
              )
       );
$$;

CREATE FUNCTION b3s_history.evidence_vault_sv9_shadow_assessment_output_is_structurally_valid(
    assessment_output jsonb
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT jsonb_typeof(assessment_output) IS NOT DISTINCT FROM 'object'
       AND b3s_history.evidence_vault_json_has_exact_keys(
           assessment_output,
           ARRAY[
               'schema_version', 'rubric_version', 'assessment_vector_version',
               'scoring_policy_version', 'tile_contract_registry_fingerprint',
               'tile_count', 'tiles', 'sv9_score', 'component_breakdown',
               'base_average', 'magnetism_capped', 'assessment_fingerprint',
               'score_fingerprint'
           ]
       )
       AND (assessment_output ->> 'schema_version') IS NOT DISTINCT FROM
           'sv9-assessment-output-v1'
       AND (assessment_output ->> 'rubric_version') IS NOT DISTINCT FROM 'baldosas-v3-1'
       AND (assessment_output ->> 'assessment_vector_version') IS NOT DISTINCT FROM
           'sv9-assessment-vector-v1'
       AND (assessment_output ->> 'scoring_policy_version') IS NOT DISTINCT FROM
           'sv9-scoring-policy-baldosas-v3-1-v1'
       AND jsonb_typeof(assessment_output -> 'tile_contract_registry_fingerprint')
            IS NOT DISTINCT FROM 'string'
       AND (assessment_output ->> 'tile_contract_registry_fingerprint') ~ '^[0-9a-f]{64}$'
       AND jsonb_typeof(assessment_output -> 'tile_count') IS NOT DISTINCT FROM 'number'
       AND (assessment_output ->> 'tile_count') IS NOT DISTINCT FROM '80'
       AND b3s_history.evidence_vault_sv9_shadow_semantic_tiles_are_valid(
           assessment_output -> 'tiles'
       )
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(assessment_output -> 'tiles') AS rows(value)
           WHERE (rows.value ->> 'assessment_state') IS NOT DISTINCT FROM 'contradiction'
       )
       AND jsonb_typeof(assessment_output -> 'sv9_score') IS NOT DISTINCT FROM 'number'
       AND (assessment_output ->> 'sv9_score') ~ '^(0|[1-9][0-9]*)$'
       AND jsonb_typeof(assessment_output -> 'base_average') IS NOT DISTINCT FROM 'number'
       AND jsonb_typeof(assessment_output -> 'magnetism_capped')
            IS NOT DISTINCT FROM 'boolean'
       AND jsonb_typeof(assessment_output -> 'assessment_fingerprint')
            IS NOT DISTINCT FROM 'string'
       AND (assessment_output ->> 'assessment_fingerprint') ~ '^[0-9a-f]{64}$'
       AND jsonb_typeof(assessment_output -> 'score_fingerprint')
            IS NOT DISTINCT FROM 'string'
       AND (assessment_output ->> 'score_fingerprint') ~ '^[0-9a-f]{64}$'
       AND jsonb_typeof(assessment_output -> 'component_breakdown')
            IS NOT DISTINCT FROM 'array'
       AND jsonb_array_length(assessment_output -> 'component_breakdown')
            IS NOT DISTINCT FROM 10
       AND (
           SELECT count(DISTINCT rows.value ->> 'component_key')
           FROM jsonb_array_elements(assessment_output -> 'component_breakdown') AS rows(value)
       ) IS NOT DISTINCT FROM 10
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(assessment_output -> 'component_breakdown')
                AS rows(value)
           WHERE jsonb_typeof(rows.value) IS DISTINCT FROM 'object'
              OR NOT b3s_history.evidence_vault_json_has_exact_keys(
                  rows.value,
                  ARRAY[
                      'component_key', 'tile_count', 'ok_count', 'no_count',
                      'sin_evidencia_count', 'raw_score', 'effective_score',
                      'multiplier', 'points', 'max_points'
                  ]
              )
              OR jsonb_typeof(rows.value -> 'component_key')
                    IS DISTINCT FROM 'string'
              OR EXISTS (
                  SELECT 1
                  FROM unnest(ARRAY[
                      'tile_count', 'ok_count', 'no_count',
                      'sin_evidencia_count', 'raw_score', 'effective_score',
                      'multiplier', 'points', 'max_points'
                  ]) AS fields(name)
                  WHERE jsonb_typeof(rows.value -> fields.name)
                            IS DISTINCT FROM 'number'
                     OR (rows.value ->> fields.name) !~ '^(0|[1-9][0-9]*)$'
              )
       );
$$;

ALTER TABLE b3s_history.evidence_vault_operational_sv9_shadow_assessments
    ADD CONSTRAINT evidence_vault_sv9_shadow_assessments_output_structural
    CHECK (
        assessment_output IS NULL
        OR b3s_history.evidence_vault_sv9_shadow_assessment_output_is_structurally_valid(
            assessment_output
        )
    );

-- The old UNIQUE treats every NULL parent as distinct.  The observation
-- identity has one explicit NULL-parent meaning.  An empty string cannot pass
-- the parent fingerprint CHECK, so this portable expression makes NULL initial
-- parents idempotent on all supported PostgreSQL versions.
CREATE UNIQUE INDEX uq_b3s_vault_sv9_shadow_observation_identity
    ON b3s_history.evidence_vault_operational_sv9_shadow_assessments (
        brand_id, operational_packet_id, operational_packet_fingerprint,
        source_packet_id, source_candidate_packet_fingerprint,
        COALESCE(expected_parent_canonical_memory_version, ''),
        candidate_overlay_version
    );

CREATE OR REPLACE FUNCTION b3s_history.validate_vault_operational_sv9_shadow_assessment_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    operational_payload jsonb;
    operational_overlay_version text;
    source_payload jsonb;
BEGIN
    SELECT packets.packet_payload, packets.candidate_overlay_version
    INTO operational_payload, operational_overlay_version
    FROM b3s_history.evidence_vault_canonical_memory_packets AS packets
    WHERE packets.brand_id = NEW.brand_id
      AND packets.id = NEW.operational_packet_id
      AND packets.packet_fingerprint = NEW.operational_packet_fingerprint
      AND packets.packet_kind = NEW.operational_packet_kind;

    SELECT packets.packet_payload
    INTO source_payload
    FROM b3s_history.evidence_vault_canonical_memory_packets AS packets
    WHERE packets.brand_id = NEW.brand_id
      AND packets.id = NEW.source_packet_id
      AND packets.packet_fingerprint = NEW.source_candidate_packet_fingerprint
      AND packets.packet_kind = NEW.source_packet_kind;

    IF jsonb_typeof(operational_payload) IS DISTINCT FROM 'object'
       OR jsonb_typeof(source_payload) IS DISTINCT FROM 'object'
       OR (operational_payload ->> 'candidate_packet_fingerprint') IS DISTINCT FROM
            NEW.operational_packet_fingerprint
       OR (source_payload ->> 'candidate_packet_fingerprint') IS DISTINCT FROM
            NEW.source_candidate_packet_fingerprint
       OR (operational_payload ->> 'source_candidate_packet_fingerprint')
            IS DISTINCT FROM NEW.source_candidate_packet_fingerprint
       OR (operational_payload ->> 'candidate_overlay_version')
            IS DISTINCT FROM NEW.candidate_overlay_version
       OR operational_overlay_version IS DISTINCT FROM NEW.candidate_overlay_version
       OR (operational_payload #>> '{candidate_overlay,parent_canonical_memory_version}')
            IS DISTINCT FROM (operational_payload ->> 'current_canonical_memory_version')
       OR (source_payload #>> '{manifest,parent_canonical_memory_version}')
            IS DISTINCT FROM (operational_payload ->> 'current_canonical_memory_version')
       OR (
            NEW.assessment_status IN (
                'available', 'contradiction_requires_semantic_reassessment'
            )
            AND (operational_payload ->> 'current_canonical_memory_version')
                IS DISTINCT FROM NEW.expected_parent_canonical_memory_version
       )
       OR (
            NEW.assessment_status = 'stale_candidate_parent'
            AND (operational_payload ->> 'current_canonical_memory_version')
                IS NOT DISTINCT FROM NEW.expected_parent_canonical_memory_version
       )
       -- Reassert every persisted/output fingerprint at the admission boundary.
       OR NOT (
            NEW.operational_packet_fingerprint ~ '^[0-9a-f]{64}$'
            AND NEW.source_candidate_packet_fingerprint ~ '^[0-9a-f]{64}$'
            AND NEW.candidate_overlay_version ~ '^[0-9a-f]{64}$'
            AND NEW.evaluation_identity ~ '^[0-9a-f]{64}$'
            AND (
                NEW.expected_parent_canonical_memory_version IS NULL
                OR NEW.expected_parent_canonical_memory_version ~ '^[0-9a-f]{64}$'
            )
            AND (
                NEW.semantic_provenance_fingerprint IS NULL
                OR NEW.semantic_provenance_fingerprint ~ '^[0-9a-f]{64}$'
            )
            AND (
                NEW.assessment_output IS NULL
                OR (
                    COALESCE(
                        (NEW.assessment_output ->> 'tile_contract_registry_fingerprint')
                            ~ '^[0-9a-f]{64}$',
                        false
                    )
                    AND COALESCE(
                        (NEW.assessment_output ->> 'assessment_fingerprint')
                            ~ '^[0-9a-f]{64}$',
                        false
                    )
                    AND COALESCE(
                        (NEW.assessment_output ->> 'score_fingerprint')
                            ~ '^[0-9a-f]{64}$',
                        false
                    )
                )
            )
       )
       OR jsonb_typeof(source_payload -> 'candidate_tiles') IS DISTINCT FROM 'array'
       OR jsonb_array_length(source_payload -> 'candidate_tiles') IS DISTINCT FROM 80
       OR (
            SELECT count(DISTINCT rows.value ->> 'tile_id')
            FROM jsonb_array_elements(source_payload -> 'candidate_tiles')
                 AS rows(value)
       ) IS DISTINCT FROM 80
       OR jsonb_typeof(operational_payload #> '{scoring_projection,tiles}')
            IS DISTINCT FROM 'array'
       OR jsonb_array_length(operational_payload #> '{scoring_projection,tiles}')
            IS DISTINCT FROM 80
       OR (
            SELECT count(DISTINCT rows.value ->> 'tile_id')
            FROM jsonb_array_elements(operational_payload #> '{scoring_projection,tiles}')
                 AS rows(value)
       ) IS DISTINCT FROM 80 THEN
        RAISE EXCEPTION
            'SV9 shadow assessment does not bind exact operational/source packet payloads';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(NEW.candidate_semantic_tiles) AS assessment(value)
        WHERE NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(source_payload -> 'candidate_tiles')
                 AS source_tile(value)
            WHERE (source_tile.value ->> 'component_key') IS NOT DISTINCT FROM
                    (assessment.value ->> 'component_key')
              AND (source_tile.value ->> 'tile_id') IS NOT DISTINCT FROM
                    (assessment.value ->> 'tile_id')
              AND (source_tile.value ->> 'tile_key') IS NOT DISTINCT FROM
                    (assessment.value ->> 'tile_key')
              AND (source_tile.value ->> 'candidate_state') IS NOT DISTINCT FROM
                    (assessment.value ->> 'assessment_state')
        )
    ) THEN
        RAISE EXCEPTION
            'SV9 shadow semantic tiles do not match the exact source packet';
    END IF;

    -- The operational scoring projection is authoritative only for this
    -- adapter's lifecycle/authority observation, never for semantic scoring.
    -- Requirements are exactly: C7 owned_web_plus_external_social, C8
    -- human_required, every other tile ordinary; contradiction is disputed,
    -- superseded is stale, rejected authority is unverifiable, else pending.
    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(NEW.candidate_semantic_tiles) AS semantic(value)
        LEFT JOIN LATERAL (
            SELECT projection.value
            FROM jsonb_array_elements(
                operational_payload #> '{scoring_projection,tiles}'
            ) AS projection(value)
            WHERE (projection.value ->> 'tile_id') IS NOT DISTINCT FROM
                    (semantic.value ->> 'tile_id')
        ) AS projection ON true
        LEFT JOIN LATERAL (
            SELECT requirement.value
            FROM jsonb_array_elements(NEW.verification_requirements -> 'tiles')
                 AS requirement(value)
            WHERE (requirement.value ->> 'tile_id') IS NOT DISTINCT FROM
                    (semantic.value ->> 'tile_id')
        ) AS requirement ON true
        WHERE projection.value IS NULL
           OR requirement.value IS NULL
           OR (projection.value ->> 'candidate_semantic_state') IS DISTINCT FROM
                (semantic.value ->> 'assessment_state')
           OR (requirement.value ->> 'verification_requirement') IS DISTINCT FROM
                CASE semantic.value ->> 'tile_id'
                    WHEN 'C7' THEN 'owned_web_plus_external_social'
                    WHEN 'C8' THEN 'human_required'
                    ELSE 'ordinary'
                END
           OR (requirement.value ->> 'verification_state') IS DISTINCT FROM
                CASE
                    WHEN (semantic.value ->> 'assessment_state')
                            IS NOT DISTINCT FROM 'contradiction' THEN 'disputed'
                    WHEN (projection.value ->> 'lifecycle_state')
                            IS NOT DISTINCT FROM 'superseded' THEN 'stale'
                    WHEN (projection.value ->> 'authority_state')
                            IS NOT DISTINCT FROM 'rejected' THEN 'unverifiable'
                    ELSE 'pending'
                END
    ) THEN
        RAISE EXCEPTION
            'SV9 shadow verification requirements do not match the exact semantic/projection adapter';
    END IF;

    IF NEW.assessment_status = 'available' AND (
        (NEW.assessment_output -> 'tiles') IS DISTINCT FROM NEW.candidate_semantic_tiles
        OR EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.candidate_semantic_tiles) AS rows(value)
            WHERE (rows.value ->> 'assessment_state') IS NOT DISTINCT FROM 'contradiction'
        )
    ) THEN
        RAISE EXCEPTION
            'available SV9 shadow assessment must be the complete non-contradictory semantic vector';
    END IF;

    IF NEW.assessment_status = 'contradiction_requires_semantic_reassessment'
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(NEW.candidate_semantic_tiles) AS rows(value)
           WHERE (rows.value ->> 'assessment_state') IS NOT DISTINCT FROM 'contradiction'
       ) THEN
        RAISE EXCEPTION
            'contradiction assessment status requires a contradiction tile';
    END IF;
    RETURN NEW;
END;
$$;

-- All constraint helpers and trigger functions are internal implementation
-- details.  A future writer must receive an explicit reviewed capability; no
-- public caller may execute them directly.
REVOKE ALL ON FUNCTION
    b3s_history.evidence_vault_sv9_shadow_semantic_tiles_are_valid(jsonb)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    b3s_history.evidence_vault_sv9_shadow_verification_is_valid(jsonb)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    b3s_history.evidence_vault_sv9_shadow_assessment_output_is_structurally_valid(jsonb)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    b3s_history.validate_vault_operational_sv9_shadow_assessment_insert()
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    b3s_history.reject_vault_operational_sv9_shadow_assessment_mutation()
FROM PUBLIC;
