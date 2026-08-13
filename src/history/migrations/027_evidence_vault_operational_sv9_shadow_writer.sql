-- Trusted append capability for the non-authoritative SV9 shadow ledger.
--
-- The writer is a capability group, never a login or object owner.  It gets
-- only the packet/adoption rows required to rederive one requested observation
-- plus INSERT/replay SELECT on this append-only ledger.  Scanner and runtime
-- roles deliberately receive none of this surface.

DO $$
DECLARE
    actor record;
    writer_role record;
BEGIN
    SELECT rolsuper, rolcreaterole INTO actor
    FROM pg_catalog.pg_roles WHERE rolname = current_user;

    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'b3s_history_vault_sv9_shadow_writer'
    ) AND (actor.rolsuper OR actor.rolcreaterole) THEN
        BEGIN
            CREATE ROLE b3s_history_vault_sv9_shadow_writer
                NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
                NOREPLICATION NOBYPASSRLS;
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END;
    END IF;

    SELECT * INTO writer_role
    FROM pg_catalog.pg_roles
    WHERE rolname = 'b3s_history_vault_sv9_shadow_writer';
    IF writer_role.oid IS NULL THEN
        RAISE EXCEPTION
            'required NOLOGIN NOINHERIT SV9 shadow writer role is not provisioned';
    END IF;
    IF writer_role.rolcanlogin OR writer_role.rolinherit
       OR writer_role.rolsuper OR writer_role.rolcreaterole
       OR writer_role.rolcreatedb OR writer_role.rolreplication
       OR writer_role.rolbypassrls THEN
        RAISE EXCEPTION
            'b3s_history_vault_sv9_shadow_writer has unsafe role attributes';
    END IF;
    -- The writer may be granted to a tightly controlled LOGIN externally, but
    -- it must never itself inherit any ambient capability.
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members
        WHERE member = writer_role.oid
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_sv9_shadow_writer must not inherit another role';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_shdepend AS dependencies
        WHERE dependencies.refclassid = 'pg_catalog.pg_authid'::regclass
          AND dependencies.refobjid = writer_role.oid
          AND dependencies.deptype = 'o'
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_sv9_shadow_writer must not own database objects';
    END IF;
END;
$$;

-- Helpers created by 025/026 and the ledger are provenance-owned durable
-- objects.  Temporary CREATE is needed for an ownership transfer on managed
-- PostgreSQL, then immediately removed again.
DO $$
DECLARE
    function_signature text;
    actual_owner text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'b3s_history_vault_provenance_owner'
    ) THEN
        RAISE EXCEPTION 'required NOLOGIN provenance owner is not provisioned';
    END IF;
    GRANT USAGE, CREATE ON SCHEMA b3s_history
        TO b3s_history_vault_provenance_owner;
    ALTER TABLE b3s_history.evidence_vault_operational_sv9_shadow_assessments
        OWNER TO b3s_history_vault_provenance_owner;
    FOREACH function_signature IN ARRAY ARRAY[
        'b3s_history.evidence_vault_sv9_shadow_semantic_tiles_are_valid(jsonb)',
        'b3s_history.evidence_vault_sv9_shadow_verification_is_valid(jsonb)',
        'b3s_history.evidence_vault_sv9_shadow_assessment_output_is_structurally_valid(jsonb)',
        'b3s_history.validate_vault_operational_sv9_shadow_assessment_insert()',
        'b3s_history.reject_vault_operational_sv9_shadow_assessment_mutation()'
    ] LOOP
        EXECUTE pg_catalog.format(
            'ALTER FUNCTION %s OWNER TO b3s_history_vault_provenance_owner',
            function_signature
        );
    END LOOP;
    REVOKE CREATE ON SCHEMA b3s_history
        FROM b3s_history_vault_provenance_owner;

    SELECT roles.rolname INTO actual_owner
    FROM pg_catalog.pg_class AS relations
    JOIN pg_catalog.pg_namespace AS schemas ON schemas.oid = relations.relnamespace
    JOIN pg_catalog.pg_roles AS roles ON roles.oid = relations.relowner
    WHERE schemas.nspname = 'b3s_history'
      AND relations.relname = 'evidence_vault_operational_sv9_shadow_assessments';
    IF actual_owner IS DISTINCT FROM 'b3s_history_vault_provenance_owner' THEN
        RAISE EXCEPTION 'SV9 shadow ledger has incorrect owner %', actual_owner;
    END IF;
    FOREACH function_signature IN ARRAY ARRAY[
        'b3s_history.evidence_vault_sv9_shadow_semantic_tiles_are_valid(jsonb)',
        'b3s_history.evidence_vault_sv9_shadow_verification_is_valid(jsonb)',
        'b3s_history.evidence_vault_sv9_shadow_assessment_output_is_structurally_valid(jsonb)',
        'b3s_history.validate_vault_operational_sv9_shadow_assessment_insert()',
        'b3s_history.reject_vault_operational_sv9_shadow_assessment_mutation()'
    ] LOOP
        SELECT roles.rolname INTO actual_owner
        FROM pg_catalog.pg_proc AS functions
        JOIN pg_catalog.pg_roles AS roles ON roles.oid = functions.proowner
        WHERE functions.oid = function_signature::pg_catalog.regprocedure;
        IF actual_owner IS DISTINCT FROM 'b3s_history_vault_provenance_owner' THEN
            RAISE EXCEPTION 'SV9 shadow helper has incorrect owner: %', function_signature;
        END IF;
    END LOOP;
