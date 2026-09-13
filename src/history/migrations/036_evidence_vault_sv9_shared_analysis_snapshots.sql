-- Private, immutable Core/Flow analysis retained for crash-safe Vault resume.
-- The public judgment candidate remains the existing strict v1/v2 contract;
-- this ledger binds exactly one private full payload to its immutable id.

CREATE FUNCTION b3s_history.validate_vault_sv9_component_payload(
    payload jsonb,
    expected_component text
) RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    expected_tiles jsonb;
    actual_tiles jsonb;
    tile jsonb;
    ok_count integer;
    blind_count integer;
    expected_scale integer;
    expected_multiplier integer;
    expected_confidence text;
    expected_lit_tiles jsonb;
    expected_off_tiles jsonb;
    expected_blind_tiles jsonb;
BEGIN
    expected_tiles := CASE expected_component
        WHEN 'mission' THEN '["M1","M2","M3","M4","M5"]'::jsonb
        WHEN 'vision' THEN '["V1","V2","V3","V4","V5"]'::jsonb
        WHEN 'values' THEN '["VA1","VA2","VA3","VA4","VA5"]'::jsonb
        WHEN 'attributes' THEN '["A1","A2","A3","A4","A5"]'::jsonb
        WHEN 'value_proposition' THEN '["P1","P2","P3","P4","P5","P6","P7","P8","P9","P10"]'::jsonb
        WHEN 'personality' THEN '["PE1","PE2","PE3","PE4","PE5","PE6","PE7","PE8","PE9","PE10"]'::jsonb
        WHEN 'brand_idea' THEN '["I1","I2","I3","I4","I5","I6","I7","I8","I9","I10"]'::jsonb
        WHEN 'core_purpose' THEN '["PR1","PR2","PR3","PR4","PR5","PR6","PR7","PR8","PR9","PR10"]'::jsonb
        WHEN 'magnetism' THEN '["MG1","MG2","MG3","MG4","MG5","MG6","MG7","MG8","MG9","MG10"]'::jsonb
        WHEN 'coherencia' THEN '["C1","C2","C3","C4","C5","C6","C7","C8","C9","C10"]'::jsonb
        ELSE NULL
    END;
    IF expected_tiles IS NULL
       OR jsonb_typeof(payload) IS DISTINCT FROM 'object'
       OR NOT payload ?& ARRAY[
           'component', 'status', 'score', 'scale', 'points', 'confidence',
           'blind_spot_count', 'veredicto', 'message', 'evaluation_model',
           'tile_profile', 'lit_tiles', 'off_tiles', 'blind_spot_tiles',
           'detected_content', 'detection_mode', 'detection_confidence',
           'detection_limitations', 'evidence_source_summary',
           'source_policy_notes', 'evidence', 'error'
       ]
       OR payload - ARRAY[
           'component', 'status', 'score', 'scale', 'points', 'confidence',
           'blind_spot_count', 'veredicto', 'message', 'evaluation_model',
           'tile_profile', 'lit_tiles', 'off_tiles', 'blind_spot_tiles',
           'detected_content', 'detection_mode', 'detection_confidence',
           'detection_limitations', 'evidence_source_summary',
           'source_policy_notes', 'evidence', 'error'
       ] <> '{}'::jsonb
       OR payload ->> 'component' IS DISTINCT FROM expected_component
       OR jsonb_typeof(payload -> 'component') IS DISTINCT FROM 'string'
       OR jsonb_typeof(payload -> 'status') IS DISTINCT FROM 'string'
       OR payload ->> 'status' IS NULL
       OR payload ->> 'status' NOT IN ('scored', 'not_detected')
       OR jsonb_typeof(payload -> 'score') IS DISTINCT FROM 'number'
       OR jsonb_typeof(payload -> 'scale') IS DISTINCT FROM 'number'
       OR jsonb_typeof(payload -> 'points') IS DISTINCT FROM 'number'
       OR jsonb_typeof(payload -> 'blind_spot_count') IS DISTINCT FROM 'number'
       OR jsonb_typeof(payload -> 'veredicto') IS DISTINCT FROM 'string'
       OR jsonb_typeof(payload -> 'message') IS DISTINCT FROM 'string'
       OR jsonb_typeof(payload -> 'confidence') NOT IN ('string', 'null')
       OR jsonb_typeof(payload -> 'evaluation_model') NOT IN ('string', 'null')
       OR jsonb_typeof(payload -> 'detected_content') NOT IN ('string', 'null')
       OR jsonb_typeof(payload -> 'detection_mode') NOT IN ('string', 'null')
       OR jsonb_typeof(payload -> 'detection_confidence') NOT IN ('string', 'null')
       OR jsonb_typeof(payload -> 'error') NOT IN ('string', 'null')
       OR jsonb_typeof(payload -> 'tile_profile') IS DISTINCT FROM 'array'
       OR jsonb_typeof(payload -> 'lit_tiles') IS DISTINCT FROM 'array'
       OR jsonb_typeof(payload -> 'off_tiles') IS DISTINCT FROM 'array'
       OR jsonb_typeof(payload -> 'blind_spot_tiles') IS DISTINCT FROM 'array'
       OR jsonb_typeof(payload -> 'detection_limitations') IS DISTINCT FROM 'array'
       OR jsonb_typeof(payload -> 'evidence_source_summary') IS DISTINCT FROM 'object'
       OR jsonb_typeof(payload -> 'source_policy_notes') IS DISTINCT FROM 'array'
       OR jsonb_typeof(payload -> 'evidence') IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;
    SELECT COALESCE(jsonb_agg(value -> 'id'), '[]'::jsonb)
      INTO actual_tiles
      FROM jsonb_array_elements(payload -> 'tile_profile');
    IF (payload ->> 'status' = 'scored' AND actual_tiles <> expected_tiles)
       OR (payload ->> 'status' = 'not_detected'
           AND actual_tiles <> '[]'::jsonb) THEN
        RETURN false;
    END IF;
    FOR tile IN SELECT value FROM jsonb_array_elements(payload -> 'tile_profile')
    LOOP
        IF jsonb_typeof(tile) IS DISTINCT FROM 'object'
           OR NOT tile ?& ARRAY['id', 'estado', 'evidencia', 'motivo', 'contexto_requerido']
           OR tile - ARRAY['id', 'estado', 'evidencia', 'motivo', 'contexto_requerido'] <> '{}'::jsonb
           OR jsonb_typeof(tile -> 'id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(tile -> 'estado') IS DISTINCT FROM 'string'
           OR tile ->> 'estado' IS NULL
           OR tile ->> 'estado' NOT IN ('ok', 'no', 'sin_evidencia')
           OR jsonb_typeof(tile -> 'evidencia') IS DISTINCT FROM 'string'
           OR jsonb_typeof(tile -> 'motivo') IS DISTINCT FROM 'string'
           OR jsonb_typeof(tile -> 'contexto_requerido') IS DISTINCT FROM 'string' THEN
            RETURN false;
        END IF;
    END LOOP;
    SELECT
        count(*) FILTER (WHERE value ->> 'estado' = 'ok'),
        count(*) FILTER (WHERE value ->> 'estado' = 'sin_evidencia'),
        COALESCE(
            jsonb_agg(value -> 'id') FILTER (WHERE value ->> 'estado' = 'ok'),
            '[]'::jsonb
        ),
        COALESCE(
            jsonb_agg(value -> 'id') FILTER (WHERE value ->> 'estado' = 'no'),
            '[]'::jsonb
        ),
        COALESCE(
            jsonb_agg(value -> 'id') FILTER (
                WHERE value ->> 'estado' = 'sin_evidencia'
            ),
            '[]'::jsonb
        )
      INTO ok_count, blind_count, expected_lit_tiles,
           expected_off_tiles, expected_blind_tiles
      FROM jsonb_array_elements(payload -> 'tile_profile');
    expected_scale := jsonb_array_length(expected_tiles);
    expected_multiplier := CASE
        WHEN expected_component IN ('magnetism', 'coherencia') THEN 2
        ELSE 1
    END;
    expected_confidence := CASE
        WHEN blind_count >= 3 THEN 'baja'
        WHEN blind_count >= 2 THEN 'media'
        ELSE 'alta'
    END;
    IF payload ->> 'status' = 'scored' AND (
        (payload ->> 'score')::numeric IS DISTINCT FROM ok_count
        OR (payload ->> 'scale')::numeric IS DISTINCT FROM expected_scale
        OR (payload ->> 'points')::numeric IS DISTINCT FROM
           ok_count * expected_multiplier
        OR (payload ->> 'blind_spot_count')::numeric IS DISTINCT FROM blind_count
        OR payload ->> 'confidence' IS DISTINCT FROM expected_confidence
        OR payload -> 'lit_tiles' IS DISTINCT FROM expected_lit_tiles
        OR payload -> 'off_tiles' IS DISTINCT FROM expected_off_tiles
        OR payload -> 'blind_spot_tiles' IS DISTINCT FROM expected_blind_tiles
    ) THEN
        RETURN false;
    END IF;
    IF payload ->> 'status' = 'not_detected' AND (
        (payload ->> 'score')::numeric IS DISTINCT FROM 0
        OR (payload ->> 'scale')::numeric IS DISTINCT FROM expected_scale
        OR (payload ->> 'points')::numeric IS DISTINCT FROM 0
        OR (payload ->> 'blind_spot_count')::numeric IS DISTINCT FROM 0
        OR jsonb_typeof(payload -> 'confidence') IS DISTINCT FROM 'null'
        OR payload -> 'lit_tiles' IS DISTINCT FROM '[]'::jsonb
        OR payload -> 'off_tiles' IS DISTINCT FROM '[]'::jsonb
        OR payload -> 'blind_spot_tiles' IS DISTINCT FROM '[]'::jsonb
    ) THEN
        RETURN false;
    END IF;
    RETURN true;
END;
$$;

CREATE FUNCTION b3s_history.validate_vault_sv9_checkpoint_process_payload(
    payload jsonb,
    checkpoint jsonb
) RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    binding jsonb;
    flow_context jsonb;
    evaluation jsonb;
    source_identity jsonb;
    strict_tile jsonb;
    component_tile jsonb;
BEGIN
    IF jsonb_typeof(payload) IS DISTINCT FROM 'object'
       OR NOT payload ?& ARRAY[
           'schema_version', 'binding', 'flow_context', 'component_result',
           'llm_usage'
       ]
       OR payload - ARRAY[
           'schema_version', 'binding', 'flow_context', 'component_result',
           'llm_usage'
       ] <> '{}'::jsonb
       OR payload ->> 'schema_version' IS DISTINCT FROM
          'evidence-vault-sv9-shared-checkpoint-process-v1'
       OR jsonb_typeof(payload -> 'llm_usage') IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    binding := payload -> 'binding';
    flow_context := payload -> 'flow_context';
    evaluation := checkpoint #> '{healthy_workset,component_evaluations,0}';
    source_identity := checkpoint #> '{evaluation_input,source_identity}';
    IF jsonb_typeof(binding) IS DISTINCT FROM 'object'
       OR NOT binding ?& ARRAY[
           'source_scan_id', 'canonical_plan_fingerprint',
           'canonical_request_fingerprint', 'current_series_fingerprint',
           'candidate_series_fingerprint', 'component_key', 'capture_origin',
           'operation_origin', 'snapshot_fingerprint'
       ]
       OR binding - ARRAY[
           'source_scan_id', 'canonical_plan_fingerprint',
           'canonical_request_fingerprint', 'current_series_fingerprint',
           'candidate_series_fingerprint', 'component_key', 'capture_origin',
           'operation_origin', 'snapshot_fingerprint'
       ] <> '{}'::jsonb
       OR binding ->> 'source_scan_id' IS DISTINCT FROM
          source_identity ->> 'source_scan_id'
       OR binding ->> 'canonical_plan_fingerprint' IS DISTINCT FROM
          checkpoint #>> '{plan_binding,canonical_plan_fingerprint}'
       OR binding ->> 'canonical_request_fingerprint' IS DISTINCT FROM
          evaluation ->> 'request_fingerprint'
       OR binding ->> 'current_series_fingerprint' IS DISTINCT FROM
          checkpoint #>> '{plan_binding,current_series_fingerprint}'
       OR binding ->> 'candidate_series_fingerprint' IS DISTINCT FROM
          checkpoint #>> '{plan_binding,candidate_series_fingerprint}'
       OR binding ->> 'component_key' IS DISTINCT FROM
          evaluation ->> 'component_key'
       OR binding -> 'capture_origin' IS DISTINCT FROM jsonb_build_object(
          'capture_id', source_identity ->> 'capture_id',
          'capture_fingerprint', source_identity ->> 'capture_fingerprint'
       )
       OR binding -> 'operation_origin' IS DISTINCT FROM jsonb_build_object(
          'operation_id', source_identity ->> 'operation_plan_id',
          'operation_fingerprint', source_identity ->> 'operation_fingerprint'
       )
       OR jsonb_typeof(binding -> 'snapshot_fingerprint') IS DISTINCT FROM 'string'
       OR binding ->> 'snapshot_fingerprint' !~ '^[0-9a-f]{64}$' THEN
        RETURN false;
    END IF;
    IF jsonb_typeof(flow_context) IS DISTINCT FROM 'object'
       OR NOT flow_context ?& ARRAY[
          'candidate', 'interpretation_debug', 'visual_evidence_packet'
       ]
       OR flow_context - ARRAY[
          'candidate', 'interpretation_debug', 'visual_evidence_packet'
       ] <> '{}'::jsonb
       OR jsonb_typeof(flow_context -> 'candidate') IS DISTINCT FROM 'object'
       OR jsonb_typeof(flow_context -> 'interpretation_debug') IS DISTINCT FROM 'object'
       OR jsonb_typeof(flow_context -> 'visual_evidence_packet') NOT IN ('object', 'null')
       OR lower(substring(
           flow_context #>> '{candidate,evidence_pack,url}'
           FROM '^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^/@]+@)?(?:www\.)?([^/:?#]+)'
       )) IS DISTINCT FROM source_identity ->> 'canonical_domain'
       OR NOT b3s_history.validate_vault_sv9_component_payload(
          payload -> 'component_result', evaluation ->> 'component_key'
       ) THEN
        RETURN false;
    END IF;
    IF evaluation ->> 'status' = 'not_detected' THEN
        RETURN payload #>> '{component_result,status}' = 'not_detected'
           AND evaluation -> 'tile_results' = '[]'::jsonb;
    END IF;
    IF evaluation ->> 'status' IS DISTINCT FROM 'evaluated'
       OR payload #>> '{component_result,status}' IS DISTINCT FROM 'scored' THEN
        RETURN false;
    END IF;
    FOR strict_tile IN
        SELECT value FROM jsonb_array_elements(evaluation -> 'tile_results')
    LOOP
        SELECT value INTO component_tile
        FROM jsonb_array_elements(payload #> '{component_result,tile_profile}')
        WHERE value ->> 'id' = strict_tile ->> 'tile_id';
        IF component_tile IS NULL
           OR component_tile ->> 'estado' IS DISTINCT FROM
              strict_tile ->> 'assessment_state' THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END;
$$;

ALTER TABLE b3s_history.evidence_vault_sv9_evaluation_checkpoints
    ADD COLUMN shared_process_payload jsonb,
    ADD COLUMN shared_process_payload_sha256 text CHECK (
        shared_process_payload_sha256 IS NULL
        OR shared_process_payload_sha256 ~ '^[0-9a-f]{64}$'
    ),
    ADD COLUMN shared_process_payload_raw bytea,
    ADD CONSTRAINT evidence_vault_sv9_checkpoint_shared_process_check CHECK ((
        (shared_process_payload IS NULL) = (shared_process_payload_sha256 IS NULL)
        AND (shared_process_payload IS NULL) = (shared_process_payload_raw IS NULL)
        AND (
            shared_process_payload IS NULL
            OR (
                b3s_history.validate_vault_sv9_checkpoint_process_payload(
                    shared_process_payload,
                    checkpoint_payload
                )
                AND convert_from(shared_process_payload_raw, 'UTF8')::jsonb
                    = shared_process_payload
                AND encode(sha256(shared_process_payload_raw), 'hex')
                    = shared_process_payload_sha256
            )
        )
    ) IS TRUE);

CREATE TABLE b3s_history.evidence_vault_sv9_shared_analysis_snapshots (
    candidate_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    brand_id uuid NOT NULL,
    scan_run_id uuid NOT NULL,
    source_scan_id text NOT NULL CHECK (length(source_scan_id) BETWEEN 1 AND 300),
    capture_id uuid NOT NULL,
    operation_plan_id uuid NOT NULL,
    schema_version text NOT NULL CHECK (
        schema_version = 'evidence-vault-sv9-shared-analysis-snapshot-v1'
    ),
    candidate_complete_record_fingerprint text NOT NULL CHECK (
        candidate_complete_record_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    canonical_plan_fingerprint text NOT NULL CHECK (
        canonical_plan_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    current_series_fingerprint text NOT NULL CHECK (
        current_series_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    candidate_series_fingerprint text NOT NULL CHECK (
        candidate_series_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    evaluation_bundle_fingerprint text NOT NULL CHECK (
        evaluation_bundle_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    assessment_fingerprint text NOT NULL CHECK (
        assessment_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    score_fingerprint text NOT NULL CHECK (
        score_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK ((
        jsonb_typeof(payload) = 'object'
        AND payload ?& ARRAY['schema_version', 'analysis_payload', 'evaluation_components', 'component_provenance']
        AND payload - ARRAY['schema_version', 'analysis_payload', 'evaluation_components', 'component_provenance'] = '{}'::jsonb
        AND payload ->> 'schema_version' = 'evidence-vault-sv9-shared-analysis-payload-v1'
        AND jsonb_typeof(payload -> 'evaluation_components') = 'object'
        AND jsonb_typeof(payload -> 'component_provenance') = 'object'
        AND jsonb_typeof(payload -> 'analysis_payload') = 'object'
        AND payload #>> '{analysis_payload,schema_version}' = 'sv9-flow-sv9-shadow-eval-v1'
        AND jsonb_typeof(payload #> '{analysis_payload,flow}') = 'object'
        AND jsonb_typeof(payload #> '{analysis_payload,flow,candidate}') = 'object'
        AND jsonb_typeof(payload #> '{analysis_payload,sv9}') = 'object'
        AND jsonb_typeof(payload #> '{analysis_payload,sv9,assessment}') = 'object'
        AND jsonb_typeof(payload #> '{analysis_payload,sv9,result}') = 'object'
        AND jsonb_typeof(payload #> '{analysis_payload,sv9,result,assessment}') = 'object'
    ) IS TRUE),
    payload_raw bytea NOT NULL CHECK ((
        octet_length(payload_raw) > 0
        AND convert_from(payload_raw, 'UTF8')::jsonb = payload
        AND encode(sha256(payload_raw), 'hex') = payload_sha256
    ) IS TRUE),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (
        candidate_id, workspace_id, brand_id, scan_run_id, capture_id,
        operation_plan_id
    ),
    FOREIGN KEY (
        candidate_id, workspace_id, brand_id, scan_run_id, capture_id,
        operation_plan_id
    ) REFERENCES b3s_history.evidence_vault_sv9_judgment_candidates (
        id, workspace_id, brand_id, scan_run_id, capture_id, operation_plan_id
    ) ON DELETE RESTRICT
);

CREATE FUNCTION b3s_history.validate_vault_sv9_shared_analysis_insert()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    candidate record;
    component_name text;
    scanner_assessment jsonb;
    result_scanner_assessment jsonb;
    assessment_output jsonb;
BEGIN
    SELECT candidates.*, brands.canonical_domain INTO candidate
    FROM b3s_history.evidence_vault_sv9_judgment_candidates AS candidates
    JOIN b3s_history.brands AS brands
      ON brands.workspace_id = candidates.workspace_id
     AND brands.id = candidates.brand_id
    WHERE candidates.id = NEW.candidate_id
      AND candidates.workspace_id = NEW.workspace_id
      AND candidates.brand_id = NEW.brand_id
      AND candidates.scan_run_id = NEW.scan_run_id
      AND candidates.capture_id = NEW.capture_id
      AND candidates.operation_plan_id = NEW.operation_plan_id;
    IF candidate.id IS NULL
       OR candidate.source_scan_id IS DISTINCT FROM NEW.source_scan_id
       OR candidate.complete_record_fingerprint IS DISTINCT FROM NEW.candidate_complete_record_fingerprint
       OR candidate.canonical_plan_fingerprint IS DISTINCT FROM NEW.canonical_plan_fingerprint
       OR candidate.current_series_fingerprint IS DISTINCT FROM NEW.current_series_fingerprint
       OR candidate.candidate_series_fingerprint IS DISTINCT FROM NEW.candidate_series_fingerprint
       OR candidate.evaluation_bundle_fingerprint IS DISTINCT FROM NEW.evaluation_bundle_fingerprint
       OR candidate.assessment_fingerprint IS DISTINCT FROM NEW.assessment_fingerprint
       OR candidate.score_fingerprint IS DISTINCT FROM NEW.score_fingerprint THEN
        RAISE EXCEPTION 'SV9 shared analysis candidate binding is invalid';
    END IF;
    IF candidate.candidate_payload #>> '{plan,current_series_contract,evaluator_version}'
           IS DISTINCT FROM 'sv9-core-shared-evaluator-v1'
       OR candidate.candidate_payload #>> '{plan,current_series_contract,prompt_version}'
           IS DISTINCT FROM 'sv9-flow-brand-interpretation-v1.2+baldosas-v3.1-evaluator-v2+sv9-editorial-v3.1'
       OR candidate.candidate_payload #>> '{plan,current_series_contract,flow_version}'
           IS DISTINCT FROM 'sv9-flow-sv9-shadow-eval-v1'
       OR candidate.candidate_payload #>> '{plan,current_series_contract,normalization_version}'
           IS DISTINCT FROM 'vault-capture-v1' THEN
        RAISE EXCEPTION 'SV9 shared analysis series binding is invalid';
    END IF;
    scanner_assessment := NEW.payload #> '{analysis_payload,sv9,assessment}';
    result_scanner_assessment := NEW.payload #> '{analysis_payload,sv9,result,assessment}';
    assessment_output := (
        scanner_assessment - ARRAY[
            'schema_version', 'assessment_schema_version',
            'expected_tile_count', 'availability', 'reason_codes'
        ]
    ) || jsonb_build_object(
        'schema_version', scanner_assessment ->> 'assessment_schema_version'
    );
    IF NEW.payload #>> '{analysis_payload,source_run_id}' IS DISTINCT FROM NEW.source_scan_id
       OR lower(substring(
           NEW.payload #>> '{analysis_payload,url}'
           FROM '^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^/@]+@)?(?:www\.)?([^/:?#]+)'
       )) IS DISTINCT FROM candidate.canonical_domain
       OR lower(substring(
           NEW.payload #>> '{analysis_payload,sv9,result,url}'
           FROM '^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^/@]+@)?(?:www\.)?([^/:?#]+)'
       )) IS DISTINCT FROM candidate.canonical_domain
       OR result_scanner_assessment IS DISTINCT FROM scanner_assessment
       OR assessment_output IS DISTINCT FROM candidate.candidate_payload -> 'assessment'
       OR scanner_assessment ->> 'assessment_fingerprint' IS DISTINCT FROM NEW.assessment_fingerprint
       OR scanner_assessment ->> 'score_fingerprint' IS DISTINCT FROM NEW.score_fingerprint THEN
        RAISE EXCEPTION 'SV9 shared analysis assessment binding is invalid';
    END IF;
    IF NOT (NEW.payload -> 'evaluation_components') ?& ARRAY[
        'mission', 'vision', 'values', 'attributes', 'value_proposition',
        'personality', 'brand_idea', 'core_purpose', 'magnetism', 'coherencia'
    ]
       OR (NEW.payload -> 'evaluation_components') - ARRAY[
        'mission', 'vision', 'values', 'attributes', 'value_proposition',
        'personality', 'brand_idea', 'core_purpose', 'magnetism', 'coherencia'
       ] <> '{}'::jsonb THEN
        RAISE EXCEPTION 'SV9 shared analysis component set is invalid';
    END IF;
    IF NOT (NEW.payload -> 'component_provenance') ?& ARRAY[
        'mission', 'vision', 'values', 'attributes', 'value_proposition',
        'personality', 'brand_idea', 'core_purpose', 'magnetism'
    ]
       OR (NEW.payload -> 'component_provenance') - ARRAY[
        'mission', 'vision', 'values', 'attributes', 'value_proposition',
        'personality', 'brand_idea', 'core_purpose', 'magnetism'
       ] <> '{}'::jsonb THEN
        RAISE EXCEPTION 'SV9 shared analysis component provenance set is invalid';
    END IF;
    FOREACH component_name IN ARRAY ARRAY[
        'mission', 'vision', 'values', 'attributes', 'value_proposition',
        'personality', 'brand_idea', 'core_purpose', 'magnetism', 'coherencia'
    ]
    LOOP
        IF NOT b3s_history.validate_vault_sv9_component_payload(
            NEW.payload #> ARRAY['evaluation_components', component_name],
            component_name
        ) THEN
            RAISE EXCEPTION 'SV9 shared analysis component payload is invalid';
        END IF;
        IF component_name <> 'coherencia'
           AND jsonb_typeof(
               NEW.payload #> ARRAY['component_provenance', component_name]
           ) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'SV9 shared analysis component provenance is invalid';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE TRIGGER validate_vault_sv9_shared_analysis_insert
    BEFORE INSERT ON b3s_history.evidence_vault_sv9_shared_analysis_snapshots
    FOR EACH ROW EXECUTE FUNCTION b3s_history.validate_vault_sv9_shared_analysis_insert();

CREATE TRIGGER reject_vault_sv9_shared_analysis_mutation
    BEFORE UPDATE OR DELETE OR TRUNCATE
    ON b3s_history.evidence_vault_sv9_shared_analysis_snapshots
    FOR EACH STATEMENT
    EXECUTE FUNCTION b3s_history.reject_vault_sv9_judgment_candidate_mutation();

CREATE FUNCTION b3s_history.require_vault_sv9_shared_analysis_before_authority_insert()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE candidate record;
BEGIN
    IF NEW.event_type NOT IN ('adopt', 'supersede') THEN
        RETURN NEW;
    END IF;
    SELECT * INTO candidate
    FROM b3s_history.evidence_vault_sv9_judgment_candidates
    WHERE id = NEW.candidate_id
      AND workspace_id = NEW.workspace_id
      AND brand_id = NEW.brand_id;
    IF candidate.id IS NOT NULL
       AND candidate.candidate_payload #>> '{plan,current_series_contract,evaluator_version}'
           = 'sv9-core-shared-evaluator-v1'
       AND candidate.candidate_payload #>> '{plan,current_series_contract,prompt_version}'
           = 'sv9-flow-brand-interpretation-v1.2+baldosas-v3.1-evaluator-v2+sv9-editorial-v3.1'
       AND candidate.candidate_payload #>> '{plan,current_series_contract,flow_version}'
           = 'sv9-flow-sv9-shadow-eval-v1'
       AND candidate.candidate_payload #>> '{plan,current_series_contract,normalization_version}'
           = 'vault-capture-v1'
       AND NOT EXISTS (
           SELECT 1
           FROM b3s_history.evidence_vault_sv9_shared_analysis_snapshots AS snapshots
           WHERE snapshots.candidate_id = candidate.id
             AND snapshots.workspace_id = candidate.workspace_id
             AND snapshots.brand_id = candidate.brand_id
             AND snapshots.scan_run_id = candidate.scan_run_id
             AND snapshots.source_scan_id = candidate.source_scan_id
             AND snapshots.capture_id = candidate.capture_id
             AND snapshots.operation_plan_id = candidate.operation_plan_id
             AND snapshots.candidate_complete_record_fingerprint = candidate.complete_record_fingerprint
             AND snapshots.canonical_plan_fingerprint = candidate.canonical_plan_fingerprint
             AND snapshots.current_series_fingerprint = candidate.current_series_fingerprint
             AND snapshots.candidate_series_fingerprint = candidate.candidate_series_fingerprint
             AND snapshots.evaluation_bundle_fingerprint = candidate.evaluation_bundle_fingerprint
             AND snapshots.assessment_fingerprint = candidate.assessment_fingerprint
             AND snapshots.score_fingerprint = candidate.score_fingerprint
       ) THEN
        RAISE EXCEPTION 'SV9 shared analysis is required before authority adoption';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER require_vault_sv9_shared_analysis_before_authority_insert
    BEFORE INSERT ON b3s_history.evidence_vault_sv9_judgment_authority_events
    FOR EACH ROW
    EXECUTE FUNCTION b3s_history.require_vault_sv9_shared_analysis_before_authority_insert();

DO $$
DECLARE owner_role record;
BEGIN
    SELECT * INTO owner_role FROM pg_catalog.pg_roles
    WHERE rolname = 'b3s_history_vault_provenance_owner';
    IF owner_role.oid IS NULL OR owner_role.rolcanlogin OR owner_role.rolsuper
       OR owner_role.rolcreaterole OR owner_role.rolcreatedb OR owner_role.rolreplication
       OR owner_role.rolbypassrls THEN
        RAISE EXCEPTION 'SV9 shared analysis owner is unsafe';
    END IF;
    GRANT USAGE, CREATE ON SCHEMA b3s_history TO b3s_history_vault_provenance_owner;
    ALTER TABLE b3s_history.evidence_vault_sv9_shared_analysis_snapshots
        OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.validate_vault_sv9_shared_analysis_insert()
        OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.require_vault_sv9_shared_analysis_before_authority_insert()
        OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.validate_vault_sv9_component_payload(jsonb, text)
        OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.validate_vault_sv9_checkpoint_process_payload(jsonb, jsonb)
        OWNER TO b3s_history_vault_provenance_owner;
    REVOKE CREATE ON SCHEMA b3s_history FROM b3s_history_vault_provenance_owner;
END;
$$;

REVOKE ALL ON b3s_history.evidence_vault_sv9_shared_analysis_snapshots FROM PUBLIC;
REVOKE ALL ON FUNCTION b3s_history.validate_vault_sv9_shared_analysis_insert(),
                       b3s_history.require_vault_sv9_shared_analysis_before_authority_insert(),
                       b3s_history.validate_vault_sv9_component_payload(jsonb, text),
                       b3s_history.validate_vault_sv9_checkpoint_process_payload(jsonb, jsonb)
    FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'b3s_pr71_app_runtime') THEN
        REVOKE ALL ON b3s_history.evidence_vault_sv9_shared_analysis_snapshots
            FROM b3s_pr71_app_runtime;
        GRANT SELECT, INSERT
            ON b3s_history.evidence_vault_sv9_shared_analysis_snapshots
            TO b3s_pr71_app_runtime;
        IF pg_catalog.has_table_privilege(
            'b3s_pr71_app_runtime',
            'b3s_history.evidence_vault_sv9_shared_analysis_snapshots',
            'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
        ) THEN
            RAISE EXCEPTION 'SV9 shared analysis runtime grant exceeds append-only contract';
        END IF;
    END IF;
END;
$$;
