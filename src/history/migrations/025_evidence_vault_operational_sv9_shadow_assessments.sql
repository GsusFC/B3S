-- Append-only, non-authoritative SV9 semantic-assessment shadow ledger.
--
-- This is deliberately storage-only: no repository write/read path, scanner
-- capability, privileged function surface, canonical-memory transition, or runtime
-- cutover is introduced here.  An assessment row is bound to the exact two
-- immutable operational packet rows that a future trusted writer must validate.

CREATE FUNCTION b3s_history.evidence_vault_sv9_shadow_semantic_tiles_are_valid(
    tiles jsonb
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT jsonb_typeof(tiles) = 'array'
       AND jsonb_array_length(tiles) = 80
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(tiles) AS rows(value)
           WHERE NOT b3s_history.evidence_vault_json_has_exact_keys(
               rows.value,
               ARRAY['component_key', 'tile_id', 'tile_key', 'assessment_state']
           )
              OR jsonb_typeof(rows.value -> 'component_key') <> 'string'
              OR jsonb_typeof(rows.value -> 'tile_id') <> 'string'
              OR jsonb_typeof(rows.value -> 'tile_key') <> 'string'
              OR rows.value ->> 'assessment_state' NOT IN (
                  'ok', 'no', 'sin_evidencia', 'contradiction'
              )
       )
       AND (
           SELECT count(DISTINCT rows.value ->> 'tile_id')
           FROM jsonb_array_elements(tiles) AS rows(value)
       ) = 80;
$$;

CREATE FUNCTION b3s_history.evidence_vault_sv9_shadow_verification_is_valid(
    requirements jsonb
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT jsonb_typeof(requirements) = 'object'
       AND b3s_history.evidence_vault_json_has_exact_keys(
           requirements, ARRAY['schema_version', 'tile_count', 'tiles', 'counts']
       )
       AND requirements ->> 'schema_version' =
           'evidence-vault-operational-verification-requirements-v1'
       AND requirements ->> 'tile_count' = '80'
       AND jsonb_typeof(requirements -> 'tiles') = 'array'
       AND jsonb_array_length(requirements -> 'tiles') = 80
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(requirements -> 'tiles') AS rows(value)
           WHERE NOT b3s_history.evidence_vault_json_has_exact_keys(
               rows.value,
               ARRAY['tile_id', 'verification_requirement', 'verification_state']
           )
              OR jsonb_typeof(rows.value -> 'tile_id') <> 'string'
              OR rows.value ->> 'verification_requirement' NOT IN (
                  'ordinary', 'owned_web_plus_external_social', 'human_required'
              )
              -- Deliberately excludes verified: this adapter has no separate
              -- evidence verification binding.
              OR rows.value ->> 'verification_state' NOT IN (
                  'pending', 'disputed', 'stale', 'unverifiable'
              )
       )
       AND (
           SELECT count(DISTINCT rows.value ->> 'tile_id')
           FROM jsonb_array_elements(requirements -> 'tiles') AS rows(value)
       ) = 80;
$$;