END;
$$;

REVOKE ALL ON SCHEMA b3s_history FROM b3s_history_vault_sv9_shadow_writer;
REVOKE CREATE ON SCHEMA b3s_history FROM b3s_history_vault_sv9_shadow_writer;
REVOKE ALL ON b3s_history.schema_migrations,
              b3s_history.workspaces,
              b3s_history.brands,
              b3s_history.evidence_vault_canonical_memory_packets,
              b3s_history.evidence_vault_canonical_memory_promotion_events,
              b3s_history.evidence_vault_operational_sv9_shadow_assessments
    FROM b3s_history_vault_sv9_shadow_writer;
GRANT USAGE ON SCHEMA b3s_history TO b3s_history_vault_sv9_shadow_writer;
GRANT SELECT ON b3s_history.schema_migrations,
                b3s_history.workspaces,
                b3s_history.brands,
                b3s_history.evidence_vault_canonical_memory_packets,
                b3s_history.evidence_vault_canonical_memory_promotion_events,
                b3s_history.evidence_vault_operational_sv9_shadow_assessments
    TO b3s_history_vault_sv9_shadow_writer;
GRANT INSERT ON b3s_history.evidence_vault_operational_sv9_shadow_assessments
    TO b3s_history_vault_sv9_shadow_writer;

-- PostgreSQL checks EXECUTE for functions used by CHECK constraints at DML
-- time.  The writer therefore receives only the three immutable predicates;
-- it never receives the mutable trigger functions as a callable capability.
REVOKE ALL ON FUNCTION
    b3s_history.evidence_vault_sv9_shadow_semantic_tiles_are_valid(jsonb),
    b3s_history.evidence_vault_sv9_shadow_verification_is_valid(jsonb),
    b3s_history.evidence_vault_sv9_shadow_assessment_output_is_structurally_valid(jsonb),
    b3s_history.validate_vault_operational_sv9_shadow_assessment_insert(),
    b3s_history.reject_vault_operational_sv9_shadow_assessment_mutation()
    FROM b3s_history_vault_sv9_shadow_writer;
GRANT EXECUTE ON FUNCTION
    b3s_history.evidence_vault_sv9_shadow_semantic_tiles_are_valid(jsonb),
    b3s_history.evidence_vault_sv9_shadow_verification_is_valid(jsonb),
    b3s_history.evidence_vault_sv9_shadow_assessment_output_is_structurally_valid(jsonb)
    TO b3s_history_vault_sv9_shadow_writer;

