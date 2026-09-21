-- 038 persists a review decision only for one concrete reopen overlay.  The
-- approved runtime must append the linked supersede event first and append the
-- resolution second, in the same transaction.  The deferred authority-side
-- guard below makes a linked approve impossible to commit without its
-- resolution; ordinary, unlinked supersedes remain outside this review path.
-- The SECURITY DEFINER insert trigger is the write boundary.  It recomputes the
-- resolution, candidate-record, request, overlay, and authority-event hashes
-- from their canonical SQL preimages instead of trusting repeated columns.
-- An unlinked ordinary supersede cannot be identified as this review path
-- without widening 032, so 038 intentionally leaves it outside this lock and
-- exclusion boundary.  That contract limit remains P2.
-- It intentionally does not reimplement the full Python signed-delta semantic
-- validator; callers must provide an authority event emitted by that existing
-- contract, and this migration only claims the canonical bindings checked below.

CREATE FUNCTION b3s_history.evidence_vault_sv9_judgment_review_fingerprint(
    namespace text,
    payload jsonb
)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT encode(
        sha256(
            convert_to(
                b3s_history.evidence_vault_canonical_json(
                    jsonb_build_object(
                        'version', 'sv9-judgment-memory-canonical-json-v1',
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

CREATE TABLE b3s_history.evidence_vault_sv9_judgment_review_resolutions (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    brand_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    scan_run_id uuid NOT NULL,
    source_scan_id text NOT NULL CHECK (length(source_scan_id) BETWEEN 1 AND 300),
    capture_id uuid NOT NULL,
    operation_plan_id uuid NOT NULL,
    schema_version text NOT NULL CHECK (schema_version = 'evidence-vault-sv9-judgment-review-resolution-v1'),
    candidate_schema_version text NOT NULL CHECK (candidate_schema_version IN (
        'evidence-vault-sv9-judgment-candidate-v1',
        'evidence-vault-sv9-judgment-candidate-v2'
    )),
    candidate_complete_record_fingerprint text NOT NULL CHECK (candidate_complete_record_fingerprint ~ '^[0-9a-f]{64}$'),
    canonical_plan_fingerprint text NOT NULL CHECK (canonical_plan_fingerprint ~ '^[0-9a-f]{64}$'),
    current_series_fingerprint text NOT NULL CHECK (current_series_fingerprint ~ '^[0-9a-f]{64}$'),
    candidate_series_fingerprint text NOT NULL CHECK (candidate_series_fingerprint ~ '^[0-9a-f]{64}$'),
    evaluation_bundle_fingerprint text NOT NULL CHECK (evaluation_bundle_fingerprint ~ '^[0-9a-f]{64}$'),
    assessment_fingerprint text NOT NULL CHECK (assessment_fingerprint ~ '^[0-9a-f]{64}$'),
    score_fingerprint text NOT NULL CHECK (score_fingerprint ~ '^[0-9a-f]{64}$'),
    decision text NOT NULL CHECK (decision IN ('approve', 'reject')),
    expected_authority_event_id uuid NOT NULL,
    expected_authority_event_fingerprint text NOT NULL CHECK (expected_authority_event_fingerprint ~ '^[0-9a-f]{64}$'),
    expected_authority_event_sequence bigint NOT NULL CHECK (expected_authority_event_sequence > 0),
    active_authority_event_id uuid NOT NULL,
    active_authority_event_fingerprint text NOT NULL CHECK (active_authority_event_fingerprint ~ '^[0-9a-f]{64}$'),
    active_authority_event_sequence bigint NOT NULL CHECK (active_authority_event_sequence > 0),
    evaluated_authority_event_id uuid NOT NULL,
    evaluated_authority_event_fingerprint text NOT NULL CHECK (evaluated_authority_event_fingerprint ~ '^[0-9a-f]{64}$'),
    evaluated_authority_sequence bigint NOT NULL CHECK (evaluated_authority_sequence > 0),
    reopen_authority_event_id uuid NOT NULL,
    reopen_authority_event_fingerprint text NOT NULL CHECK (reopen_authority_event_fingerprint ~ '^[0-9a-f]{64}$'),
    reopen_authority_event_sequence bigint NOT NULL CHECK (reopen_authority_event_sequence > 0),
    reopen_active_parent_event_id uuid NOT NULL,
    reopen_active_parent_event_fingerprint text NOT NULL CHECK (reopen_active_parent_event_fingerprint ~ '^[0-9a-f]{64}$'),
    reopen_active_parent_event_sequence bigint NOT NULL CHECK (reopen_active_parent_event_sequence > 0),
    reopen_delta_fingerprint text NOT NULL CHECK (reopen_delta_fingerprint ~ '^[0-9a-f]{64}$'),
    reopen_review_overlay_fingerprint text NOT NULL CHECK (reopen_review_overlay_fingerprint ~ '^[0-9a-f]{64}$'),
    successor_authority_event_id uuid,
    successor_authority_event_fingerprint text CHECK (successor_authority_event_fingerprint IS NULL OR successor_authority_event_fingerprint ~ '^[0-9a-f]{64}$'),
    successor_authority_event_sequence bigint CHECK (successor_authority_event_sequence IS NULL OR successor_authority_event_sequence > 0),
    resolution_key_hash text NOT NULL CHECK (resolution_key_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (workspace_id, brand_id, resolution_key_hash),
    UNIQUE (workspace_id, brand_id, candidate_id),
    CHECK ((successor_authority_event_id IS NULL) = (successor_authority_event_fingerprint IS NULL)),
    CHECK ((successor_authority_event_id IS NULL) = (successor_authority_event_sequence IS NULL)),
    CHECK (reopen_authority_event_id = expected_authority_event_id),
    CHECK (reopen_authority_event_fingerprint = expected_authority_event_fingerprint),
    CHECK (reopen_authority_event_sequence = expected_authority_event_sequence),
    CHECK (reopen_active_parent_event_id = active_authority_event_id),
    CHECK (reopen_active_parent_event_fingerprint = active_authority_event_fingerprint),
    CHECK (reopen_active_parent_event_sequence = active_authority_event_sequence),
    CHECK (evaluated_authority_event_id = reopen_active_parent_event_id),
    CHECK (evaluated_authority_event_fingerprint = reopen_active_parent_event_fingerprint),
    CHECK (evaluated_authority_sequence = reopen_active_parent_event_sequence),
    CHECK (reopen_active_parent_event_sequence < reopen_authority_event_sequence),
    CHECK (
        (decision = 'approve'
            AND successor_authority_event_id IS NOT NULL
            AND successor_authority_event_fingerprint IS NOT NULL
            AND successor_authority_event_sequence IS NOT NULL)
        OR (decision = 'reject'
            AND successor_authority_event_id IS NULL
            AND successor_authority_event_fingerprint IS NULL
            AND successor_authority_event_sequence IS NULL)
    ),
    FOREIGN KEY (candidate_id, workspace_id, brand_id, scan_run_id, capture_id, operation_plan_id)
        REFERENCES b3s_history.evidence_vault_sv9_judgment_candidates
            (id, workspace_id, brand_id, scan_run_id, capture_id, operation_plan_id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, brand_id, expected_authority_event_id)
        REFERENCES b3s_history.evidence_vault_sv9_judgment_authority_events
            (workspace_id, brand_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, brand_id, active_authority_event_id)
        REFERENCES b3s_history.evidence_vault_sv9_judgment_authority_events
            (workspace_id, brand_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, brand_id, evaluated_authority_event_id)
        REFERENCES b3s_history.evidence_vault_sv9_judgment_authority_events
            (workspace_id, brand_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, brand_id, reopen_authority_event_id)
        REFERENCES b3s_history.evidence_vault_sv9_judgment_authority_events
            (workspace_id, brand_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, brand_id, reopen_active_parent_event_id)
        REFERENCES b3s_history.evidence_vault_sv9_judgment_authority_events
            (workspace_id, brand_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, brand_id, successor_authority_event_id)
        REFERENCES b3s_history.evidence_vault_sv9_judgment_authority_events
            (workspace_id, brand_id, id) ON DELETE RESTRICT
);

CREATE FUNCTION b3s_history.evidence_vault_sv9_judgment_candidate_complete_record_fingerprint(
    candidate_row b3s_history.evidence_vault_sv9_judgment_candidates
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, b3s_history
AS $$
DECLARE
    payload jsonb := candidate_row.candidate_payload;
    candidate_version text := payload ->> 'schema_version';
    record_namespace text;
    expected_keys text[];
    expected_bundle text;
    expected_complete text;
BEGIN
    IF candidate_row.schema_version IS DISTINCT FROM candidate_version THEN
        RAISE EXCEPTION 'SV9 judgment candidate payload schema is not canonical';
    END IF;
    IF candidate_version = 'evidence-vault-sv9-judgment-candidate-v1' THEN
        record_namespace := 'evidence-vault-sv9-judgment-candidate-record-v1';
        expected_keys := ARRAY[
            'schema_version', 'plan', 'canonical_plan_fingerprint',
            'current_series_fingerprint', 'candidate_series_fingerprint',
            'component_evaluations', 'evidence_bindings',
            'candidate_tile_judgments', 'candidate_component_sentinels',
            'assessment', 'telemetry', 'evaluation_bundle_fingerprint',
            'assessment_fingerprint', 'score_fingerprint',
            'complete_record_fingerprint'
        ];
    ELSIF candidate_version = 'evidence-vault-sv9-judgment-candidate-v2' THEN
        record_namespace := 'evidence-vault-sv9-judgment-candidate-record-v2';
        expected_keys := ARRAY[
            'schema_version', 'plan', 'canonical_plan_fingerprint',
            'current_series_fingerprint', 'candidate_series_fingerprint',
            'component_evaluations', 'evidence_bindings',
            'candidate_tile_judgments', 'candidate_component_sentinels',
            'assessment', 'telemetry', 'evaluation_bundle_fingerprint',
            'assessment_fingerprint', 'score_fingerprint',
            'complete_record_fingerprint', 'authoritative_relation_witness'
        ];
    ELSE
        RAISE EXCEPTION 'SV9 judgment candidate payload schema is unsupported';
    END IF;

    IF b3s_history.evidence_vault_json_has_exact_keys(payload, expected_keys) IS NOT TRUE THEN
        RAISE EXCEPTION 'SV9 judgment candidate payload shape is not canonical';
    END IF;
    IF jsonb_typeof(payload -> 'assessment') IS DISTINCT FROM 'object'
       OR jsonb_typeof(payload -> 'component_evaluations') IS DISTINCT FROM 'array'
       OR jsonb_typeof(payload -> 'evidence_bindings') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'SV9 judgment candidate payload types are not canonical';
    END IF;

    IF payload ->> 'canonical_plan_fingerprint' IS DISTINCT FROM candidate_row.canonical_plan_fingerprint
       OR payload ->> 'current_series_fingerprint' IS DISTINCT FROM candidate_row.current_series_fingerprint
       OR payload ->> 'candidate_series_fingerprint' IS DISTINCT FROM candidate_row.candidate_series_fingerprint
       OR payload ->> 'evaluation_bundle_fingerprint' IS DISTINCT FROM candidate_row.evaluation_bundle_fingerprint
       OR payload ->> 'assessment_fingerprint' IS DISTINCT FROM candidate_row.assessment_fingerprint
       OR payload ->> 'score_fingerprint' IS DISTINCT FROM candidate_row.score_fingerprint
       OR payload ->> 'complete_record_fingerprint' IS DISTINCT FROM candidate_row.complete_record_fingerprint
       OR payload -> 'assessment' ->> 'assessment_fingerprint' IS DISTINCT FROM candidate_row.assessment_fingerprint
       OR payload -> 'assessment' ->> 'score_fingerprint' IS DISTINCT FROM candidate_row.score_fingerprint THEN
        RAISE EXCEPTION 'SV9 judgment candidate payload fingerprints are not canonical';
    END IF;

    expected_bundle := b3s_history.evidence_vault_sv9_judgment_review_fingerprint(
        'sv9-judgment-evaluation-bundle-v1',
        jsonb_build_object(
            'canonical_plan_fingerprint', payload ->> 'canonical_plan_fingerprint',
            'evaluations', payload -> 'component_evaluations'
        )
    );
    IF expected_bundle IS DISTINCT FROM candidate_row.evaluation_bundle_fingerprint THEN
        RAISE EXCEPTION 'SV9 judgment candidate evaluation bundle is not canonical';
    END IF;

    expected_complete := b3s_history.evidence_vault_sv9_judgment_review_fingerprint(
        record_namespace,
        payload - 'complete_record_fingerprint'
    );
    IF expected_complete IS DISTINCT FROM candidate_row.complete_record_fingerprint THEN
        RAISE EXCEPTION 'SV9 judgment candidate complete record is not canonical';
    END IF;
    RETURN expected_complete;
END;
$$;

CREATE FUNCTION b3s_history.evidence_vault_sv9_judgment_authority_event_fingerprint(
    event_row b3s_history.evidence_vault_sv9_judgment_authority_events
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, b3s_history
AS $$
DECLARE
    request jsonb := event_row.event_payload -> 'request';
    event_schema text := event_row.event_payload ->> 'schema_version';
    request_schema text := request ->> 'schema_version';
    predecessor b3s_history.evidence_vault_sv9_judgment_authority_events%ROWTYPE;
    active_parent b3s_history.evidence_vault_sv9_judgment_authority_events%ROWTYPE;
    candidate b3s_history.evidence_vault_sv9_judgment_candidates%ROWTYPE;
    expected_request text;
    expected_idempotency text;
    candidate_complete text;
    event_identity jsonb;
    expected_keys text[];
BEGIN
    IF jsonb_typeof(request) IS DISTINCT FROM 'object'
       OR event_schema IS NULL
       OR request_schema IS NULL THEN
        RAISE EXCEPTION 'SV9 judgment authority event request is not canonical';
    END IF;
    IF request_schema = 'evidence-vault-sv9-judgment-authority-request-v1' THEN
        expected_keys := ARRAY[
            'schema_version', 'action', 'candidate_id', 'source_scan_id',
            'expected_predecessor_event_fingerprint', 'delta_fingerprint'
        ];
    ELSIF request_schema = 'evidence-vault-sv9-judgment-authority-request-v2' THEN
        expected_keys := ARRAY[
            'schema_version', 'action', 'candidate_id', 'source_scan_id',
            'expected_predecessor_event_fingerprint', 'delta_fingerprint',
            'workset_partition_fingerprint'
        ];
    ELSE
        RAISE EXCEPTION 'SV9 judgment authority request schema is unsupported';
    END IF;
    IF b3s_history.evidence_vault_json_has_exact_keys(request, expected_keys) IS NOT TRUE THEN
        RAISE EXCEPTION 'SV9 judgment authority request shape is not canonical';
    END IF;
    IF event_row.predecessor_event_id IS NOT NULL THEN
        SELECT * INTO predecessor
        FROM b3s_history.evidence_vault_sv9_judgment_authority_events
        WHERE workspace_id = event_row.workspace_id
          AND brand_id = event_row.brand_id
          AND id = event_row.predecessor_event_id;
    END IF;
    IF event_row.active_parent_event_id IS NOT NULL THEN
        SELECT * INTO active_parent
        FROM b3s_history.evidence_vault_sv9_judgment_authority_events
        WHERE workspace_id = event_row.workspace_id
          AND brand_id = event_row.brand_id
          AND id = event_row.active_parent_event_id;
    END IF;
    IF event_row.predecessor_event_id IS NULL THEN
        IF request -> 'expected_predecessor_event_fingerprint' IS DISTINCT FROM 'null'::jsonb THEN
            RAISE EXCEPTION 'SV9 judgment authority predecessor fingerprint is not canonical';
        END IF;
    ELSIF predecessor.id IS NULL
       OR request ->> 'expected_predecessor_event_fingerprint' IS DISTINCT FROM predecessor.event_fingerprint THEN
        RAISE EXCEPTION 'SV9 judgment authority predecessor binding is not canonical';
    END IF;
    IF event_row.active_parent_event_id IS NOT NULL
       AND (active_parent.id IS NULL
            OR active_parent.event_fingerprint IS NULL) THEN
        RAISE EXCEPTION 'SV9 judgment authority active parent binding is not canonical';
    END IF;

    IF event_row.event_type IN ('adopt', 'supersede') THEN
        IF event_schema <> 'evidence-vault-sv9-judgment-authority-event-v1'
           OR request_schema <> 'evidence-vault-sv9-judgment-authority-request-v1'
           OR request ->> 'action' <> 'adopt_candidate'
           OR request ->> 'candidate_id' IS DISTINCT FROM event_row.candidate_id::text
           OR request -> 'delta_fingerprint' IS DISTINCT FROM 'null'::jsonb
           OR event_row.delta_fingerprint IS NOT NULL THEN
            RAISE EXCEPTION 'SV9 judgment authority candidate event request is not canonical';
        END IF;
        SELECT * INTO candidate
        FROM b3s_history.evidence_vault_sv9_judgment_candidates
        WHERE id = event_row.candidate_id
          AND workspace_id = event_row.workspace_id
          AND brand_id = event_row.brand_id
          AND scan_run_id = event_row.candidate_scan_run_id
          AND capture_id = event_row.candidate_capture_id
          AND operation_plan_id = event_row.candidate_operation_plan_id;
        IF candidate.id IS NULL THEN
            RAISE EXCEPTION 'SV9 judgment authority candidate event candidate is unavailable';
        END IF;
        candidate_complete := b3s_history.evidence_vault_sv9_judgment_candidate_complete_record_fingerprint(candidate);
        IF event_row.evaluation_bundle_fingerprint IS DISTINCT FROM candidate.evaluation_bundle_fingerprint
           OR event_row.canonical_plan_fingerprint IS DISTINCT FROM candidate.canonical_plan_fingerprint
           OR event_row.current_series_fingerprint IS DISTINCT FROM candidate.current_series_fingerprint
           OR event_row.candidate_series_fingerprint IS DISTINCT FROM candidate.candidate_series_fingerprint
           OR event_row.assessment_fingerprint IS DISTINCT FROM candidate.assessment_fingerprint
           OR event_row.score_fingerprint IS DISTINCT FROM candidate.score_fingerprint THEN
            RAISE EXCEPTION 'SV9 judgment authority candidate fingerprints are not canonical';
        END IF;
        IF event_row.event_payload - 'sv9_review_resolution' IS DISTINCT FROM
           jsonb_build_object('schema_version', event_schema, 'request', request) THEN
            RAISE EXCEPTION 'SV9 judgment authority candidate event payload is not canonical';
        END IF;
        IF request ->> 'source_scan_id' IS DISTINCT FROM candidate.source_scan_id THEN
            RAISE EXCEPTION 'SV9 judgment authority candidate event source is not canonical';
        END IF;
    ELSIF event_row.event_type = 'reopen' THEN
        IF event_schema NOT IN (
               'evidence-vault-sv9-judgment-authority-event-v1',
               'evidence-vault-sv9-judgment-authority-event-v2'
           )
           OR request_schema <> (CASE
               WHEN event_schema = 'evidence-vault-sv9-judgment-authority-event-v2'
               THEN 'evidence-vault-sv9-judgment-authority-request-v2'
               ELSE 'evidence-vault-sv9-judgment-authority-request-v1'
           END)
           OR request ->> 'action' <> 'reopen_authority'
           OR request -> 'candidate_id' IS DISTINCT FROM 'null'::jsonb
           OR event_row.candidate_id IS NOT NULL
           OR event_row.delta_fingerprint IS DISTINCT FROM request ->> 'delta_fingerprint'
           OR event_row.event_payload ->> 'review_state' <> 'pending'
           OR jsonb_typeof(event_row.event_payload -> 'signed_delta') IS DISTINCT FROM 'object'
           OR event_row.event_payload -> 'signed_delta' ->> 'canonical_delta_fingerprint'
                IS DISTINCT FROM event_row.delta_fingerprint THEN
            RAISE EXCEPTION 'SV9 judgment authority reopen event payload is not canonical';
        END IF;
        IF event_schema = 'evidence-vault-sv9-judgment-authority-event-v2'
           AND event_row.event_payload ->> 'workset_partition_fingerprint'
                IS DISTINCT FROM request ->> 'workset_partition_fingerprint' THEN
            RAISE EXCEPTION 'SV9 judgment authority reopen partition is not canonical';
        END IF;
        IF event_schema = 'evidence-vault-sv9-judgment-authority-event-v1'
           AND (event_row.event_payload ? 'workset_partition'
                OR event_row.event_payload ? 'workset_partition_fingerprint') THEN
            RAISE EXCEPTION 'SV9 judgment authority v1 reopen contains v2 fields';
        END IF;
        IF event_schema = 'evidence-vault-sv9-judgment-authority-event-v2'
           AND jsonb_typeof(event_row.event_payload -> 'workset_partition') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'SV9 judgment authority v2 reopen partition is not canonical';
        END IF;
        IF event_row.event_payload - 'workset_partition' - 'workset_partition_fingerprint'
             IS DISTINCT FROM jsonb_build_object(
                 'schema_version', event_schema,
                 'request', request,
                 'review_state', 'pending',
                 'signed_delta', event_row.event_payload -> 'signed_delta'
             ) THEN
            RAISE EXCEPTION 'SV9 judgment authority reopen payload is not canonical';
        END IF;
    ELSE
        RAISE EXCEPTION 'SV9 judgment authority event type is unsupported';
    END IF;

    expected_request := b3s_history.evidence_vault_sv9_judgment_review_fingerprint(
        request_schema,
        request
    );
    expected_idempotency := b3s_history.evidence_vault_sv9_judgment_review_fingerprint(
        CASE
            WHEN request_schema = 'evidence-vault-sv9-judgment-authority-request-v2'
            THEN 'evidence-vault-sv9-authority-application-idempotency-v2'
            ELSE 'evidence-vault-sv9-authority-application-idempotency-v1'
        END,
        jsonb_build_object(
            'action', request -> 'action',
            'source_scan_id', request -> 'source_scan_id',
            'candidate_id', request -> 'candidate_id',
            'canonical_delta_fingerprint', request -> 'delta_fingerprint',
            'expected_predecessor_event_fingerprint', request -> 'expected_predecessor_event_fingerprint'
        ) || CASE
            WHEN request_schema = 'evidence-vault-sv9-judgment-authority-request-v2'
            THEN jsonb_build_object('workset_partition_fingerprint', request -> 'workset_partition_fingerprint')
            ELSE '{}'::jsonb
        END
    );
    IF event_row.request_fingerprint IS DISTINCT FROM expected_request
       OR event_row.idempotency_key_hash IS DISTINCT FROM expected_idempotency
       OR (event_row.predecessor_event_id IS NULL AND request -> 'expected_predecessor_event_fingerprint' IS DISTINCT FROM 'null'::jsonb)
       OR (event_row.predecessor_event_id IS NOT NULL AND request ->> 'expected_predecessor_event_fingerprint' IS DISTINCT FROM predecessor.event_fingerprint)
       OR (event_row.predecessor_event_id IS NULL AND predecessor.id IS NOT NULL)
       OR (event_row.predecessor_event_id IS NOT NULL AND predecessor.id IS NULL)
       OR (event_row.active_parent_event_id IS NOT NULL AND active_parent.id IS NULL) THEN
        RAISE EXCEPTION 'SV9 judgment authority request fingerprints are not canonical';
    END IF;

    event_identity := jsonb_build_object(
        'event_id', event_row.id::text,
        'event_type', event_row.event_type,
        'sequence', event_row.sequence,
        'predecessor_event_fingerprint', predecessor.event_fingerprint,
        'active_parent_event_fingerprint', active_parent.event_fingerprint,
        'candidate_id', event_row.candidate_id::text,
        'candidate_complete_record_fingerprint', candidate_complete,
        'evaluation_bundle_fingerprint', event_row.evaluation_bundle_fingerprint,
        'canonical_plan_fingerprint', event_row.canonical_plan_fingerprint,
        'current_series_fingerprint', event_row.current_series_fingerprint,
        'candidate_series_fingerprint', event_row.candidate_series_fingerprint,
        'assessment_fingerprint', event_row.assessment_fingerprint,
        'score_fingerprint', event_row.score_fingerprint,
        'delta_fingerprint', event_row.delta_fingerprint,
        'request_fingerprint', event_row.request_fingerprint,
        'idempotency_key_hash', event_row.idempotency_key_hash
    );
    IF event_schema = 'evidence-vault-sv9-judgment-authority-event-v2' THEN
        event_identity := event_identity || jsonb_build_object(
            'workset_partition_fingerprint', event_row.event_payload ->> 'workset_partition_fingerprint'
        );
    END IF;
    RETURN b3s_history.evidence_vault_sv9_judgment_review_fingerprint(
        event_schema,
        event_identity
    );
END;
$$;

CREATE FUNCTION b3s_history.validate_vault_sv9_judgment_review_resolution_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, b3s_history
AS $$
DECLARE
    candidate b3s_history.evidence_vault_sv9_judgment_candidates%ROWTYPE;
    expected_event b3s_history.evidence_vault_sv9_judgment_authority_events%ROWTYPE;
    active_event b3s_history.evidence_vault_sv9_judgment_authority_events%ROWTYPE;
    successor_event b3s_history.evidence_vault_sv9_judgment_authority_events%ROWTYPE;
    latest_event b3s_history.evidence_vault_sv9_judgment_authority_events%ROWTYPE;
    expected_active_event_id uuid;
    expected_candidate_complete text;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended(NEW.workspace_id::text || ':' || NEW.brand_id::text, 0)
    );

    IF NEW.decision NOT IN ('approve', 'reject') THEN
        RAISE EXCEPTION 'SV9 judgment review resolution decision is invalid';
    END IF;
    IF (NEW.successor_authority_event_id IS NULL) <> (NEW.successor_authority_event_fingerprint IS NULL)
       OR (NEW.successor_authority_event_id IS NULL) <> (NEW.successor_authority_event_sequence IS NULL) THEN
        RAISE EXCEPTION 'SV9 judgment review resolution successor binding is incomplete';
    END IF;
    IF (NEW.decision = 'approve' AND NEW.successor_authority_event_id IS NULL)
       OR (NEW.decision = 'reject' AND NEW.successor_authority_event_id IS NOT NULL) THEN
        RAISE EXCEPTION 'SV9 judgment review resolution decision and successor disagree';
    END IF;

    IF NEW.resolution_key_hash IS DISTINCT FROM
       b3s_history.evidence_vault_sv9_judgment_review_fingerprint(
           'evidence-vault-sv9-judgment-review-resolution-key-v1',
           jsonb_build_object(
               'schema_version', NEW.schema_version,
               'workspace_id', NEW.workspace_id::text,
               'brand_id', NEW.brand_id::text,
               'candidate_id', NEW.candidate_id::text,
               'scan_run_id', NEW.scan_run_id::text,
               'capture_id', NEW.capture_id::text,
               'operation_plan_id', NEW.operation_plan_id::text,
               'source_scan_id', NEW.source_scan_id,
               'candidate_schema_version', NEW.candidate_schema_version,
               'candidate_complete_record_fingerprint', NEW.candidate_complete_record_fingerprint,
               'canonical_plan_fingerprint', NEW.canonical_plan_fingerprint,
               'current_series_fingerprint', NEW.current_series_fingerprint,
               'candidate_series_fingerprint', NEW.candidate_series_fingerprint,
               'evaluation_bundle_fingerprint', NEW.evaluation_bundle_fingerprint,
               'assessment_fingerprint', NEW.assessment_fingerprint,
               'score_fingerprint', NEW.score_fingerprint,
               'decision', NEW.decision,
               'expected_authority_event_id', NEW.expected_authority_event_id::text,
               'expected_authority_event_fingerprint', NEW.expected_authority_event_fingerprint,
               'expected_authority_event_sequence', NEW.expected_authority_event_sequence,
               'active_authority_event_id', NEW.active_authority_event_id::text,
               'active_authority_event_fingerprint', NEW.active_authority_event_fingerprint,
               'active_authority_event_sequence', NEW.active_authority_event_sequence,
               'evaluated_authority_event_id', NEW.evaluated_authority_event_id::text,
               'evaluated_authority_event_fingerprint', NEW.evaluated_authority_event_fingerprint,
               'evaluated_authority_sequence', NEW.evaluated_authority_sequence,
               'reopen_authority_event_id', NEW.reopen_authority_event_id::text,
               'reopen_authority_event_fingerprint', NEW.reopen_authority_event_fingerprint,
               'reopen_authority_event_sequence', NEW.reopen_authority_event_sequence,
               'reopen_active_parent_event_id', NEW.reopen_active_parent_event_id::text,
               'reopen_active_parent_event_fingerprint', NEW.reopen_active_parent_event_fingerprint,
               'reopen_active_parent_event_sequence', NEW.reopen_active_parent_event_sequence,
               'reopen_delta_fingerprint', NEW.reopen_delta_fingerprint,
               'reopen_review_overlay_fingerprint', NEW.reopen_review_overlay_fingerprint,
               'successor_authority_event_id', NEW.successor_authority_event_id::text,
               'successor_authority_event_fingerprint', NEW.successor_authority_event_fingerprint,
               'successor_authority_event_sequence', NEW.successor_authority_event_sequence
           )
       ) THEN
        RAISE EXCEPTION 'SV9 judgment review resolution key hash is not canonical';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM b3s_history.scan_runs AS scans
        WHERE scans.id = NEW.scan_run_id
          AND scans.workspace_id = NEW.workspace_id
          AND scans.brand_id = NEW.brand_id
          AND scans.source_scan_id = NEW.source_scan_id
    ) THEN
        RAISE EXCEPTION 'SV9 judgment review resolution source provenance is invalid';
    END IF;

    SELECT * INTO candidate
    FROM b3s_history.evidence_vault_sv9_judgment_candidates
    WHERE id = NEW.candidate_id
      AND workspace_id = NEW.workspace_id
      AND brand_id = NEW.brand_id
      AND scan_run_id = NEW.scan_run_id
      AND capture_id = NEW.capture_id
      AND operation_plan_id = NEW.operation_plan_id;
    IF candidate.id IS NOT NULL THEN
        expected_candidate_complete :=
            b3s_history.evidence_vault_sv9_judgment_candidate_complete_record_fingerprint(candidate);
    END IF;
    IF candidate.id IS NULL
       OR candidate.source_scan_id IS DISTINCT FROM NEW.source_scan_id
       OR candidate.schema_version IS DISTINCT FROM NEW.candidate_schema_version
       OR expected_candidate_complete IS DISTINCT FROM NEW.candidate_complete_record_fingerprint
       OR candidate.complete_record_fingerprint IS DISTINCT FROM NEW.candidate_complete_record_fingerprint
       OR candidate.canonical_plan_fingerprint IS DISTINCT FROM NEW.canonical_plan_fingerprint
       OR candidate.current_series_fingerprint IS DISTINCT FROM NEW.current_series_fingerprint
       OR candidate.candidate_series_fingerprint IS DISTINCT FROM NEW.candidate_series_fingerprint
       OR candidate.evaluation_bundle_fingerprint IS DISTINCT FROM NEW.evaluation_bundle_fingerprint
       OR candidate.assessment_fingerprint IS DISTINCT FROM NEW.assessment_fingerprint
       OR candidate.score_fingerprint IS DISTINCT FROM NEW.score_fingerprint
       OR candidate.authority IS DISTINCT FROM 'pending'
       OR candidate.review_state IS DISTINCT FROM 'none'
       OR candidate.lifecycle_state IS DISTINCT FROM 'active'
       OR candidate.runtime_effect IS DISTINCT FROM 'shadow_only' THEN
        RAISE EXCEPTION 'SV9 judgment review resolution candidate provenance is invalid';
    END IF;
    IF candidate.schema_version = 'evidence-vault-sv9-judgment-candidate-v2'
       AND (
           jsonb_typeof(candidate.candidate_payload -> 'authoritative_relation_witness') IS DISTINCT FROM 'object'
           OR jsonb_typeof(candidate.candidate_payload -> 'authoritative_relation_witness' -> 'operational_witness') IS DISTINCT FROM 'object'
           OR candidate.candidate_payload -> 'authoritative_relation_witness' -> 'operational_witness' ->> 'adoption_event_id'
                IS DISTINCT FROM NEW.evaluated_authority_event_id::text
           OR candidate.candidate_payload -> 'authoritative_relation_witness' -> 'operational_witness' ->> 'adoption_sequence'
                IS DISTINCT FROM NEW.evaluated_authority_sequence::text
       ) THEN
        RAISE EXCEPTION 'SV9 judgment review resolution candidate witness authority binding is invalid';
    END IF;
    IF NEW.decision = 'reject' AND EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_sv9_judgment_authority_events AS adopted
        WHERE adopted.workspace_id = NEW.workspace_id
          AND adopted.brand_id = NEW.brand_id
          AND adopted.candidate_id = NEW.candidate_id
          AND adopted.event_type IN ('adopt', 'supersede')
    ) THEN
        RAISE EXCEPTION 'SV9 judgment review resolution cannot reject an adopted candidate';
    END IF;

    SELECT * INTO expected_event
    FROM b3s_history.evidence_vault_sv9_judgment_authority_events
    WHERE workspace_id = NEW.workspace_id
      AND brand_id = NEW.brand_id
      AND id = NEW.reopen_authority_event_id;
    IF expected_event.id IS NULL
       OR expected_event.event_type <> 'reopen'
       OR expected_event.event_fingerprint IS DISTINCT FROM NEW.expected_authority_event_fingerprint
       OR expected_event.sequence IS DISTINCT FROM NEW.expected_authority_event_sequence
       OR expected_event.id IS DISTINCT FROM NEW.reopen_authority_event_id
       OR expected_event.event_fingerprint IS DISTINCT FROM NEW.reopen_authority_event_fingerprint
       OR expected_event.sequence IS DISTINCT FROM NEW.reopen_authority_event_sequence
       OR expected_event.delta_fingerprint IS DISTINCT FROM NEW.reopen_delta_fingerprint
       OR expected_event.current_series_fingerprint IS DISTINCT FROM NEW.current_series_fingerprint
       OR expected_event.event_payload ->> 'review_state' IS DISTINCT FROM 'pending'
       OR jsonb_typeof(expected_event.event_payload -> 'signed_delta') IS DISTINCT FROM 'object'
       OR expected_event.event_payload -> 'signed_delta' ->> 'canonical_delta_fingerprint'
            IS DISTINCT FROM NEW.reopen_delta_fingerprint
       OR expected_event.event_payload -> 'request' ->> 'action' IS DISTINCT FROM 'reopen_authority'
       OR expected_event.event_payload -> 'request' ->> 'source_scan_id'
            IS DISTINCT FROM NEW.source_scan_id
       OR expected_event.event_payload -> 'request' ->> 'delta_fingerprint'
            IS DISTINCT FROM NEW.reopen_delta_fingerprint
       OR NEW.reopen_review_overlay_fingerprint IS DISTINCT FROM
            b3s_history.evidence_vault_sv9_judgment_review_fingerprint(
                'evidence-vault-sv9-review-overlay-v1', expected_event.event_payload
            )
       OR expected_event.event_fingerprint IS DISTINCT FROM
            b3s_history.evidence_vault_sv9_judgment_authority_event_fingerprint(expected_event) THEN
        RAISE EXCEPTION 'SV9 judgment review resolution expected authority CAS is stale';
    END IF;

    SELECT * INTO active_event
    FROM b3s_history.evidence_vault_sv9_judgment_authority_events
    WHERE workspace_id = NEW.workspace_id
      AND brand_id = NEW.brand_id
      AND id = NEW.reopen_active_parent_event_id;
    IF active_event.id IS NULL
       OR active_event.event_fingerprint IS DISTINCT FROM NEW.active_authority_event_fingerprint
       OR active_event.sequence IS DISTINCT FROM NEW.active_authority_event_sequence
       OR active_event.id IS DISTINCT FROM NEW.reopen_active_parent_event_id
       OR active_event.event_fingerprint IS DISTINCT FROM NEW.reopen_active_parent_event_fingerprint
       OR active_event.sequence IS DISTINCT FROM NEW.reopen_active_parent_event_sequence
       OR active_event.event_type NOT IN ('adopt', 'supersede') THEN
        RAISE EXCEPTION 'SV9 judgment review resolution active authority binding is invalid';
    END IF;
    IF active_event.event_fingerprint IS DISTINCT FROM
       b3s_history.evidence_vault_sv9_judgment_authority_event_fingerprint(active_event) THEN
        RAISE EXCEPTION 'SV9 judgment review resolution active authority fingerprint is not canonical';
    END IF;
    IF NEW.evaluated_authority_event_id IS DISTINCT FROM active_event.id
       OR NEW.evaluated_authority_event_fingerprint IS DISTINCT FROM active_event.event_fingerprint
       OR NEW.evaluated_authority_sequence IS DISTINCT FROM active_event.sequence THEN
        RAISE EXCEPTION 'SV9 judgment review resolution evaluated authority binding is invalid';
    END IF;

    expected_active_event_id := expected_event.active_parent_event_id;
    IF expected_active_event_id IS NULL
       OR expected_active_event_id IS DISTINCT FROM active_event.id
       OR expected_event.predecessor_event_id IS NULL
       OR expected_event.active_parent_event_id IS DISTINCT FROM NEW.reopen_active_parent_event_id
       OR expected_event.sequence <= active_event.sequence
       OR expected_event.active_parent_event_id IS DISTINCT FROM active_event.id
       OR expected_event.current_series_fingerprint IS DISTINCT FROM active_event.current_series_fingerprint
       OR active_event.current_series_fingerprint IS DISTINCT FROM NEW.current_series_fingerprint THEN
        RAISE EXCEPTION 'SV9 judgment review resolution active authority lineage is invalid';
    END IF;

    SELECT * INTO latest_event
    FROM b3s_history.evidence_vault_sv9_judgment_authority_events
    WHERE workspace_id = NEW.workspace_id
      AND brand_id = NEW.brand_id
    ORDER BY sequence DESC
    LIMIT 1;
    IF latest_event.id IS NULL THEN
        RAISE EXCEPTION 'SV9 judgment review resolution requires an authority head';
    END IF;

    IF NEW.decision = 'reject' THEN
        IF latest_event.id IS DISTINCT FROM expected_event.id
           OR latest_event.event_fingerprint IS DISTINCT FROM expected_event.event_fingerprint
           OR latest_event.sequence IS DISTINCT FROM expected_event.sequence THEN
            RAISE EXCEPTION 'SV9 judgment review resolution reject CAS is stale';
        END IF;
        RETURN NEW;
    END IF;

    SELECT * INTO successor_event
    FROM b3s_history.evidence_vault_sv9_judgment_authority_events
    WHERE workspace_id = NEW.workspace_id
      AND brand_id = NEW.brand_id
      AND id = NEW.successor_authority_event_id;
    IF successor_event.id IS NULL
       OR successor_event.event_fingerprint IS DISTINCT FROM NEW.successor_authority_event_fingerprint
       OR successor_event.sequence IS DISTINCT FROM NEW.successor_authority_event_sequence
       OR successor_event.event_type <> 'supersede'
       OR successor_event.predecessor_event_id IS DISTINCT FROM expected_event.id
       OR successor_event.active_parent_event_id IS DISTINCT FROM active_event.id
       OR successor_event.candidate_id IS DISTINCT FROM NEW.candidate_id
       OR successor_event.candidate_scan_run_id IS DISTINCT FROM NEW.scan_run_id
       OR successor_event.candidate_capture_id IS DISTINCT FROM NEW.capture_id
       OR successor_event.candidate_operation_plan_id IS DISTINCT FROM NEW.operation_plan_id
       OR successor_event.current_series_fingerprint IS DISTINCT FROM NEW.current_series_fingerprint
       OR successor_event.candidate_series_fingerprint IS DISTINCT FROM NEW.candidate_series_fingerprint
       OR successor_event.evaluation_bundle_fingerprint IS DISTINCT FROM NEW.evaluation_bundle_fingerprint
       OR successor_event.canonical_plan_fingerprint IS DISTINCT FROM NEW.canonical_plan_fingerprint
       OR successor_event.assessment_fingerprint IS DISTINCT FROM NEW.assessment_fingerprint
       OR successor_event.score_fingerprint IS DISTINCT FROM NEW.score_fingerprint
       OR jsonb_typeof(successor_event.event_payload -> 'sv9_review_resolution') IS DISTINCT FROM 'object'
       OR b3s_history.evidence_vault_json_has_exact_keys(
              successor_event.event_payload -> 'sv9_review_resolution',
              ARRAY['resolution_id', 'resolution_key_hash']
          ) IS NOT TRUE
       OR successor_event.event_payload -> 'sv9_review_resolution' ->> 'resolution_id'
            IS DISTINCT FROM NEW.id::text
       OR successor_event.event_payload -> 'sv9_review_resolution' ->> 'resolution_key_hash'
            IS DISTINCT FROM NEW.resolution_key_hash
       OR successor_event.event_fingerprint IS DISTINCT FROM
            b3s_history.evidence_vault_sv9_judgment_authority_event_fingerprint(successor_event) THEN
        RAISE EXCEPTION 'SV9 judgment review resolution successor authority event is invalid';
    END IF;
    IF successor_event.sequence <= expected_event.sequence
       OR successor_event.sequence <> expected_event.sequence + 1
       OR latest_event.id IS DISTINCT FROM successor_event.id
       OR latest_event.event_fingerprint IS DISTINCT FROM successor_event.event_fingerprint
       OR latest_event.sequence IS DISTINCT FROM successor_event.sequence THEN
        RAISE EXCEPTION 'SV9 judgment review resolution successor authority CAS is stale or gapped';
    END IF;

    RETURN NEW;
END;
$$;

CREATE FUNCTION b3s_history.reject_vault_sv9_judgment_review_resolution_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'SV9 judgment review resolution ledger is append-only';
END;
$$;

CREATE FUNCTION b3s_history.reject_vault_sv9_judgment_review_resolution_candidate_authority_conflict()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, b3s_history
AS $$
BEGIN
    IF NEW.event_type NOT IN ('adopt', 'supersede')
       OR (NEW.event_type = 'supersede' AND NOT (NEW.event_payload ? 'sv9_review_resolution')) THEN
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(NEW.workspace_id::text || ':' || NEW.brand_id::text, 0)
    );
    IF EXISTS (
           SELECT 1
           FROM b3s_history.evidence_vault_sv9_judgment_review_resolutions AS resolutions
           WHERE resolutions.workspace_id = NEW.workspace_id
             AND resolutions.brand_id = NEW.brand_id
             AND resolutions.candidate_id = NEW.candidate_id
             AND resolutions.decision = 'reject'
       ) THEN
        RAISE EXCEPTION 'SV9 judgment authority cannot adopt a rejected review candidate';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION b3s_history.validate_vault_sv9_judgment_review_resolution_authority_pair()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, b3s_history
AS $$
BEGIN
    IF NEW.event_type <> 'supersede'
       OR NOT (NEW.event_payload ? 'sv9_review_resolution') THEN
        RETURN NULL;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(NEW.workspace_id::text || ':' || NEW.brand_id::text, 0)
    );
    IF jsonb_typeof(NEW.event_payload -> 'sv9_review_resolution') IS DISTINCT FROM 'object'
       OR b3s_history.evidence_vault_json_has_exact_keys(
              NEW.event_payload -> 'sv9_review_resolution',
              ARRAY['resolution_id', 'resolution_key_hash']
          ) IS NOT TRUE
       OR NOT EXISTS (
           SELECT 1
           FROM b3s_history.evidence_vault_sv9_judgment_review_resolutions AS resolutions
           WHERE resolutions.workspace_id = NEW.workspace_id
             AND resolutions.brand_id = NEW.brand_id
             AND resolutions.id::text = NEW.event_payload -> 'sv9_review_resolution' ->> 'resolution_id'
             AND resolutions.resolution_key_hash = NEW.event_payload -> 'sv9_review_resolution' ->> 'resolution_key_hash'
             AND resolutions.decision = 'approve'
             AND resolutions.successor_authority_event_id = NEW.id
       ) THEN
        RAISE EXCEPTION 'SV9 judgment authority approve event requires its linked resolution';
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER validate_vault_sv9_judgment_review_resolution_insert
    BEFORE INSERT ON b3s_history.evidence_vault_sv9_judgment_review_resolutions
    FOR EACH ROW EXECUTE FUNCTION b3s_history.validate_vault_sv9_judgment_review_resolution_insert();
CREATE TRIGGER reject_vault_sv9_judgment_review_resolution_mutation
    BEFORE UPDATE OR DELETE OR TRUNCATE ON b3s_history.evidence_vault_sv9_judgment_review_resolutions
    FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_vault_sv9_judgment_review_resolution_mutation();
CREATE TRIGGER reject_vault_sv9_judgment_review_resolution_candidate_authority_conflict
    BEFORE INSERT ON b3s_history.evidence_vault_sv9_judgment_authority_events
    FOR EACH ROW EXECUTE FUNCTION b3s_history.reject_vault_sv9_judgment_review_resolution_candidate_authority_conflict();
CREATE CONSTRAINT TRIGGER validate_vault_sv9_judgment_review_resolution_authority_pair
    AFTER INSERT ON b3s_history.evidence_vault_sv9_judgment_authority_events
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION b3s_history.validate_vault_sv9_judgment_review_resolution_authority_pair();

DO $$
DECLARE owner_role record;
BEGIN
    SELECT * INTO owner_role
    FROM pg_catalog.pg_roles
    WHERE rolname = 'b3s_history_vault_provenance_owner';
    IF owner_role.oid IS NULL OR owner_role.rolcanlogin OR owner_role.rolsuper
       OR owner_role.rolcreaterole OR owner_role.rolcreatedb OR owner_role.rolreplication
       OR owner_role.rolbypassrls THEN
        RAISE EXCEPTION 'SV9 judgment review resolution owner is unsafe';
    END IF;
    GRANT USAGE, CREATE ON SCHEMA b3s_history TO b3s_history_vault_provenance_owner;
    ALTER TABLE b3s_history.evidence_vault_sv9_judgment_review_resolutions
        OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.evidence_vault_sv9_judgment_review_fingerprint(text, jsonb)
        OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.evidence_vault_sv9_judgment_candidate_complete_record_fingerprint(
        b3s_history.evidence_vault_sv9_judgment_candidates
    ) OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.evidence_vault_sv9_judgment_authority_event_fingerprint(
        b3s_history.evidence_vault_sv9_judgment_authority_events
    ) OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.validate_vault_sv9_judgment_review_resolution_insert()
        OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.reject_vault_sv9_judgment_review_resolution_mutation()
        OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.reject_vault_sv9_judgment_review_resolution_candidate_authority_conflict()
        OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.validate_vault_sv9_judgment_review_resolution_authority_pair()
        OWNER TO b3s_history_vault_provenance_owner;
    REVOKE CREATE ON SCHEMA b3s_history FROM b3s_history_vault_provenance_owner;
END;
$$;

REVOKE ALL ON b3s_history.evidence_vault_sv9_judgment_review_resolutions FROM PUBLIC;
REVOKE ALL ON FUNCTION b3s_history.evidence_vault_sv9_judgment_review_fingerprint(text, jsonb),
                       b3s_history.evidence_vault_sv9_judgment_candidate_complete_record_fingerprint(
                           b3s_history.evidence_vault_sv9_judgment_candidates
                       ),
                       b3s_history.evidence_vault_sv9_judgment_authority_event_fingerprint(
                           b3s_history.evidence_vault_sv9_judgment_authority_events
                       ),
                       b3s_history.validate_vault_sv9_judgment_review_resolution_insert(),
                       b3s_history.reject_vault_sv9_judgment_review_resolution_mutation(),
                       b3s_history.reject_vault_sv9_judgment_review_resolution_candidate_authority_conflict(),
                       b3s_history.validate_vault_sv9_judgment_review_resolution_authority_pair() FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'b3s_pr71_app_runtime') THEN
        REVOKE ALL ON SCHEMA b3s_history FROM b3s_pr71_app_runtime;
        GRANT USAGE ON SCHEMA b3s_history TO b3s_pr71_app_runtime;
        REVOKE ALL ON b3s_history.evidence_vault_sv9_judgment_review_resolutions FROM b3s_pr71_app_runtime;
        GRANT SELECT, INSERT
            ON b3s_history.evidence_vault_sv9_judgment_review_resolutions TO b3s_pr71_app_runtime;
        IF pg_catalog.has_table_privilege(
            'b3s_pr71_app_runtime',
            'b3s_history.evidence_vault_sv9_judgment_review_resolutions',
            'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
        ) THEN
            RAISE EXCEPTION 'SV9 judgment review resolution runtime grant exceeds append-only contract';
        END IF;
    END IF;
END;
$$;