CREATE TABLE b3s_history.evidence_vault_operational_sv9_shadow_assessments (
    id uuid PRIMARY KEY,
    brand_id uuid NOT NULL
        REFERENCES b3s_history.brands(id) ON DELETE RESTRICT,
    operational_packet_id uuid NOT NULL,
    operational_packet_fingerprint text NOT NULL CHECK (
        operational_packet_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    operational_packet_kind text NOT NULL CHECK (
        operational_packet_kind = 'operational_v2'
    ),
    source_packet_id uuid NOT NULL,
    source_candidate_packet_fingerprint text NOT NULL CHECK (
        source_candidate_packet_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    source_packet_kind text NOT NULL CHECK (
        source_packet_kind IN (
            'operational_source_v2', 'operational_reviewed_v2'
        )
    ),
    -- This is the explicit expected parent supplied by the future writer.
    -- It is an identity value, not the transient omitted-argument outcome
    -- `expected_parent_required`, which is intentionally not persistible.
    expected_parent_canonical_memory_version text CHECK (
        expected_parent_canonical_memory_version IS NULL
        OR expected_parent_canonical_memory_version ~ '^[0-9a-f]{64}$'
    ),
    candidate_overlay_version text NOT NULL CHECK (
        candidate_overlay_version ~ '^[0-9a-f]{64}$'
    ),
    evaluation_identity text NOT NULL CHECK (
        evaluation_identity ~ '^[0-9a-f]{64}$'
    ),
    schema_version text NOT NULL CHECK (
        schema_version =
            'evidence-vault-operational-semantic-assessment-shadow-v1'
    ),
    authority boolean NOT NULL DEFAULT false CHECK (authority = false),
    production_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        production_runtime_effect = false
    ),
    scanner_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        scanner_runtime_effect = false
    ),
    -- `available` stores a complete kernel result.  The two unavailable rows
    -- remain audit observations, never scores.  The adapter's omitted-parent
    -- diagnostic is purposely absent from this finite persisted vocabulary.
    assessment_status text NOT NULL CHECK (
        assessment_status IN (
            'available',
            'stale_candidate_parent',
            'contradiction_requires_semantic_reassessment'
        )
    ),
    candidate_semantic_tiles jsonb NOT NULL CHECK (
        b3s_history.evidence_vault_sv9_shadow_semantic_tiles_are_valid(
            candidate_semantic_tiles
        )
    ),
    assessment_output jsonb CHECK (
        assessment_output IS NULL
        OR (
            jsonb_typeof(assessment_output) = 'object'
            AND b3s_history.evidence_vault_json_has_exact_keys(
                assessment_output,
                ARRAY[
                    'schema_version', 'rubric_version',
                    'assessment_vector_version', 'scoring_policy_version',
                    'tile_contract_registry_fingerprint', 'tile_count', 'tiles',
                    'sv9_score', 'component_breakdown', 'base_average',
                    'magnetism_capped', 'assessment_fingerprint',
                    'score_fingerprint'
                ]
            )
            AND assessment_output ->> 'schema_version' = 'sv9-assessment-output-v1'
            AND assessment_output ->> 'rubric_version' = 'baldosas-v3-1'
            AND assessment_output ->> 'assessment_vector_version' =
                'sv9-assessment-vector-v1'
            AND assessment_output ->> 'scoring_policy_version' =
                'sv9-scoring-policy-baldosas-v3-1-v1'
            AND assessment_output ->> 'tile_count' = '80'
            AND jsonb_typeof(assessment_output -> 'tiles') = 'array'
            AND jsonb_array_length(assessment_output -> 'tiles') = 80
            AND assessment_output ->> 'assessment_fingerprint' ~ '^[0-9a-f]{64}$'
            AND assessment_output ->> 'score_fingerprint' ~ '^[0-9a-f]{64}$'
        )
    ),
    semantic_provenance_fingerprint text CHECK (
        semantic_provenance_fingerprint IS NULL
        OR semantic_provenance_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    verification_requirements jsonb NOT NULL CHECK (
        b3s_history.evidence_vault_sv9_shadow_verification_is_valid(
            verification_requirements
        )
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (brand_id, evaluation_identity),
    UNIQUE (
        brand_id, operational_packet_id, operational_packet_fingerprint,
        source_packet_id, source_candidate_packet_fingerprint,
        expected_parent_canonical_memory_version, candidate_overlay_version
    ),
    -- These are the exact four-part identity keys established in migration 021;
    -- an id/fingerprint/kind mix from another immutable packet cannot bind.
    FOREIGN KEY (
        brand_id, operational_packet_id, operational_packet_fingerprint,
        operational_packet_kind
    ) REFERENCES b3s_history.evidence_vault_canonical_memory_packets (
        brand_id, id, packet_fingerprint, packet_kind
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        brand_id, source_packet_id, source_candidate_packet_fingerprint,
        source_packet_kind
    ) REFERENCES b3s_history.evidence_vault_canonical_memory_packets (
        brand_id, id, packet_fingerprint, packet_kind
    ) ON DELETE RESTRICT,
    CHECK (
        (assessment_status = 'available'
            AND assessment_output IS NOT NULL
            AND semantic_provenance_fingerprint IS NOT NULL)
        OR (assessment_status IN (
                'stale_candidate_parent',
                'contradiction_requires_semantic_reassessment'
            )
            AND assessment_output IS NULL
            AND semantic_provenance_fingerprint IS NULL)
    )
);

CREATE INDEX idx_b3s_vault_sv9_shadow_assessments_created
    ON b3s_history.evidence_vault_operational_sv9_shadow_assessments (
        brand_id, created_at DESC, id DESC
    );

CREATE FUNCTION b3s_history.validate_vault_operational_sv9_shadow_assessment_insert()
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

    -- `canonical_v1` is never a valid source: it may lack packet_payload and
    -- cannot prove the operational source-candidate contract.
    IF jsonb_typeof(operational_payload) <> 'object'
       OR jsonb_typeof(source_payload) <> 'object'
       OR operational_payload ->> 'candidate_packet_fingerprint' IS DISTINCT FROM
            NEW.operational_packet_fingerprint
       OR source_payload ->> 'candidate_packet_fingerprint' IS DISTINCT FROM
            NEW.source_candidate_packet_fingerprint
       OR operational_payload ->> 'source_candidate_packet_fingerprint'
            IS DISTINCT FROM NEW.source_candidate_packet_fingerprint
       OR operational_payload ->> 'candidate_overlay_version'
            IS DISTINCT FROM NEW.candidate_overlay_version
       OR operational_overlay_version IS DISTINCT FROM NEW.candidate_overlay_version
       OR operational_payload -> 'candidate_overlay' ->> 'parent_canonical_memory_version'
            IS DISTINCT FROM operational_payload ->> 'current_canonical_memory_version'
       OR source_payload -> 'manifest' ->> 'parent_canonical_memory_version'
            IS DISTINCT FROM operational_payload ->> 'current_canonical_memory_version'
       OR (
            NEW.assessment_status IN (
                'available', 'contradiction_requires_semantic_reassessment'
            )
            AND operational_payload ->> 'current_canonical_memory_version'
                IS DISTINCT FROM NEW.expected_parent_canonical_memory_version
       )
       OR (
            NEW.assessment_status = 'stale_candidate_parent'
            AND operational_payload ->> 'current_canonical_memory_version'
                IS NOT DISTINCT FROM NEW.expected_parent_canonical_memory_version
       )
       OR jsonb_typeof(source_payload -> 'candidate_tiles') <> 'array'
       OR jsonb_array_length(source_payload -> 'candidate_tiles') <> 80
       OR (
            SELECT count(DISTINCT rows.value ->> 'tile_id')
            FROM jsonb_array_elements(source_payload -> 'candidate_tiles')
                 AS rows(value)
       ) <> 80 THEN
        RAISE EXCEPTION
            'SV9 shadow assessment does not bind exact operational/source packet payloads';
    END IF;

    -- Persisted semantic rows must be the exact source candidate vector, not a
    -- projection reconstructed from a similarly named or later packet.
    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(NEW.candidate_semantic_tiles) AS assessment(value)
        WHERE NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(source_payload -> 'candidate_tiles')
                 AS source_tile(value)
            WHERE source_tile.value ->> 'component_key' =
                    assessment.value ->> 'component_key'
              AND source_tile.value ->> 'tile_id' = assessment.value ->> 'tile_id'
              AND source_tile.value ->> 'tile_key' = assessment.value ->> 'tile_key'
              AND source_tile.value ->> 'candidate_state' =
                    assessment.value ->> 'assessment_state'
        )
    ) THEN
        RAISE EXCEPTION
            'SV9 shadow semantic tiles do not match the exact source packet';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(
            NEW.verification_requirements -> 'tiles'
        ) AS verification(value)
        WHERE NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.candidate_semantic_tiles)
                 AS semantic(value)
            WHERE semantic.value ->> 'tile_id' = verification.value ->> 'tile_id'
        )
    ) THEN
        RAISE EXCEPTION
            'SV9 shadow verification tiles do not match the semantic vector';
    END IF;

    IF NEW.assessment_status = 'available' AND (
        NEW.assessment_output -> 'tiles' IS DISTINCT FROM NEW.candidate_semantic_tiles
        OR EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.candidate_semantic_tiles) AS rows(value)
            WHERE rows.value ->> 'assessment_state' = 'contradiction'
        )
    ) THEN
        RAISE EXCEPTION
            'available SV9 shadow assessment must be the complete non-contradictory semantic vector';
    END IF;

    IF NEW.assessment_status =
            'contradiction_requires_semantic_reassessment'
       AND NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.candidate_semantic_tiles) AS rows(value)
            WHERE rows.value ->> 'assessment_state' = 'contradiction'
       ) THEN
        RAISE EXCEPTION
            'contradiction assessment status requires a contradiction tile';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION b3s_history.reject_vault_operational_sv9_shadow_assessment_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Evidence Vault operational SV9 shadow assessments are append-only';
