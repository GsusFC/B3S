-- Preserve exact trusted-raw replay after the mutable scan lifecycle advances.
--
-- scan_runs.request_payload and its observation hash are immutable under
-- migration 018. The operation-plan identity/payload is immutable under
-- migration 021. Project the original acquisition-time scan envelope from
-- those authorities instead of exposing later report lifecycle mutations to
-- the execute-only raw reader.

-- CREATE OR REPLACE requires schema CREATE for the fixed function owner. Keep
-- that capability inside this migration transaction and revoke it before any
-- postcondition succeeds.
GRANT USAGE, CREATE ON SCHEMA b3s_history
TO b3s_history_vault_provenance_owner;
SET LOCAL ROLE b3s_history_vault_provenance_owner;

CREATE OR REPLACE FUNCTION b3s_history.read_evidence_vault_raw_acquisition(
    p_workspace_slug text,
    p_source_scan_id text
)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT jsonb_build_object(
        'workspace', to_jsonb(workspaces),
        'brand', to_jsonb(brands),
        'scan_run', jsonb_build_object(
            'id', scans.id,
            'workspace_id', scans.workspace_id,
            'brand_id', scans.brand_id,
            'source_scan_id', scans.source_scan_id,
            'source_run_id', scans.source_run_id,
            'status', scans.status,
            'pipeline_version',
                scans.request_payload ->> 'pipeline_version',
            'acquisition_state',
                scans.request_payload ->> 'acquisition_state',
            'requested_at', scans.requested_at,
            'started_at', scans.started_at,
            'completed_at',
                (scans.request_payload ->> 'recorded_at')::timestamptz,
            'recorded_at',
                (scans.request_payload ->> 'recorded_at')::timestamptz,
            'error_summary', scans.error_summary,
            'request_payload', scans.request_payload,
            'metadata', (
                scans.metadata - ARRAY[
                    'persisted_as',
                    'analysis_status',
                    'analysis_result_fingerprint',
                    'candidate_packet_fingerprint',
                    'report_hash'
                ]::text[]
            ) || jsonb_build_object(
                'persisted_as', 'capture_only',
                'analysis_status', CASE
                    WHEN COALESCE(
                        (plans.plan_payload #>>
                            '{operations,llm_required}')::boolean,
                        false
                    )
                    OR COALESCE(
                        (plans.plan_payload #>>
                            '{operations,create_candidate_packet}')::boolean,
                        false
                    )
                    OR COALESCE(
                        (plans.plan_payload #>>
                            '{operations,create_diagnostic_report}')::boolean,
                        false
                    )
                    THEN 'pending'
                    ELSE 'not_required'
                END
            )
        ),
        'operation_plan', to_jsonb(plans),
        'capture', to_jsonb(captures),
        'evidence_records', COALESCE((
            SELECT jsonb_agg(
                to_jsonb(evidence)
                ORDER BY evidence.evidence_ref
            )
            FROM b3s_history.evidence_records AS evidence
            WHERE evidence.capture_id = captures.id
        ), '[]'::jsonb),
        'watermark_event', (
            SELECT to_jsonb(watermarks)
            FROM b3s_history.evidence_vault_capture_watermark_events
                AS watermarks
            WHERE watermarks.brand_id = captures.brand_id
              AND watermarks.capture_id = captures.id
        ),
        'receipts', COALESCE((
            SELECT jsonb_agg(
                to_jsonb(receipts)
                ORDER BY receipts.channel_role
            )
            FROM b3s_history.evidence_vault_raw_acquisition_receipts
                AS receipts
            WHERE receipts.workspace_id = workspaces.id
              AND receipts.scan_run_id = scans.id
              AND receipts.capture_id = captures.id
        ), '[]'::jsonb),
        'evidence_bindings', COALESCE((
            SELECT jsonb_agg(
                to_jsonb(raw_bindings)
                ORDER BY raw_bindings.channel_role
            )
            FROM b3s_history.evidence_vault_raw_evidence_bindings
                AS raw_bindings
            WHERE raw_bindings.workspace_id = workspaces.id
              AND raw_bindings.scan_run_id = scans.id
              AND raw_bindings.capture_id = captures.id
        ), '[]'::jsonb)
    )
    FROM b3s_history.workspaces AS workspaces
    JOIN b3s_history.scan_runs AS scans
      ON scans.workspace_id = workspaces.id
    JOIN b3s_history.brands AS brands
      ON brands.id = scans.brand_id
     AND brands.workspace_id = workspaces.id
    JOIN b3s_history.captures AS captures
      ON captures.scan_run_id = scans.id
     AND captures.brand_id = brands.id
    JOIN b3s_history.evidence_vault_operation_plans AS plans
      ON plans.workspace_id = workspaces.id
     AND plans.brand_id = brands.id
     AND plans.scan_run_id = scans.id
    WHERE workspaces.slug = p_workspace_slug
      AND scans.source_scan_id = p_source_scan_id
      AND EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
        WHERE receipts.workspace_id = workspaces.id
          AND receipts.scan_run_id = scans.id
          AND receipts.capture_id = captures.id
      );
