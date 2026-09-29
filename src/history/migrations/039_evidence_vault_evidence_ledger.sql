-- Shadow evidence ledger: for each prior evidence identity, whether a re-scan's capture saw it,
-- verified its absence or could not verify it (src/services/evidence_vault_evidence_ledger.py).
-- Rows are append-only diagnostics with no runtime effect: nothing that scores, selects a
-- canonical report or publishes reads them.
CREATE TABLE b3s_history.evidence_vault_evidence_ledger_rows (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    brand_id uuid NOT NULL,
    scan_run_id uuid NOT NULL,
    capture_id uuid NOT NULL,
    source_scan_id text NOT NULL CHECK (length(source_scan_id) BETWEEN 1 AND 300),
    prior_scan_run_id uuid NOT NULL,
    prior_capture_id uuid NOT NULL,
    prior_source_scan_id text NOT NULL CHECK (length(prior_source_scan_id) BETWEEN 1 AND 300),
    evidence_ref text NOT NULL CHECK (length(evidence_ref) BETWEEN 1 AND 1000),
    evidence_fingerprint text NOT NULL CHECK (evidence_fingerprint ~ '^[0-9a-f]{64}$'),
    evidence_id text NOT NULL CHECK (evidence_id ~ '^[0-9a-f]{64}$'),
    source_identity_id text NOT NULL CHECK (source_identity_id ~ '^[0-9a-f]{64}$'),
    source_key text NOT NULL,
    -- Not a closed enum: an unregistered evidence class passes through as not_verified.
    evidence_class text NOT NULL CHECK (evidence_class ~ '^[a-z0-9_]{1,64}$'),
    state text NOT NULL CHECK (state IN ('seen', 'verified_absent', 'not_verified')),
    reason_codes jsonb NOT NULL CHECK (jsonb_typeof(reason_codes) = 'array'),
    health jsonb NOT NULL CHECK (jsonb_typeof(health) = 'object'),
    shown_to_core jsonb NOT NULL CHECK (jsonb_typeof(shown_to_core) = 'object'),
    policy_version text NOT NULL CHECK (policy_version ~ '^evidence-vault-evidence-ledger-v[0-9]+$'),
    runtime_effect boolean NOT NULL DEFAULT false CHECK (runtime_effect = false),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (capture_id <> prior_capture_id),
    UNIQUE (capture_id, prior_capture_id, evidence_ref),
    FOREIGN KEY (workspace_id, brand_id) REFERENCES b3s_history.brands (workspace_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (brand_id, scan_run_id, capture_id) REFERENCES b3s_history.captures (brand_id, scan_run_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (brand_id, prior_scan_run_id, prior_capture_id) REFERENCES b3s_history.captures (brand_id, scan_run_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (prior_capture_id, evidence_ref) REFERENCES b3s_history.evidence_records (capture_id, evidence_ref) ON DELETE RESTRICT
);
CREATE INDEX evidence_vault_evidence_ledger_rows_scan_idx
    ON b3s_history.evidence_vault_evidence_ledger_rows (workspace_id, brand_id, source_scan_id);
CREATE FUNCTION b3s_history.reject_vault_evidence_ledger_row_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'evidence ledger rows are append-only'; END;
$$;
CREATE TRIGGER reject_vault_evidence_ledger_row_mutation
    BEFORE UPDATE OR DELETE OR TRUNCATE ON b3s_history.evidence_vault_evidence_ledger_rows
    FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_vault_evidence_ledger_row_mutation();
DO $$
DECLARE owner_role record;
BEGIN
    SELECT * INTO owner_role FROM pg_catalog.pg_roles WHERE rolname = 'b3s_history_vault_provenance_owner';
    IF owner_role.oid IS NULL OR owner_role.rolcanlogin OR owner_role.rolsuper
       OR owner_role.rolcreaterole OR owner_role.rolcreatedb OR owner_role.rolreplication OR owner_role.rolbypassrls THEN
        RAISE EXCEPTION 'evidence ledger owner is unsafe';
    END IF;
    GRANT USAGE, CREATE ON SCHEMA b3s_history TO b3s_history_vault_provenance_owner;
    ALTER TABLE b3s_history.evidence_vault_evidence_ledger_rows OWNER TO b3s_history_vault_provenance_owner;
    ALTER FUNCTION b3s_history.reject_vault_evidence_ledger_row_mutation() OWNER TO b3s_history_vault_provenance_owner;
    REVOKE CREATE ON SCHEMA b3s_history FROM b3s_history_vault_provenance_owner;
END;
$$;
REVOKE ALL ON b3s_history.evidence_vault_evidence_ledger_rows FROM PUBLIC;
REVOKE ALL ON FUNCTION b3s_history.reject_vault_evidence_ledger_row_mutation() FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'b3s_pr71_app_runtime') THEN
        REVOKE ALL ON SCHEMA b3s_history FROM b3s_pr71_app_runtime;
        GRANT USAGE ON SCHEMA b3s_history TO b3s_pr71_app_runtime;
        REVOKE ALL ON b3s_history.evidence_vault_evidence_ledger_rows FROM b3s_pr71_app_runtime;
        GRANT SELECT, INSERT ON b3s_history.evidence_vault_evidence_ledger_rows TO b3s_pr71_app_runtime;
        IF pg_catalog.has_table_privilege('b3s_pr71_app_runtime', 'b3s_history.evidence_vault_evidence_ledger_rows', 'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'evidence ledger runtime grant exceeds append-only contract';
        END IF;
    END IF;
END;
$$;