END;
$$;

CREATE TRIGGER evidence_vault_operational_sv9_shadow_assessments_validate_insert
BEFORE INSERT ON b3s_history.evidence_vault_operational_sv9_shadow_assessments
FOR EACH ROW
EXECUTE FUNCTION b3s_history.validate_vault_operational_sv9_shadow_assessment_insert();

CREATE TRIGGER evidence_vault_operational_sv9_shadow_assessments_append_only
BEFORE UPDATE OR DELETE ON b3s_history.evidence_vault_operational_sv9_shadow_assessments
FOR EACH ROW
EXECUTE FUNCTION b3s_history.reject_vault_operational_sv9_shadow_assessment_mutation();

CREATE TRIGGER evidence_vault_operational_sv9_shadow_assessments_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_vault_operational_sv9_shadow_assessments
FOR EACH STATEMENT
EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();

-- No scanner surface is created.  Remove both implicit/public and the known
-- scanner login's direct access; a future privileged writer needs a separately
-- reviewed capability rather than inheriting scanner ingestion grants.
REVOKE ALL ON b3s_history.evidence_vault_operational_sv9_shadow_assessments
FROM PUBLIC;

REVOKE ALL ON FUNCTION
    b3s_history.validate_vault_operational_sv9_shadow_assessment_insert()
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    b3s_history.reject_vault_operational_sv9_shadow_assessment_mutation()
FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'b3s_pr71_scanner_ingest'
    ) THEN
        EXECUTE
            'REVOKE ALL PRIVILEGES ON '
            || 'b3s_history.evidence_vault_operational_sv9_shadow_assessments '
            || 'FROM b3s_pr71_scanner_ingest';
        IF pg_catalog.has_table_privilege(
            'b3s_pr71_scanner_ingest',
            'b3s_history.evidence_vault_operational_sv9_shadow_assessments',
            'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'
        ) OR pg_catalog.has_any_column_privilege(
            'b3s_pr71_scanner_ingest',
            'b3s_history.evidence_vault_operational_sv9_shadow_assessments',
            'SELECT, INSERT, UPDATE, REFERENCES'
        ) THEN
            RAISE EXCEPTION
                'scanner role retains forbidden SV9 shadow assessment ledger access';
        END IF;
    END IF;
END;
$$;

COMMENT ON TABLE b3s_history.evidence_vault_operational_sv9_shadow_assessments IS
    'Append-only non-authoritative SV9 semantic shadow observations. They have no scanner, production, canonical-memory, or cutover effect.';
