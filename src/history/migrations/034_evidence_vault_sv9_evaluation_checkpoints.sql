CREATE TABLE b3s_history.evidence_vault_sv9_evaluation_checkpoints (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    brand_id uuid NOT NULL,
    scan_run_id uuid NOT NULL,
    source_scan_id text NOT NULL CHECK (length(source_scan_id) BETWEEN 1 AND 300),
    capture_id uuid NOT NULL,
    operation_plan_id uuid NOT NULL,
    schema_version text NOT NULL CHECK (schema_version = 'evidence-vault-sv9-evaluation-checkpoint-v1'),
    evaluation_input_fingerprint text NOT NULL CHECK (evaluation_input_fingerprint ~ '^[0-9a-f]{64}$'),
    relation_projection_fingerprint text NOT NULL CHECK (relation_projection_fingerprint ~ '^[0-9a-f]{64}$'),
    prior_authority_snapshot_fingerprint text NOT NULL CHECK (prior_authority_snapshot_fingerprint ~ '^[0-9a-f]{64}$'),
    canonical_plan_fingerprint text NOT NULL CHECK (canonical_plan_fingerprint ~ '^[0-9a-f]{64}$'),
    current_series_fingerprint text NOT NULL CHECK (current_series_fingerprint ~ '^[0-9a-f]{64}$'),
    candidate_series_fingerprint text NOT NULL CHECK (candidate_series_fingerprint ~ '^[0-9a-f]{64}$'),
    checkpoint_fingerprint text NOT NULL CHECK (checkpoint_fingerprint ~ '^[0-9a-f]{64}$'),
    evaluation_state text NOT NULL CHECK (evaluation_state IN ('partial', 'provider_failure')),
    authority boolean NOT NULL CHECK (authority IS FALSE),
    runtime_effect text NOT NULL CHECK (runtime_effect = 'checkpoint_only'),
    score_state text NOT NULL CHECK (score_state = 'unavailable'),
    checkpoint_payload jsonb NOT NULL CHECK ((
        jsonb_typeof(checkpoint_payload) = 'object'
        AND checkpoint_payload ?& ARRAY['schema_version', 'authority', 'runtime_effect', 'evaluation_state', 'score_state', 'reason_codes', 'evaluation_input', 'prior_authority_snapshot', 'plan_binding', 'healthy_workset', 'review_partition', 'pending_evidence', 'non_authoritative_hints', 'checkpoint_fingerprint']
        AND checkpoint_payload - ARRAY['schema_version', 'authority', 'runtime_effect', 'evaluation_state', 'score_state', 'reason_codes', 'evaluation_input', 'prior_authority_snapshot', 'plan_binding', 'healthy_workset', 'review_partition', 'pending_evidence', 'non_authoritative_hints', 'checkpoint_fingerprint'] = '{}'::jsonb
        AND jsonb_typeof(checkpoint_payload -> 'schema_version') = 'string'
        AND jsonb_typeof(checkpoint_payload -> 'authority') = 'boolean'
        AND jsonb_typeof(checkpoint_payload -> 'runtime_effect') = 'string'
        AND jsonb_typeof(checkpoint_payload -> 'evaluation_state') = 'string'
        AND jsonb_typeof(checkpoint_payload -> 'score_state') = 'string'
        AND jsonb_typeof(checkpoint_payload -> 'reason_codes') = 'array'
        AND jsonb_typeof(checkpoint_payload -> 'evaluation_input') = 'object'
        AND jsonb_typeof(checkpoint_payload -> 'prior_authority_snapshot') = 'object'
        AND jsonb_typeof(checkpoint_payload -> 'plan_binding') = 'object'
        AND jsonb_typeof(checkpoint_payload -> 'healthy_workset') = 'object'
        AND jsonb_typeof(checkpoint_payload -> 'review_partition') = 'object'
        AND jsonb_typeof(checkpoint_payload -> 'pending_evidence') = 'array'
        AND jsonb_typeof(checkpoint_payload -> 'non_authoritative_hints') = 'array'
        AND jsonb_typeof(checkpoint_payload -> 'checkpoint_fingerprint') = 'string'
        AND checkpoint_payload ->> 'schema_version' = schema_version
        AND checkpoint_payload -> 'authority' = 'false'::jsonb
        AND checkpoint_payload ->> 'runtime_effect' = runtime_effect
        AND checkpoint_payload ->> 'evaluation_state' = evaluation_state
        AND checkpoint_payload ->> 'score_state' = score_state
        AND checkpoint_payload #>> '{evaluation_input,evaluation_input_fingerprint}' = evaluation_input_fingerprint
        AND checkpoint_payload #>> '{evaluation_input,relation_projection_fingerprint}' = relation_projection_fingerprint
        AND checkpoint_payload #>> '{prior_authority_snapshot,snapshot_fingerprint}' = prior_authority_snapshot_fingerprint
        AND checkpoint_payload #>> '{plan_binding,canonical_plan_fingerprint}' = canonical_plan_fingerprint
        AND checkpoint_payload #>> '{plan_binding,current_series_fingerprint}' = current_series_fingerprint
        AND checkpoint_payload #>> '{plan_binding,candidate_series_fingerprint}' = candidate_series_fingerprint
        AND checkpoint_payload ->> 'checkpoint_fingerprint' = checkpoint_fingerprint
        AND NOT jsonb_path_exists(checkpoint_payload, '$.** ? (exists(@.assessment) || exists(@.score) || exists(@.adoptable) || exists(@.publishable) || exists(@.candidate) || exists(@.candidate_payload) || exists(@.complete_candidate) || exists(@.complete_candidate_fingerprint) || exists(@.complete_record_fingerprint) || exists(@.candidate_tile_judgments) || exists(@.candidate_component_sentinels) || exists(@.evaluation_bundle_fingerprint) || exists(@.assessment_fingerprint) || exists(@.score_fingerprint))')
    ) IS TRUE),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (workspace_id, brand_id, capture_id, operation_plan_id, checkpoint_fingerprint),
    UNIQUE (id, workspace_id, brand_id, scan_run_id, capture_id, operation_plan_id),
    FOREIGN KEY (workspace_id, brand_id) REFERENCES b3s_history.brands (workspace_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, brand_id, scan_run_id, operation_plan_id) REFERENCES b3s_history.evidence_vault_operation_plans (workspace_id, brand_id, scan_run_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (brand_id, scan_run_id, capture_id) REFERENCES b3s_history.captures (brand_id, scan_run_id, id) ON DELETE RESTRICT
);
CREATE TABLE b3s_history.evidence_vault_sv9_evaluation_checkpoint_evidence_bindings (
    checkpoint_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    brand_id uuid NOT NULL,
    scan_run_id uuid NOT NULL,
    capture_id uuid NOT NULL,
    operation_plan_id uuid NOT NULL,
    evidence_record_id uuid NOT NULL,
    evidence_ref text NOT NULL CHECK (length(evidence_ref) BETWEEN 1 AND 1000),
    evidence_fingerprint text NOT NULL CHECK (evidence_fingerprint ~ '^[0-9a-f]{64}$'),
    evidence_id text CHECK (evidence_id IS NULL OR evidence_id ~ '^[0-9a-f]{64}$'),
    source_identity_id text CHECK (source_identity_id IS NULL OR source_identity_id ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (checkpoint_id, evidence_record_id),
    CHECK ((evidence_id IS NULL) = (source_identity_id IS NULL)),
    FOREIGN KEY (checkpoint_id, workspace_id, brand_id, scan_run_id, capture_id, operation_plan_id)
        REFERENCES b3s_history.evidence_vault_sv9_evaluation_checkpoints
        (id, workspace_id, brand_id, scan_run_id, capture_id, operation_plan_id) ON DELETE RESTRICT,
    FOREIGN KEY (capture_id, evidence_record_id, evidence_fingerprint)
        REFERENCES b3s_history.evidence_records (capture_id, id, content_hash) ON DELETE RESTRICT
);
CREATE INDEX evidence_vault_sv9_evaluation_checkpoint_binding_evidence_idx
    ON b3s_history.evidence_vault_sv9_evaluation_checkpoint_evidence_bindings (capture_id, evidence_record_id, evidence_fingerprint);
CREATE FUNCTION b3s_history.validate_vault_sv9_evaluation_checkpoint_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM b3s_history.scan_runs AS scans
        WHERE scans.id = NEW.scan_run_id AND scans.workspace_id = NEW.workspace_id
          AND scans.brand_id = NEW.brand_id AND scans.source_scan_id = NEW.source_scan_id
    ) THEN RAISE EXCEPTION 'SV9 evaluation checkpoint source provenance is invalid'; END IF;
    RETURN NEW;
END;
$$;
CREATE FUNCTION b3s_history.reject_vault_sv9_evaluation_checkpoint_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'SV9 evaluation checkpoint ledger is append-only'; END;
$$;
CREATE TRIGGER validate_vault_sv9_evaluation_checkpoint_insert
    BEFORE INSERT ON b3s_history.evidence_vault_sv9_evaluation_checkpoints
    FOR EACH ROW EXECUTE FUNCTION b3s_history.validate_vault_sv9_evaluation_checkpoint_insert();
CREATE TRIGGER reject_vault_sv9_evaluation_checkpoint_mutation
    BEFORE UPDATE OR DELETE OR TRUNCATE ON b3s_history.evidence_vault_sv9_evaluation_checkpoints
    FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_vault_sv9_evaluation_checkpoint_mutation();
CREATE TRIGGER reject_vault_sv9_evaluation_checkpoint_binding_mutation
    BEFORE UPDATE OR DELETE OR TRUNCATE ON b3s_history.evidence_vault_sv9_evaluation_checkpoint_evidence_bindings
    FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_vault_sv9_evaluation_checkpoint_mutation();
DO $$
DECLARE owner_role record;
BEGIN
    SELECT * INTO owner_role FROM pg_catalog.pg_roles WHERE rolname = 'b3s_history_vault_provenance_owner';
    IF owner_role.oid IS NULL OR owner_role.rolcanlogin OR owner_role.rolsuper
       OR owner_role.rolcreaterole OR owner_role.rolcreatedb OR owner_role.rolreplication OR owner_role.rolbypassrls THEN
        RAISE EXCEPTION 'SV9 evaluation checkpoint owner is unsafe';
    END IF;
    GRANT USAGE, CREATE ON SCHEMA b3s_history TO b3s_history_vault_provenance_owner;
    ALTER TABLE b3s_history.evidence_vault_sv9_evaluation_checkpoints OWNER TO b3s_history_vault_provenance_owner;
    ALTER TABLE b3s_history.evidence_vault_sv9_evaluation_checkpoint_evidence_bindings OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.validate_vault_sv9_evaluation_checkpoint_insert() OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.reject_vault_sv9_evaluation_checkpoint_mutation() OWNER TO b3s_history_vault_provenance_owner;
    REVOKE CREATE ON SCHEMA b3s_history FROM b3s_history_vault_provenance_owner;
END;
$$;
REVOKE ALL ON b3s_history.evidence_vault_sv9_evaluation_checkpoints,
              b3s_history.evidence_vault_sv9_evaluation_checkpoint_evidence_bindings FROM PUBLIC;
REVOKE ALL ON FUNCTION b3s_history.validate_vault_sv9_evaluation_checkpoint_insert(),
                       b3s_history.reject_vault_sv9_evaluation_checkpoint_mutation() FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'b3s_pr71_app_runtime') THEN
        REVOKE ALL ON SCHEMA b3s_history FROM b3s_pr71_app_runtime;
        GRANT USAGE ON SCHEMA b3s_history TO b3s_pr71_app_runtime;
        REVOKE ALL ON b3s_history.evidence_vault_sv9_evaluation_checkpoints,
                      b3s_history.evidence_vault_sv9_evaluation_checkpoint_evidence_bindings FROM b3s_pr71_app_runtime;
        GRANT SELECT, INSERT ON b3s_history.evidence_vault_sv9_evaluation_checkpoints,
                               b3s_history.evidence_vault_sv9_evaluation_checkpoint_evidence_bindings TO b3s_pr71_app_runtime;
        IF pg_catalog.has_table_privilege('b3s_pr71_app_runtime', 'b3s_history.evidence_vault_sv9_evaluation_checkpoints', 'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_table_privilege('b3s_pr71_app_runtime', 'b3s_history.evidence_vault_sv9_evaluation_checkpoint_evidence_bindings', 'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'SV9 evaluation checkpoint runtime grant exceeds append-only contract';
        END IF;
    END IF;
END;
$$;
