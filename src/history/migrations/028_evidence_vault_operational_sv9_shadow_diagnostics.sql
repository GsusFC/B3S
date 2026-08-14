-- Sanitized, read-only diagnostics for the non-authoritative SV9 shadow ledger.
--
-- The raw append-only table remains private.  This view exposes only bounded
-- scalar observations; it adds no writer, Scanner, score-selection, ranking,
-- canonical-authority, or production-runtime capability.

CREATE VIEW b3s_history.evidence_vault_operational_sv9_shadow_diagnostics_v1
WITH (security_barrier = true, security_invoker = false)
AS
SELECT assessments.brand_id,
       assessments.schema_version,
       assessments.evaluation_identity,
       assessments.operational_packet_fingerprint,
       assessments.assessment_status,
       (assessments.assessment_output ->> 'sv9_score')::integer AS sv9_score,
       (assessments.assessment_output ->> 'base_average')::numeric AS base_average,
       (assessments.assessment_output ->> 'magnetism_capped')::boolean AS magnetism_capped,
       assessments.assessment_output ->> 'assessment_fingerprint'
           AS assessment_fingerprint,
       assessments.assessment_output ->> 'score_fingerprint'
           AS score_fingerprint,
       assessments.semantic_provenance_fingerprint,
       (assessments.verification_requirements #>> '{counts,pending}')::integer
           AS verification_pending_count,
       (assessments.verification_requirements #>> '{counts,verified}')::integer
           AS verification_verified_count,
       (assessments.verification_requirements #>> '{counts,disputed}')::integer
           AS verification_disputed_count,
       (assessments.verification_requirements #>> '{counts,stale}')::integer
           AS verification_stale_count,
       (assessments.verification_requirements #>> '{counts,unverifiable}')::integer
           AS verification_unverifiable_count,
       assessments.authority,
       assessments.production_runtime_effect,
       assessments.scanner_runtime_effect,
       assessments.created_at
FROM b3s_history.evidence_vault_operational_sv9_shadow_assessments AS assessments;

ALTER VIEW b3s_history.evidence_vault_operational_sv9_shadow_diagnostics_v1
    OWNER TO b3s_history_vault_provenance_owner;

REVOKE ALL ON b3s_history.evidence_vault_operational_sv9_shadow_diagnostics_v1
    FROM PUBLIC,
         b3s_history_vault_sv9_shadow_writer,
         b3s_history_vault_runtime_read;

DO $$
DECLARE
    actual_owner text;
    actual_columns text[];
    owner_role record;
    principal text;
BEGIN
    SELECT * INTO owner_role
    FROM pg_catalog.pg_roles
    WHERE rolname = 'b3s_history_vault_provenance_owner';
    IF owner_role.oid IS NULL
       OR owner_role.rolcanlogin
       OR owner_role.rolsuper
       OR owner_role.rolcreaterole
       OR owner_role.rolcreatedb
       OR owner_role.rolreplication
       OR owner_role.rolbypassrls THEN
        RAISE EXCEPTION 'SV9 shadow diagnostic owner role is unsafe';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members
        WHERE member = owner_role.oid
    ) THEN
        RAISE EXCEPTION 'SV9 shadow diagnostic owner inherits another role';
    END IF;

    SELECT roles.rolname INTO actual_owner
    FROM pg_catalog.pg_class AS relations
    JOIN pg_catalog.pg_namespace AS schemas ON schemas.oid = relations.relnamespace
    JOIN pg_catalog.pg_roles AS roles ON roles.oid = relations.relowner
    WHERE schemas.nspname = 'b3s_history'
      AND relations.relname = 'evidence_vault_operational_sv9_shadow_diagnostics_v1'
      AND relations.relkind = 'v';
    IF actual_owner IS DISTINCT FROM 'b3s_history_vault_provenance_owner' THEN
        RAISE EXCEPTION 'SV9 shadow diagnostic view has unsafe owner %', actual_owner;
    END IF;

    SELECT pg_catalog.array_agg(attributes.attname::text ORDER BY attributes.attnum)
    INTO actual_columns
    FROM pg_catalog.pg_attribute AS attributes
    WHERE attributes.attrelid =
              'b3s_history.evidence_vault_operational_sv9_shadow_diagnostics_v1'::regclass
      AND attributes.attnum > 0
      AND NOT attributes.attisdropped;
    IF actual_columns IS DISTINCT FROM ARRAY[
        'brand_id', 'schema_version', 'evaluation_identity',
        'operational_packet_fingerprint', 'assessment_status', 'sv9_score',
        'base_average', 'magnetism_capped', 'assessment_fingerprint',
        'score_fingerprint', 'semantic_provenance_fingerprint',
        'verification_pending_count', 'verification_verified_count',
        'verification_disputed_count', 'verification_stale_count',
        'verification_unverifiable_count', 'authority',
        'production_runtime_effect', 'scanner_runtime_effect', 'created_at'
    ]::text[] THEN
        RAISE EXCEPTION 'SV9 shadow diagnostic view columns are not exact';
    END IF;

    FOREACH principal IN ARRAY ARRAY[
        'b3s_history_vault_sv9_shadow_writer',
        'b3s_history_vault_runtime_read',
        'b3s_pr71_scanner_ingest',
        'b3s_pr71_app_runtime'
    ] LOOP
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = principal
        ) THEN
            EXECUTE pg_catalog.format(
                'REVOKE ALL ON b3s_history.evidence_vault_operational_sv9_shadow_diagnostics_v1 FROM %I',
                principal
            );
            IF pg_catalog.has_table_privilege(
                principal,
                'b3s_history.evidence_vault_operational_sv9_shadow_diagnostics_v1',
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
            ) THEN
                RAISE EXCEPTION
                    'principal % inherits access to the SV9 shadow diagnostic view',
                    principal;
            END IF;
        END IF;
    END LOOP;


    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'b3s_pr71_app_runtime'
    ) THEN
        REVOKE ALL ON
            b3s_history.evidence_vault_operational_sv9_shadow_assessments
            FROM b3s_pr71_app_runtime;
        IF pg_catalog.has_table_privilege(
            'b3s_pr71_app_runtime',
            'b3s_history.evidence_vault_operational_sv9_shadow_assessments',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
        ) THEN
            RAISE EXCEPTION
                'PR71 app runtime inherits access to the raw SV9 shadow ledger';
        END IF;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS relations
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(
                relations.relacl,
                pg_catalog.acldefault('r', relations.relowner)
            )
        ) AS privileges
        WHERE relations.oid =
                  'b3s_history.evidence_vault_operational_sv9_shadow_diagnostics_v1'::regclass
          AND privileges.grantee = 0
          AND privileges.privilege_type IN (
              'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
              'REFERENCES', 'TRIGGER'
          )
    ) THEN
        RAISE EXCEPTION 'PUBLIC can access the SV9 shadow diagnostic view';
    END IF;
END;
$$;
