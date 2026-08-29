CREATE TABLE b3s_history.evidence_vault_sv9_judgment_authority_events (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    brand_id uuid NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('adopt', 'reopen', 'supersede')),
    sequence bigint NOT NULL CHECK (sequence > 0),
    predecessor_event_id uuid,
    active_parent_event_id uuid,
    candidate_id uuid,
    candidate_scan_run_id uuid,
    candidate_capture_id uuid,
    candidate_operation_plan_id uuid,
    request_fingerprint text NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    event_fingerprint text NOT NULL CHECK (event_fingerprint ~ '^[0-9a-f]{64}$'),
    evaluation_bundle_fingerprint text CHECK (evaluation_bundle_fingerprint IS NULL OR evaluation_bundle_fingerprint ~ '^[0-9a-f]{64}$'),
    canonical_plan_fingerprint text CHECK (canonical_plan_fingerprint IS NULL OR canonical_plan_fingerprint ~ '^[0-9a-f]{64}$'),
    current_series_fingerprint text CHECK (current_series_fingerprint IS NULL OR current_series_fingerprint ~ '^[0-9a-f]{64}$'),
    candidate_series_fingerprint text CHECK (candidate_series_fingerprint IS NULL OR candidate_series_fingerprint ~ '^[0-9a-f]{64}$'),
    assessment_fingerprint text CHECK (assessment_fingerprint IS NULL OR assessment_fingerprint ~ '^[0-9a-f]{64}$'),
    score_fingerprint text CHECK (score_fingerprint IS NULL OR score_fingerprint ~ '^[0-9a-f]{64}$'),
    delta_fingerprint text CHECK (delta_fingerprint IS NULL OR delta_fingerprint ~ '^[0-9a-f]{64}$'),
    idempotency_key_hash text NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    event_payload jsonb NOT NULL CHECK (jsonb_typeof(event_payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (workspace_id, brand_id, id),
    UNIQUE (workspace_id, brand_id, sequence),
    UNIQUE (workspace_id, brand_id, idempotency_key_hash),
    FOREIGN KEY (workspace_id, brand_id)
        REFERENCES b3s_history.brands (workspace_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, brand_id, predecessor_event_id)
        REFERENCES b3s_history.evidence_vault_sv9_judgment_authority_events (workspace_id, brand_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, brand_id, active_parent_event_id)
        REFERENCES b3s_history.evidence_vault_sv9_judgment_authority_events (workspace_id, brand_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (candidate_id, workspace_id, brand_id, candidate_scan_run_id, candidate_capture_id, candidate_operation_plan_id)
        REFERENCES b3s_history.evidence_vault_sv9_judgment_candidates (id, workspace_id, brand_id, scan_run_id, capture_id, operation_plan_id) ON DELETE RESTRICT,
    CHECK (
        (sequence = 1 AND event_type = 'adopt' AND predecessor_event_id IS NULL AND active_parent_event_id IS NULL)
        OR (sequence > 1 AND event_type IN ('reopen', 'supersede') AND predecessor_event_id IS NOT NULL AND active_parent_event_id IS NOT NULL)
    ),
    CHECK (
        (event_type IN ('adopt', 'supersede')
            AND candidate_id IS NOT NULL AND candidate_scan_run_id IS NOT NULL
            AND candidate_capture_id IS NOT NULL AND candidate_operation_plan_id IS NOT NULL
            AND evaluation_bundle_fingerprint IS NOT NULL AND canonical_plan_fingerprint IS NOT NULL
            AND current_series_fingerprint IS NOT NULL AND candidate_series_fingerprint IS NOT NULL
            AND assessment_fingerprint IS NOT NULL AND score_fingerprint IS NOT NULL
            AND delta_fingerprint IS NULL)
        OR (event_type = 'reopen'
            AND candidate_id IS NULL AND candidate_scan_run_id IS NULL
            AND candidate_capture_id IS NULL AND candidate_operation_plan_id IS NULL
            AND evaluation_bundle_fingerprint IS NULL AND canonical_plan_fingerprint IS NULL
            AND candidate_series_fingerprint IS NULL AND assessment_fingerprint IS NULL
            AND score_fingerprint IS NULL AND current_series_fingerprint IS NOT NULL
            AND delta_fingerprint IS NOT NULL AND event_payload ? 'signed_delta'
            AND event_payload ? 'review_state'
            AND jsonb_typeof(event_payload -> 'signed_delta') = 'object')
    )
);

CREATE UNIQUE INDEX evidence_vault_sv9_judgment_authority_candidate_adoption_key
    ON b3s_history.evidence_vault_sv9_judgment_authority_events (workspace_id, brand_id, candidate_id)
    WHERE event_type IN ('adopt', 'supersede');

CREATE FUNCTION b3s_history.validate_vault_sv9_judgment_authority_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE tail record; parent record; candidate record; expected_parent uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.workspace_id::text || ':' || NEW.brand_id::text, 0));
    SELECT * INTO tail FROM b3s_history.evidence_vault_sv9_judgment_authority_events
    WHERE workspace_id = NEW.workspace_id AND brand_id = NEW.brand_id
    ORDER BY sequence DESC LIMIT 1;
    IF tail.id IS NULL THEN
        IF NEW.sequence <> 1 OR NEW.event_type <> 'adopt' THEN
            RAISE EXCEPTION 'SV9 judgment authority first event must adopt';
        END IF;
    ELSE
        IF NEW.sequence <> tail.sequence + 1 OR NEW.predecessor_event_id IS DISTINCT FROM tail.id THEN
            RAISE EXCEPTION 'SV9 judgment authority sequence is stale, forked, or gapped';
        END IF;
        IF NEW.event_type = 'adopt' THEN
            RAISE EXCEPTION 'SV9 judgment authority adopt can only start a chain';
        END IF;
        expected_parent := CASE WHEN tail.event_type = 'reopen' THEN tail.active_parent_event_id ELSE tail.id END;
        IF NEW.active_parent_event_id IS DISTINCT FROM expected_parent THEN
            RAISE EXCEPTION 'SV9 judgment authority active parent is stale or invalid';
        END IF;
        SELECT * INTO parent FROM b3s_history.evidence_vault_sv9_judgment_authority_events
        WHERE workspace_id = NEW.workspace_id AND brand_id = NEW.brand_id AND id = NEW.active_parent_event_id;
        IF parent.id IS NULL OR parent.event_type NOT IN ('adopt', 'supersede') OR parent.candidate_id IS NULL THEN
            RAISE EXCEPTION 'SV9 judgment authority active parent is invalid';
        END IF;
        IF NEW.event_type = 'reopen' AND NEW.current_series_fingerprint IS DISTINCT FROM parent.candidate_series_fingerprint THEN
            RAISE EXCEPTION 'SV9 judgment authority reopen must preserve the accepted series';
        END IF;
        IF NEW.event_type = 'supersede' AND NEW.current_series_fingerprint IS DISTINCT FROM parent.candidate_series_fingerprint THEN
            RAISE EXCEPTION 'SV9 judgment authority supersede is based on a stale series';
        END IF;
        IF NEW.event_type = 'supersede' AND NEW.candidate_id IS NOT DISTINCT FROM parent.candidate_id THEN
            RAISE EXCEPTION 'SV9 judgment authority supersede requires a new candidate';
        END IF;
    END IF;
    IF NEW.event_type IN ('adopt', 'supersede') THEN
        SELECT * INTO candidate FROM b3s_history.evidence_vault_sv9_judgment_candidates
        WHERE id = NEW.candidate_id AND workspace_id = NEW.workspace_id AND brand_id = NEW.brand_id
          AND scan_run_id = NEW.candidate_scan_run_id AND capture_id = NEW.candidate_capture_id
          AND operation_plan_id = NEW.candidate_operation_plan_id;
        IF candidate.id IS NULL
           OR NEW.evaluation_bundle_fingerprint IS DISTINCT FROM candidate.evaluation_bundle_fingerprint
           OR NEW.canonical_plan_fingerprint IS DISTINCT FROM candidate.canonical_plan_fingerprint
           OR NEW.current_series_fingerprint IS DISTINCT FROM candidate.current_series_fingerprint
           OR NEW.candidate_series_fingerprint IS DISTINCT FROM candidate.candidate_series_fingerprint
           OR NEW.assessment_fingerprint IS DISTINCT FROM candidate.assessment_fingerprint
           OR NEW.score_fingerprint IS DISTINCT FROM candidate.score_fingerprint THEN
            RAISE EXCEPTION 'SV9 judgment authority candidate provenance is invalid';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER validate_vault_sv9_judgment_authority_insert
    BEFORE INSERT ON b3s_history.evidence_vault_sv9_judgment_authority_events
    FOR EACH ROW EXECUTE FUNCTION b3s_history.validate_vault_sv9_judgment_authority_insert();
