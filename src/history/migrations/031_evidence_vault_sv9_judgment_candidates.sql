ALTER TABLE b3s_history.evidence_records
    ADD CONSTRAINT evidence_records_capture_id_id_content_hash_key
    UNIQUE (capture_id, id, content_hash);
ALTER TABLE b3s_history.captures ADD CONSTRAINT captures_brand_scan_run_id_key
    UNIQUE (brand_id, scan_run_id, id);
CREATE TABLE b3s_history.evidence_vault_sv9_judgment_candidates (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    brand_id uuid NOT NULL,
    scan_run_id uuid NOT NULL,
    source_scan_id text NOT NULL CHECK (length(source_scan_id) BETWEEN 1 AND 300),
    capture_id uuid NOT NULL,
    operation_plan_id uuid NOT NULL,
    schema_version text NOT NULL CHECK (schema_version = 'evidence-vault-sv9-judgment-candidate-v1'),
    canonical_plan_fingerprint text NOT NULL CHECK (canonical_plan_fingerprint ~ '^[0-9a-f]{64}$'),
    current_series_fingerprint text NOT NULL CHECK (current_series_fingerprint ~ '^[0-9a-f]{64}$'),
    candidate_series_fingerprint text NOT NULL CHECK (candidate_series_fingerprint ~ '^[0-9a-f]{64}$'),
    evaluation_bundle_fingerprint text NOT NULL CHECK (evaluation_bundle_fingerprint ~ '^[0-9a-f]{64}$'),
    assessment_fingerprint text NOT NULL CHECK (assessment_fingerprint ~ '^[0-9a-f]{64}$'),
    score_fingerprint text NOT NULL CHECK (score_fingerprint ~ '^[0-9a-f]{64}$'),
    complete_record_fingerprint text NOT NULL CHECK (complete_record_fingerprint ~ '^[0-9a-f]{64}$'),
    candidate_payload jsonb NOT NULL CHECK (jsonb_typeof(candidate_payload) = 'object'),
    authority text NOT NULL CHECK (authority = 'pending'),
    review_state text NOT NULL CHECK (review_state = 'none'),
    lifecycle_state text NOT NULL CHECK (lifecycle_state = 'active'),
    runtime_effect text NOT NULL CHECK (runtime_effect = 'shadow_only'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (workspace_id, brand_id, capture_id, operation_plan_id, canonical_plan_fingerprint),
    UNIQUE (id, workspace_id, brand_id, scan_run_id, capture_id, operation_plan_id),
    FOREIGN KEY (workspace_id, brand_id) REFERENCES b3s_history.brands (workspace_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, brand_id, scan_run_id, operation_plan_id) REFERENCES b3s_history.evidence_vault_operation_plans (workspace_id, brand_id, scan_run_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (brand_id, scan_run_id, capture_id) REFERENCES b3s_history.captures (brand_id, scan_run_id, id) ON DELETE RESTRICT
);
CREATE TABLE b3s_history.evidence_vault_sv9_judgment_evidence_bindings (
    candidate_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    brand_id uuid NOT NULL,
    scan_run_id uuid NOT NULL,
    capture_id uuid NOT NULL,
    operation_plan_id uuid NOT NULL,
    evidence_record_id uuid NOT NULL,
    evidence_fingerprint text NOT NULL CHECK (evidence_fingerprint ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (candidate_id, evidence_record_id),
    FOREIGN KEY (candidate_id, workspace_id, brand_id, scan_run_id, capture_id, operation_plan_id) REFERENCES b3s_history.evidence_vault_sv9_judgment_candidates
        (id, workspace_id, brand_id, scan_run_id, capture_id, operation_plan_id) ON DELETE RESTRICT,
    FOREIGN KEY (capture_id, evidence_record_id, evidence_fingerprint)
        REFERENCES b3s_history.evidence_records (capture_id, id, content_hash) ON DELETE RESTRICT
);
CREATE FUNCTION b3s_history.validate_vault_sv9_judgment_candidate_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM b3s_history.scan_runs AS scans
        WHERE scans.id = NEW.scan_run_id
          AND scans.workspace_id = NEW.workspace_id
          AND scans.brand_id = NEW.brand_id
          AND scans.source_scan_id = NEW.source_scan_id
    ) THEN
        RAISE EXCEPTION 'SV9 judgment candidate source provenance is invalid';
    END IF;
    RETURN NEW;
END;
$$;
CREATE FUNCTION b3s_history.reject_vault_sv9_judgment_candidate_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'SV9 judgment candidate authority is append-only';
END;
$$;
CREATE TRIGGER validate_vault_sv9_judgment_candidate_insert
    BEFORE INSERT ON b3s_history.evidence_vault_sv9_judgment_candidates
    FOR EACH ROW EXECUTE FUNCTION b3s_history.validate_vault_sv9_judgment_candidate_insert();
CREATE TRIGGER reject_vault_sv9_judgment_candidate_mutation
    BEFORE UPDATE OR DELETE OR TRUNCATE ON b3s_history.evidence_vault_sv9_judgment_candidates
    FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_vault_sv9_judgment_candidate_mutation();
CREATE TRIGGER reject_vault_sv9_judgment_evidence_binding_mutation
    BEFORE UPDATE OR DELETE OR TRUNCATE ON b3s_history.evidence_vault_sv9_judgment_evidence_bindings
    FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_vault_sv9_judgment_candidate_mutation();
DO $$
DECLARE owner_role record;
BEGIN
    SELECT * INTO owner_role FROM pg_catalog.pg_roles
    WHERE rolname = 'b3s_history_vault_provenance_owner';
    IF owner_role.oid IS NULL OR owner_role.rolcanlogin OR owner_role.rolsuper
       OR owner_role.rolcreaterole OR owner_role.rolcreatedb OR owner_role.rolreplication
       OR owner_role.rolbypassrls THEN
        RAISE EXCEPTION 'SV9 judgment candidate owner is unsafe';
    END IF;
    GRANT USAGE, CREATE ON SCHEMA b3s_history TO b3s_history_vault_provenance_owner;
    ALTER TABLE b3s_history.evidence_vault_sv9_judgment_candidates OWNER TO b3s_history_vault_provenance_owner;
    ALTER TABLE b3s_history.evidence_vault_sv9_judgment_evidence_bindings OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.validate_vault_sv9_judgment_candidate_insert() OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.reject_vault_sv9_judgment_candidate_mutation() OWNER TO b3s_history_vault_provenance_owner;
    REVOKE CREATE ON SCHEMA b3s_history FROM b3s_history_vault_provenance_owner;
END;
$$;
REVOKE ALL ON b3s_history.evidence_vault_sv9_judgment_candidates,
              b3s_history.evidence_vault_sv9_judgment_evidence_bindings FROM PUBLIC;
REVOKE ALL ON FUNCTION b3s_history.validate_vault_sv9_judgment_candidate_insert(),
                       b3s_history.reject_vault_sv9_judgment_candidate_mutation() FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'b3s_pr71_app_runtime') THEN
        REVOKE ALL ON SCHEMA b3s_history FROM b3s_pr71_app_runtime;
        GRANT USAGE ON SCHEMA b3s_history TO b3s_pr71_app_runtime;
        REVOKE ALL ON b3s_history.evidence_vault_sv9_judgment_candidates,
                      b3s_history.evidence_vault_sv9_judgment_evidence_bindings FROM b3s_pr71_app_runtime;
        GRANT SELECT, INSERT ON b3s_history.evidence_vault_sv9_judgment_candidates,
                               b3s_history.evidence_vault_sv9_judgment_evidence_bindings TO b3s_pr71_app_runtime;
        IF pg_catalog.has_table_privilege('b3s_pr71_app_runtime', 'b3s_history.evidence_vault_sv9_judgment_candidates', 'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_table_privilege('b3s_pr71_app_runtime', 'b3s_history.evidence_vault_sv9_judgment_evidence_bindings', 'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'SV9 judgment runtime grant exceeds append-only contract';
        END IF;
    END IF;
END;
$$;