$$;

COMMENT ON FUNCTION
    b3s_history.read_evidence_vault_raw_acquisition(text, text)
IS
    'Scanner-worker-only exact raw replay projection; reconstructs the frozen acquisition scan envelope from immutable request and operation-plan authorities.';

REVOKE EXECUTE ON FUNCTION
    b3s_history.read_evidence_vault_raw_acquisition(text, text)
FROM PUBLIC;

DO $$
DECLARE
    unexpected_grantee text;
    scanner_role_exists boolean;
BEGIN
    scanner_role_exists := EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'b3s_pr71_scanner_ingest'
    );
    FOR unexpected_grantee IN
        SELECT DISTINCT roles.rolname
        FROM pg_catalog.pg_proc AS functions
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(
                functions.proacl,
                pg_catalog.acldefault('f', functions.proowner)
            )
        ) AS grants
        JOIN pg_catalog.pg_roles AS roles ON roles.oid = grants.grantee
        WHERE functions.oid =
            'b3s_history.read_evidence_vault_raw_acquisition(text,text)'
                ::regprocedure
          AND grants.grantee <> functions.proowner
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL PRIVILEGES ON FUNCTION %s FROM %I',
            'b3s_history.read_evidence_vault_raw_acquisition(text,text)',
            unexpected_grantee
        );
    END LOOP;
    IF scanner_role_exists THEN
        GRANT EXECUTE ON FUNCTION
            b3s_history.read_evidence_vault_raw_acquisition(text, text)
        TO b3s_pr71_scanner_ingest;
    END IF;
END;
$$;

RESET ROLE;
REVOKE CREATE ON SCHEMA b3s_history
FROM b3s_history_vault_provenance_owner;

DO $$
DECLARE
    actual_owner text;
    scanner_role_exists boolean;
BEGIN
    IF pg_catalog.has_schema_privilege(
        'b3s_history_vault_provenance_owner',
        'b3s_history',
        'CREATE'
    ) THEN
        RAISE EXCEPTION 'provenance owner retains forbidden schema CREATE';
    END IF;

    SELECT roles.rolname INTO actual_owner
    FROM pg_catalog.pg_proc AS functions
    JOIN pg_catalog.pg_roles AS roles ON roles.oid = functions.proowner
    WHERE functions.oid =
        'b3s_history.read_evidence_vault_raw_acquisition(text,text)'
            ::regprocedure
      AND functions.prosecdef
      AND functions.proconfig = ARRAY['search_path=pg_catalog']::text[];
    IF actual_owner IS DISTINCT FROM
            'b3s_history_vault_provenance_owner' THEN
        RAISE EXCEPTION
            'raw acquisition replay projection has incorrect owner/config %',
            actual_owner;
    END IF;

    scanner_role_exists := EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'b3s_pr71_scanner_ingest'
    );
    IF scanner_role_exists AND NOT pg_catalog.has_function_privilege(
        'b3s_pr71_scanner_ingest',
        'b3s_history.read_evidence_vault_raw_acquisition(text,text)',
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION
            'raw acquisition replay projection lacks scanner EXECUTE';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS functions
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(
                functions.proacl,
                pg_catalog.acldefault('f', functions.proowner)
            )
        ) AS grants
        LEFT JOIN pg_catalog.pg_roles AS roles
          ON roles.oid = grants.grantee
        WHERE functions.oid =
            'b3s_history.read_evidence_vault_raw_acquisition(text,text)'
                ::regprocedure
          AND grants.privilege_type = 'EXECUTE'
          AND (
              grants.grantee = 0
              OR grants.is_grantable
              OR (
                  grants.grantee <> functions.proowner
                  AND (
                      NOT scanner_role_exists
                      OR roles.rolname IS DISTINCT FROM
                            'b3s_pr71_scanner_ingest'
                  )
              )
          )
    ) THEN
        RAISE EXCEPTION
            'raw acquisition replay projection retains unexpected EXECUTE';
    END IF;
END;
$$;