CREATE TRIGGER reject_vault_sv9_judgment_authority_mutation
    BEFORE UPDATE OR DELETE OR TRUNCATE ON b3s_history.evidence_vault_sv9_judgment_authority_events
    FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_vault_sv9_judgment_candidate_mutation();

CREATE VIEW b3s_history.evidence_vault_sv9_judgment_active_series_v1 AS
WITH latest AS (
    SELECT DISTINCT ON (workspace_id, brand_id) *
    FROM b3s_history.evidence_vault_sv9_judgment_authority_events
    ORDER BY workspace_id, brand_id, sequence DESC
)
SELECT latest.workspace_id, latest.brand_id, authority.id AS authority_event_id,
       authority.sequence AS authority_sequence, authority.candidate_id,
       authority.candidate_scan_run_id, authority.candidate_capture_id,
       authority.candidate_operation_plan_id, authority.request_fingerprint,
       authority.event_fingerprint, authority.evaluation_bundle_fingerprint,
       authority.canonical_plan_fingerprint, authority.current_series_fingerprint,
       authority.candidate_series_fingerprint, authority.assessment_fingerprint,
       authority.score_fingerprint, latest.id AS latest_event_id,
       latest.sequence AS latest_sequence, latest.event_type AS latest_event_type,
       CASE WHEN latest.event_type = 'reopen' THEN latest.delta_fingerprint END AS reopen_delta_fingerprint,
       CASE WHEN latest.event_type = 'reopen' THEN latest.event_payload END AS reopen_review_overlay