-- Do not accidentally extend this capability to the existing shadow runtime or
-- scanner ingest role.  Some disposable upgrade fixtures do not provision the
-- scanner login, so revoke only when that role exists.
REVOKE ALL ON b3s_history.evidence_vault_operational_sv9_shadow_assessments
    FROM b3s_history_vault_runtime_read;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'b3s_pr71_scanner_ingest'
    ) THEN
        REVOKE ALL ON b3s_history.evidence_vault_operational_sv9_shadow_assessments
            FROM b3s_pr71_scanner_ingest;
        REVOKE b3s_history_vault_sv9_shadow_writer FROM b3s_pr71_scanner_ingest;
    END IF;
END;
$$;
REVOKE b3s_history_vault_sv9_shadow_writer FROM b3s_history_vault_runtime_read;

DO $$
DECLARE
    relation_name text;
    writer_oid oid;
BEGIN
    SELECT oid INTO writer_oid FROM pg_catalog.pg_roles
    WHERE rolname = 'b3s_history_vault_sv9_shadow_writer';
    IF NOT pg_catalog.has_schema_privilege(
        'b3s_history_vault_sv9_shadow_writer', 'b3s_history', 'USAGE'
    ) OR pg_catalog.has_schema_privilege(
        'b3s_history_vault_sv9_shadow_writer', 'b3s_history', 'CREATE'
    ) THEN
        RAISE EXCEPTION 'SV9 shadow writer schema scope is unsafe';
    END IF;
    FOR relation_name IN
        SELECT relations.relname
        FROM pg_catalog.pg_class AS relations
        JOIN pg_catalog.pg_namespace AS schemas ON schemas.oid = relations.relnamespace
        WHERE schemas.nspname = 'b3s_history'
          AND relations.relkind IN ('r', 'p', 'v', 'm', 'f')
    LOOP
        IF relation_name IN (
            'schema_migrations', 'workspaces', 'brands', 'evidence_vault_canonical_memory_packets',
            'evidence_vault_canonical_memory_promotion_events',
            'evidence_vault_operational_sv9_shadow_assessments'
        ) THEN
            CONTINUE;
        END IF;
        IF pg_catalog.has_table_privilege(
            'b3s_history_vault_sv9_shadow_writer',
            pg_catalog.format('b3s_history.%I', relation_name),
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
        ) THEN
            RAISE EXCEPTION 'SV9 shadow writer has forbidden relation privilege on %', relation_name;
        END IF;
    END LOOP;
    IF NOT pg_catalog.has_table_privilege(
        'b3s_history_vault_sv9_shadow_writer',
        'b3s_history.evidence_vault_operational_sv9_shadow_assessments', 'INSERT'
    ) OR pg_catalog.has_table_privilege(
        'b3s_history_vault_sv9_shadow_writer',
        'b3s_history.evidence_vault_operational_sv9_shadow_assessments',
        'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
    ) THEN
        RAISE EXCEPTION 'SV9 shadow ledger writer privileges are unsafe';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS functions
        WHERE functions.oid = ANY(ARRAY[
            'b3s_history.validate_vault_operational_sv9_shadow_assessment_insert()'::regprocedure::oid,
            'b3s_history.reject_vault_operational_sv9_shadow_assessment_mutation()'::regprocedure::oid
        ])
          AND pg_catalog.has_function_privilege(
              'b3s_history_vault_sv9_shadow_writer', functions.oid, 'EXECUTE'
          )
    ) THEN
        RAISE EXCEPTION 'SV9 shadow writer can execute private trigger functions';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members
        WHERE roleid = writer_oid
          AND member IN (
              SELECT oid FROM pg_catalog.pg_roles
              WHERE rolname IN ('b3s_history_vault_runtime_read', 'b3s_pr71_scanner_ingest')
          )
    ) THEN
        RAISE EXCEPTION 'runtime or scanner receives direct SV9 shadow writer membership';
    END IF;
    -- Detect inherited membership too: revoking a direct grant is not enough
    -- if a deployment has routed either principal through another group.
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS principals
        WHERE principals.rolname IN (
            'b3s_history_vault_runtime_read', 'b3s_pr71_scanner_ingest'
        )
          AND pg_catalog.pg_has_role(
              principals.oid, writer_oid, 'MEMBER'
          )
    ) THEN
        RAISE EXCEPTION 'runtime or scanner inherits SV9 shadow writer membership';
    END IF;
END;
$$;
