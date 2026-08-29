ALTER TABLE b3s_history.evidence_vault_sv9_judgment_candidates DROP CONSTRAINT evidence_vault_sv9_judgment_candidates_schema_version_check;
ALTER TABLE b3s_history.evidence_vault_sv9_judgment_candidates ADD CONSTRAINT evidence_vault_sv9_judgment_candidates_schema_version_check CHECK (schema_version IN ('evidence-vault-sv9-judgment-candidate-v1', 'evidence-vault-sv9-judgment-candidate-v2')) NOT VALID;
ALTER TABLE b3s_history.evidence_vault_sv9_judgment_candidates
    ADD CONSTRAINT evidence_vault_sv9_judgment_candidate_payload_witness_check
    CHECK ((
        jsonb_typeof(candidate_payload) = 'object'
        AND candidate_payload ?& ARRAY['schema_version', 'plan', 'canonical_plan_fingerprint', 'current_series_fingerprint', 'candidate_series_fingerprint', 'component_evaluations', 'evidence_bindings', 'candidate_tile_judgments', 'candidate_component_sentinels', 'assessment', 'telemetry', 'evaluation_bundle_fingerprint', 'assessment_fingerprint', 'score_fingerprint', 'complete_record_fingerprint']
        AND NOT jsonb_path_exists(candidate_payload, '$.* ? (@.type() == "null")')
        AND jsonb_typeof(candidate_payload->'schema_version') = 'string'
        AND jsonb_typeof(candidate_payload->'plan') = 'object'
        AND jsonb_typeof(candidate_payload->'canonical_plan_fingerprint') = 'string'
        AND jsonb_typeof(candidate_payload->'current_series_fingerprint') = 'string'
        AND jsonb_typeof(candidate_payload->'candidate_series_fingerprint') = 'string'
        AND jsonb_typeof(candidate_payload->'component_evaluations') = 'array'
        AND jsonb_typeof(candidate_payload->'evidence_bindings') = 'array'
        AND jsonb_typeof(candidate_payload->'candidate_tile_judgments') = 'array'
        AND jsonb_typeof(candidate_payload->'candidate_component_sentinels') = 'array'
        AND jsonb_typeof(candidate_payload->'assessment') = 'object'
        AND jsonb_typeof(candidate_payload->'telemetry') = 'object'
        AND jsonb_typeof(candidate_payload->'evaluation_bundle_fingerprint') = 'string'
        AND jsonb_typeof(candidate_payload->'assessment_fingerprint') = 'string'
        AND jsonb_typeof(candidate_payload->'score_fingerprint') = 'string'
        AND jsonb_typeof(candidate_payload->'complete_record_fingerprint') = 'string'
        AND candidate_payload->>'schema_version' = schema_version
        AND candidate_payload->>'canonical_plan_fingerprint' = canonical_plan_fingerprint
        AND candidate_payload->>'current_series_fingerprint' = current_series_fingerprint
        AND candidate_payload->>'candidate_series_fingerprint' = candidate_series_fingerprint
        AND candidate_payload->>'evaluation_bundle_fingerprint' = evaluation_bundle_fingerprint
        AND candidate_payload->>'assessment_fingerprint' = assessment_fingerprint
        AND candidate_payload->>'score_fingerprint' = score_fingerprint
        AND candidate_payload->>'complete_record_fingerprint' = complete_record_fingerprint
        AND (
            (schema_version = 'evidence-vault-sv9-judgment-candidate-v1' AND (candidate_payload - ARRAY['schema_version', 'plan', 'canonical_plan_fingerprint', 'current_series_fingerprint', 'candidate_series_fingerprint', 'component_evaluations', 'evidence_bindings', 'candidate_tile_judgments', 'candidate_component_sentinels', 'assessment', 'telemetry', 'evaluation_bundle_fingerprint', 'assessment_fingerprint', 'score_fingerprint', 'complete_record_fingerprint']) = '{}'::jsonb)
            OR (schema_version = 'evidence-vault-sv9-judgment-candidate-v2'
                AND (candidate_payload - ARRAY['schema_version', 'plan', 'canonical_plan_fingerprint', 'current_series_fingerprint', 'candidate_series_fingerprint', 'component_evaluations', 'evidence_bindings', 'candidate_tile_judgments', 'candidate_component_sentinels', 'assessment', 'telemetry', 'evaluation_bundle_fingerprint', 'assessment_fingerprint', 'score_fingerprint', 'complete_record_fingerprint', 'authoritative_relation_witness']) = '{}'::jsonb
                AND jsonb_typeof(candidate_payload->'authoritative_relation_witness') = 'object'
                AND candidate_payload->'authoritative_relation_witness' ?& ARRAY['schema_version', 'source_scan_id', 'operational_witness', 'authoritative_relations', 'projection_fingerprint', 'witness_fingerprint']
                AND ((candidate_payload->'authoritative_relation_witness') - ARRAY['schema_version', 'source_scan_id', 'operational_witness', 'authoritative_relations', 'projection_fingerprint', 'witness_fingerprint']) = '{}'::jsonb
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness', '$.* ? (@.type() == "null")')
                AND jsonb_typeof(candidate_payload->'authoritative_relation_witness'->'schema_version') = 'string'
                AND jsonb_typeof(candidate_payload->'authoritative_relation_witness'->'source_scan_id') = 'string'
                AND jsonb_typeof(candidate_payload->'authoritative_relation_witness'->'projection_fingerprint') = 'string'
                AND jsonb_typeof(candidate_payload->'authoritative_relation_witness'->'witness_fingerprint') = 'string'
                AND candidate_payload->'authoritative_relation_witness'->>'schema_version' = 'evidence-vault-sv9-authoritative-relation-witness-v1'
                AND candidate_payload->'authoritative_relation_witness'->>'source_scan_id' = source_scan_id
                AND jsonb_typeof(candidate_payload->'authoritative_relation_witness'->'operational_witness') = 'object'
                AND candidate_payload->'authoritative_relation_witness'->'operational_witness' ?& ARRAY['canonical_memory_version', 'adoption_event_id', 'adoption_sequence', 'candidate_packet_fingerprint', 'request_fingerprint']
                AND ((candidate_payload->'authoritative_relation_witness'->'operational_witness') - ARRAY['canonical_memory_version', 'adoption_event_id', 'adoption_sequence', 'candidate_packet_fingerprint', 'request_fingerprint']) = '{}'::jsonb
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'operational_witness', '$.* ? (@.type() == "null")')
                AND jsonb_typeof(candidate_payload->'authoritative_relation_witness'->'operational_witness'->'canonical_memory_version') = 'string'
                AND jsonb_typeof(candidate_payload->'authoritative_relation_witness'->'operational_witness'->'adoption_event_id') = 'string'
                AND jsonb_typeof(candidate_payload->'authoritative_relation_witness'->'operational_witness'->'adoption_sequence') = 'number'
                AND jsonb_typeof(candidate_payload->'authoritative_relation_witness'->'operational_witness'->'candidate_packet_fingerprint') = 'string'
                AND jsonb_typeof(candidate_payload->'authoritative_relation_witness'->'operational_witness'->'request_fingerprint') = 'string'
                AND candidate_payload->'authoritative_relation_witness'->'operational_witness'->>'adoption_sequence' ~ '^[1-9][0-9]*$'
                AND candidate_payload->'authoritative_relation_witness'->'operational_witness'->>'adoption_event_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                AND candidate_payload->'authoritative_relation_witness'->'operational_witness'->>'canonical_memory_version' ~ '^[0-9a-f]{64}$'
                AND candidate_payload->'authoritative_relation_witness'->'operational_witness'->>'candidate_packet_fingerprint' ~ '^[0-9a-f]{64}$'
                AND candidate_payload->'authoritative_relation_witness'->'operational_witness'->>'request_fingerprint' ~ '^[0-9a-f]{64}$'
                AND candidate_payload->'authoritative_relation_witness'->>'projection_fingerprint' ~ '^[0-9a-f]{64}$'
                AND candidate_payload->'authoritative_relation_witness'->>'witness_fingerprint' ~ '^[0-9a-f]{64}$'
                AND jsonb_typeof(candidate_payload->'authoritative_relation_witness'->'authoritative_relations') = 'array'
                AND candidate_payload->'authoritative_relation_witness'->'authoritative_relations' <> '[]'::jsonb
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'authoritative_relations', 'strict $[*] ? (@.type() != "object" || exists(@.schema_version ? (@.type() != "string")) || exists(@.tile_id ? (@.type() != "string")) || exists(@.component_key ? (@.type() != "string")) || exists(@.disposition ? (@.type() != "string")) || exists(@.evidence_ref ? (@.type() != "string")) || exists(@.evidence_fingerprint ? (@.type() != "string")) || exists(@.capture_origin ? (@.type() != "object")) || exists(@.capture_origin.capture_id ? (@.type() != "string")) || exists(@.capture_origin.capture_fingerprint ? (@.type() != "string")) || exists(@.operation_origin ? (@.type() != "object")) || exists(@.operation_origin.operation_id ? (@.type() != "string")) || exists(@.operation_origin.operation_fingerprint ? (@.type() != "string")) || exists(@.relation_fingerprint ? (@.type() != "string")) )')
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'authoritative_relations', '$[*] ? (!(exists(@.schema_version)) || !(exists(@.tile_id)) || !(exists(@.component_key)) || !(exists(@.disposition)) || !(exists(@.evidence_ref)) || !(exists(@.evidence_fingerprint)) || !(exists(@.capture_origin)) || !(exists(@.capture_origin.capture_id)) || !(exists(@.capture_origin.capture_fingerprint)) || !(exists(@.operation_origin)) || !(exists(@.operation_origin.operation_id)) || !(exists(@.operation_origin.operation_fingerprint)) || !(exists(@.relation_fingerprint)) || exists(@.keyvalue() ? (@.key != "schema_version" && @.key != "tile_id" && @.key != "component_key" && @.key != "disposition" && @.key != "evidence_ref" && @.key != "evidence_fingerprint" && @.key != "capture_origin" && @.key != "operation_origin" && @.key != "relation_fingerprint")) || exists(@.capture_origin.keyvalue() ? (@.key != "capture_id" && @.key != "capture_fingerprint")) || exists(@.operation_origin.keyvalue() ? (@.key != "operation_id" && @.key != "operation_fingerprint")))')
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'authoritative_relations', '$[*] ? (@.schema_version != "evidence-vault-sv9-authoritative-tile-relation-v1")')
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'authoritative_relations', '$[*] ? (@.tile_id == "")')
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'authoritative_relations', '$[*] ? (@.component_key == "")')
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'authoritative_relations', '$[*] ? (@.disposition != "relevant")')
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'authoritative_relations', '$[*] ? (@.evidence_ref == "")')
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'authoritative_relations', '$[*] ? (!(@.evidence_fingerprint like_regex "^[0-9a-f]{64}$"))')
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'authoritative_relations', '$[*] ? (!(@.capture_origin.capture_id like_regex "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"))')
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'authoritative_relations', '$[*] ? (!(@.capture_origin.capture_fingerprint like_regex "^[0-9a-f]{64}$"))')
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'authoritative_relations', '$[*] ? (!(@.operation_origin.operation_id like_regex "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"))')
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'authoritative_relations', '$[*] ? (!(@.operation_origin.operation_fingerprint like_regex "^[0-9a-f]{64}$"))')
                AND NOT jsonb_path_exists(candidate_payload->'authoritative_relation_witness'->'authoritative_relations', '$[*] ? (!(@.relation_fingerprint like_regex "^[0-9a-f]{64}$"))')
            )
        )
    ) IS TRUE) NOT VALID;
ALTER TABLE b3s_history.evidence_vault_sv9_judgment_candidates VALIDATE CONSTRAINT evidence_vault_sv9_judgment_candidates_schema_version_check;
ALTER TABLE b3s_history.evidence_vault_sv9_judgment_candidates VALIDATE CONSTRAINT evidence_vault_sv9_judgment_candidate_payload_witness_check;