FROM latest
JOIN b3s_history.evidence_vault_sv9_judgment_authority_events AS authority
  ON authority.workspace_id = latest.workspace_id AND authority.brand_id = latest.brand_id
 AND authority.id = CASE WHEN latest.event_type = 'reopen' THEN latest.active_parent_event_id ELSE latest.id END;

CREATE VIEW b3s_history.evidence_vault_sv9_judgment_active_partition_v1 AS
SELECT series.*, candidates.candidate_payload AS accepted_candidate_payload
FROM b3s_history.evidence_vault_sv9_judgment_active_series_v1 AS series
JOIN b3s_history.evidence_vault_sv9_judgment_candidates AS candidates
  ON candidates.id = series.candidate_id AND candidates.workspace_id = series.workspace_id
 AND candidates.brand_id = series.brand_id AND candidates.scan_run_id = series.candidate_scan_run_id
 AND candidates.capture_id = series.candidate_capture_id
 AND candidates.operation_plan_id = series.candidate_operation_plan_id;

DO $$
DECLARE owner_role record;
BEGIN
    SELECT * INTO owner_role FROM pg_catalog.pg_roles WHERE rolname = 'b3s_history_vault_provenance_owner';
    IF owner_role.oid IS NULL OR owner_role.rolcanlogin OR owner_role.rolsuper
       OR owner_role.rolcreaterole OR owner_role.rolcreatedb OR owner_role.rolreplication OR owner_role.rolbypassrls THEN
        RAISE EXCEPTION 'SV9 judgment authority owner is unsafe';
    END IF;
    GRANT USAGE, CREATE ON SCHEMA b3s_history TO b3s_history_vault_provenance_owner;
    ALTER TABLE b3s_history.evidence_vault_sv9_judgment_authority_events OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.validate_vault_sv9_judgment_authority_insert() OWNER TO b3s_history_vault_provenance_owner;
    ALTER VIEW b3s_history.evidence_vault_sv9_judgment_active_series_v1 OWNER TO b3s_history_vault_provenance_owner;
    ALTER VIEW b3s_history.evidence_vault_sv9_judgment_active_partition_v1 OWNER TO b3s_history_vault_provenance_owner;
    REVOKE CREATE ON SCHEMA b3s_history FROM b3s_history_vault_provenance_owner;
END;
$$;
REVOKE ALL ON b3s_history.evidence_vault_sv9_judgment_authority_events,
              b3s_history.evidence_vault_sv9_judgment_active_series_v1,
              b3s_history.evidence_vault_sv9_judgment_active_partition_v1 FROM PUBLIC;
REVOKE ALL ON FUNCTION b3s_history.validate_vault_sv9_judgment_authority_insert() FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'b3s_pr71_app_runtime') THEN
        REVOKE ALL ON b3s_history.evidence_vault_sv9_judgment_authority_events,
                      b3s_history.evidence_vault_sv9_judgment_active_series_v1,
                      b3s_history.evidence_vault_sv9_judgment_active_partition_v1 FROM b3s_pr71_app_runtime;
        GRANT SELECT, INSERT ON b3s_history.evidence_vault_sv9_judgment_authority_events TO b3s_pr71_app_runtime;
        GRANT SELECT ON b3s_history.evidence_vault_sv9_judgment_active_series_v1,
                        b3s_history.evidence_vault_sv9_judgment_active_partition_v1 TO b3s_pr71_app_runtime;
        IF pg_catalog.has_table_privilege('b3s_pr71_app_runtime', 'b3s_history.evidence_vault_sv9_judgment_authority_events', 'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_table_privilege('b3s_pr71_app_runtime', 'b3s_history.evidence_vault_sv9_judgment_active_series_v1', 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_table_privilege('b3s_pr71_app_runtime', 'b3s_history.evidence_vault_sv9_judgment_active_partition_v1', 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'SV9 judgment authority runtime grant exceeds contract';
        END IF;
    END IF;
END;
$$;
