-- Add forward-only analyzer claims to the bounded verified-raw planning context.
-- Historical plans and rows remain immutable; this migration performs no DML.

GRANT USAGE, CREATE ON SCHEMA b3s_history
TO b3s_history_vault_provenance_owner;
GRANT SELECT ON
    b3s_history.evidence_vault_operation_plans,
    b3s_history.scan_runs
TO b3s_history_vault_provenance_owner;
SET LOCAL ROLE b3s_history_vault_provenance_owner;

CREATE FUNCTION b3s_history.read_evidence_vault_raw_planning_context(
    p_workspace_slug text,
    p_source_scan_id text,
    p_canonical_domain text,
    p_workspace_id uuid,
    p_brand_id uuid,
    p_scan_run_id uuid,
    p_semantic_analysis_contract_fingerprint text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    base_context jsonb;
    semantic_claims jsonb := '[]'::jsonb;
    semantic_claim_count bigint := 0;
BEGIN
    IF p_semantic_analysis_contract_fingerprint IS NULL
       OR p_semantic_analysis_contract_fingerprint !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'raw planning semantic analysis contract is invalid';
    END IF;

    -- The frozen v1 projection owns identity validation, lock order, replay,
    -- authority, evidence bounds, and accepted-relation projection.
    base_context := b3s_history.read_evidence_vault_raw_planning_context(
        p_workspace_slug,
        p_source_scan_id,
        p_canonical_domain,
        p_workspace_id,
        p_brand_id,
        p_scan_run_id
    );

    -- Exact source-scan replay remains independent of later history.
    IF base_context -> 'existing_operation_plan' = 'null'::jsonb THEN
        SELECT count(*), COALESCE(
                   jsonb_agg(rows.evidence_fingerprint
                             ORDER BY rows.evidence_fingerprint),
                   '[]'::jsonb
               )
        INTO semantic_claim_count, semantic_claims
        FROM (
            SELECT DISTINCT work.value AS evidence_fingerprint
            FROM (
                SELECT plans.plan_payload
                FROM b3s_history.evidence_vault_operation_plans AS plans
                JOIN b3s_history.scan_runs AS scans
                  ON scans.workspace_id = plans.workspace_id
                 AND scans.brand_id = plans.brand_id
                 AND scans.id = plans.scan_run_id
                WHERE plans.workspace_id = p_workspace_id
                  AND plans.brand_id = p_brand_id
                  AND plans.status <> 'superseded'
                  AND plans.plan_payload #>>
                        '{semantic_context,semantic_analysis_contract,semantic_analysis_contract_fingerprint}'
                        = p_semantic_analysis_contract_fingerprint
                ORDER BY scans.recorded_at DESC, plans.id DESC
                LIMIT 500
            ) AS plans
            CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE
                    WHEN jsonb_typeof(
                        plans.plan_payload #>
                            '{operations,classify_evidence_fingerprints}'
                    ) = 'array'
                    THEN plans.plan_payload #>
                        '{operations,classify_evidence_fingerprints}'
                    ELSE '[]'::jsonb
                END
            ) AS work(value)
            ORDER BY work.value
            LIMIT 5001
        ) AS rows;
        IF semantic_claim_count > 5000
           OR COALESCE(pg_column_size(semantic_claims), 0) > 1048576 THEN
            RAISE EXCEPTION 'raw planning semantic analysis claims exceed bound';
        END IF;
    END IF;

    RETURN jsonb_set(
        jsonb_set(
            jsonb_set(
                base_context,
                '{schema_version}',
                to_jsonb('evidence-vault-raw-planning-context-v2'::text)
            ),
            '{semantic_analysis_contract_fingerprint}',
            to_jsonb(p_semantic_analysis_contract_fingerprint)
        ),
        '{semantic_analysis_claimed_fingerprints}',
        semantic_claims
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION
    b3s_history.read_evidence_vault_raw_planning_context(
        text, text, text, uuid, uuid, uuid, text
    ) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION
    b3s_history.read_evidence_vault_raw_planning_context(
        text, text, text, uuid, uuid, uuid
    ) FROM PUBLIC;

DO $$
DECLARE
    function_signature text;
    grantee text;
    scanner_role_exists boolean;
BEGIN
    scanner_role_exists := EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'b3s_pr71_scanner_ingest'
    );
    FOREACH function_signature IN ARRAY ARRAY[
        'b3s_history.read_evidence_vault_raw_planning_context(text,text,text,uuid,uuid,uuid)',
        'b3s_history.read_evidence_vault_raw_planning_context(text,text,text,uuid,uuid,uuid,text)'
    ] LOOP
        FOR grantee IN
            SELECT DISTINCT roles.rolname
            FROM pg_catalog.pg_proc AS functions
            CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
                functions.proacl,
                pg_catalog.acldefault('f', functions.proowner)
            )) AS grants
            JOIN pg_catalog.pg_roles AS roles ON roles.oid = grants.grantee
            WHERE functions.oid = function_signature::regprocedure
              AND grants.grantee <> functions.proowner
        LOOP
            EXECUTE pg_catalog.format(
                'REVOKE ALL PRIVILEGES ON FUNCTION %s FROM %I',
                function_signature,
                grantee
            );
        END LOOP;
    END LOOP;
    IF scanner_role_exists THEN
        EXECUTE
            'GRANT EXECUTE ON FUNCTION '
            'b3s_history.read_evidence_vault_raw_planning_context('
            'text,text,text,uuid,uuid,uuid,text) '
            'TO b3s_pr71_scanner_ingest';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS functions
        JOIN pg_catalog.pg_roles AS owners
          ON owners.oid = functions.proowner
        WHERE functions.oid =
            'b3s_history.read_evidence_vault_raw_planning_context(text,text,text,uuid,uuid,uuid,text)'::regprocedure
          AND (
              owners.rolname <>
                  'b3s_history_vault_provenance_owner'
              OR NOT functions.prosecdef
          )
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS functions
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
            functions.proacl,
            pg_catalog.acldefault('f', functions.proowner)
        )) AS grants
        LEFT JOIN pg_catalog.pg_roles AS roles
          ON roles.oid = grants.grantee
        WHERE functions.oid = ANY(ARRAY[
            'b3s_history.read_evidence_vault_raw_planning_context(text,text,text,uuid,uuid,uuid)'::regprocedure::oid,
            'b3s_history.read_evidence_vault_raw_planning_context(text,text,text,uuid,uuid,uuid,text)'::regprocedure::oid
        ])
          AND grants.privilege_type = 'EXECUTE'
          AND (
              grants.grantee = 0
              OR grants.is_grantable
              OR (
                  grants.grantee <> functions.proowner
                  AND (
                      functions.oid =
                          'b3s_history.read_evidence_vault_raw_planning_context(text,text,text,uuid,uuid,uuid)'::regprocedure::oid
                      OR NOT scanner_role_exists
                      OR roles.rolname IS DISTINCT FROM
                          'b3s_pr71_scanner_ingest'
                  )
              )
          )
    ) THEN
        RAISE EXCEPTION 'raw semantic planning function ACL is invalid';
    END IF;
END;
$$;

RESET ROLE;
REVOKE CREATE ON SCHEMA b3s_history
FROM b3s_history_vault_provenance_owner;

COMMENT ON FUNCTION b3s_history.read_evidence_vault_raw_planning_context(
    text, text, text, uuid, uuid, uuid, text
) IS
    'Scanner-only bounded planning projection with forward-only semantic analyzer claims and exact replay.';
