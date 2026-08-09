-- Bounded execute-only read projections for Evidence Vault C7 shadow readiness.
--
-- Migration 019 is immutable and may already be recorded by checksum.  This
-- migration adds a separate read capability without changing any append,
-- binding, disposition, scoring, or runtime-authority surface.

-- The SECURITY DEFINER owner created by 019 must still be a narrow NOLOGIN
-- role, and the release actor must be able to transfer the new functions to it.
DO $$
DECLARE
    actor record;
    owner_role record;
    can_set_owner boolean := false;
    server_version integer := current_setting('server_version_num')::integer;
BEGIN
    SELECT rolsuper, rolcreaterole
    INTO actor
    FROM pg_catalog.pg_roles
    WHERE rolname = current_user;

    SELECT *
    INTO owner_role
    FROM pg_catalog.pg_roles
    WHERE rolname = 'b3s_history_vault_provenance_owner';

    IF owner_role.oid IS NULL THEN
        RAISE EXCEPTION
            'required NOLOGIN provenance owner is not provisioned';
    END IF;
    IF owner_role.rolcanlogin
       OR owner_role.rolsuper
       OR owner_role.rolcreaterole
       OR owner_role.rolcreatedb
       OR owner_role.rolreplication
       OR owner_role.rolbypassrls THEN
        RAISE EXCEPTION
            'b3s_history_vault_provenance_owner has unsafe role attributes';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS memberships
        WHERE memberships.member = owner_role.oid
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_provenance_owner must not inherit another role';
    END IF;

    IF actor.rolsuper THEN
        can_set_owner := true;
    ELSIF server_version >= 160000 THEN
        EXECUTE
            'SELECT pg_catalog.pg_has_role(current_user, '
            '''b3s_history_vault_provenance_owner'', ''SET'')'
        INTO can_set_owner;
    ELSE
        SELECT pg_catalog.pg_has_role(
            current_user,
            'b3s_history_vault_provenance_owner',
            'MEMBER'
        ) INTO can_set_owner;
    END IF;
    IF NOT can_set_owner THEN
        RAISE EXCEPTION
            'migrator must be able to SET required provenance owner';
    END IF;
END;
$$;

-- A managed PostgreSQL deployment without CREATEROLE must pre-provision this
-- exact NOLOGIN NOINHERIT role.  Existing role drift is a migration conflict;
-- it is never silently repaired because members of this group receive a
-- SECURITY DEFINER capability.
DO $$
DECLARE
    actor record;
    runtime_role record;
BEGIN
    SELECT rolsuper, rolcreaterole
    INTO actor
    FROM pg_catalog.pg_roles
    WHERE rolname = current_user;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'b3s_history_vault_runtime_read'
    ) AND (actor.rolsuper OR actor.rolcreaterole) THEN
        BEGIN
            CREATE ROLE b3s_history_vault_runtime_read
                NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                NOINHERIT NOREPLICATION NOBYPASSRLS;
        EXCEPTION
            WHEN duplicate_object THEN
                NULL;
        END;
    END IF;

    SELECT *
    INTO runtime_role
    FROM pg_catalog.pg_roles
    WHERE rolname = 'b3s_history_vault_runtime_read';

    IF runtime_role.oid IS NULL THEN
        RAISE EXCEPTION
            'required NOLOGIN NOINHERIT Vault runtime-read role is not provisioned';
    END IF;
    IF runtime_role.rolcanlogin
       OR runtime_role.rolinherit
       OR runtime_role.rolsuper
       OR runtime_role.rolcreaterole
       OR runtime_role.rolcreatedb
       OR runtime_role.rolreplication
       OR runtime_role.rolbypassrls THEN
        RAISE EXCEPTION
            'b3s_history_vault_runtime_read has unsafe role attributes';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS memberships
        WHERE memberships.member = runtime_role.oid
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_runtime_read must not inherit another role';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_shdepend AS dependencies
        WHERE dependencies.refclassid =
                'pg_catalog.pg_authid'::pg_catalog.regclass
          AND dependencies.refobjid = runtime_role.oid
          AND dependencies.deptype = 'o'
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_runtime_read must not own database objects';
    END IF;
    IF pg_catalog.has_schema_privilege(
        'b3s_history_vault_runtime_read', 'b3s_history', 'CREATE'
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_runtime_read retains forbidden schema CREATE';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS relations
        JOIN pg_catalog.pg_namespace AS schemas
          ON schemas.oid = relations.relnamespace
        WHERE schemas.nspname = 'b3s_history'
          AND relations.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (
              pg_catalog.has_table_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'SELECT'
              ) OR pg_catalog.has_table_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'INSERT'
              ) OR pg_catalog.has_table_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'UPDATE'
              ) OR pg_catalog.has_table_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'DELETE'
              ) OR pg_catalog.has_table_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'TRUNCATE'
              ) OR pg_catalog.has_table_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'REFERENCES'
              ) OR pg_catalog.has_table_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'TRIGGER'
              ) OR pg_catalog.has_any_column_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'SELECT'
              ) OR pg_catalog.has_any_column_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'INSERT'
              ) OR pg_catalog.has_any_column_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'UPDATE'
              ) OR pg_catalog.has_any_column_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'REFERENCES'
              )
          )
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_runtime_read has forbidden table privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS sequences
        JOIN pg_catalog.pg_namespace AS schemas
          ON schemas.oid = sequences.relnamespace
        WHERE schemas.nspname = 'b3s_history'
          AND sequences.relkind = 'S'
          AND (
              pg_catalog.has_sequence_privilege(
                  'b3s_history_vault_runtime_read', sequences.oid, 'USAGE'
              ) OR pg_catalog.has_sequence_privilege(
                  'b3s_history_vault_runtime_read', sequences.oid, 'SELECT'
              ) OR pg_catalog.has_sequence_privilege(
                  'b3s_history_vault_runtime_read', sequences.oid, 'UPDATE'
              )
          )
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_runtime_read has forbidden sequence privileges';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS functions
        WHERE functions.prosecdef
          AND pg_catalog.has_function_privilege(
              'b3s_history_vault_runtime_read', functions.oid, 'EXECUTE'
          )
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_runtime_read has preexisting SECURITY DEFINER EXECUTE';
    END IF;
END;
$$;

-- The shared SECURITY DEFINER owner needs read-only access to the three
-- committed operational journals newly projected by the context function.
GRANT SELECT ON
    b3s_history.evidence_vault_canonical_memory_promotion_events,
    b3s_history.evidence_vault_operational_relation_reviews,
    b3s_history.evidence_vault_canonical_score_evaluations
    TO b3s_history_vault_provenance_owner;

-- Bounded brand context.  Exact counts are computed from skinny aggregates;
-- wide JSON rows are independently capped at limit + 1 so overflow can never
-- masquerade as a complete witness.  Watermarks are intentionally a derived
-- latest-two selector rather than a complete journal projection.
CREATE FUNCTION b3s_history.read_evidence_vault_c7_shadow_context(
    p_workspace_slug text,
    p_canonical_brand text
)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    WITH brand_count AS (
        SELECT count(*) AS total
        FROM b3s_history.brands AS brands
        JOIN b3s_history.workspaces AS workspaces
          ON workspaces.id = brands.workspace_id
        WHERE workspaces.slug = p_workspace_slug
          AND brands.canonical_domain = p_canonical_brand
    ), brand_rows AS MATERIALIZED (
        SELECT brands.*
        FROM b3s_history.brands AS brands
        JOIN b3s_history.workspaces AS workspaces
          ON workspaces.id = brands.workspace_id
        WHERE workspaces.slug = p_workspace_slug
          AND brands.canonical_domain = p_canonical_brand
        ORDER BY brands.id
        LIMIT 2
    ), selected_brand AS MATERIALIZED (
        SELECT rows.*
        FROM brand_rows AS rows
        WHERE (SELECT total FROM brand_count) = 1
    ), watermark_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_capture_watermark_events AS watermarks
        WHERE watermarks.brand_id = (SELECT id FROM selected_brand)
    ), latest_watermark_rows AS MATERIALIZED (
        SELECT watermarks.*
        FROM b3s_history.evidence_vault_capture_watermark_events AS watermarks
        WHERE watermarks.brand_id = (SELECT id FROM selected_brand)
        ORDER BY watermarks.capture_sequence DESC, watermarks.id DESC
        LIMIT 2
    ), binding_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_verified_c7_lineage_bindings AS bindings
        WHERE bindings.brand_id = (SELECT id FROM selected_brand)
          AND bindings.watermark_event_id IN (
              SELECT id FROM latest_watermark_rows
          )
    ), binding_rows AS MATERIALIZED (
        SELECT bindings.*
        FROM b3s_history.evidence_vault_verified_c7_lineage_bindings AS bindings
        WHERE bindings.brand_id = (SELECT id FROM selected_brand)
          AND bindings.watermark_event_id IN (
              SELECT id FROM latest_watermark_rows
          )
        ORDER BY bindings.capture_sequence, bindings.id
        LIMIT 9
    ), adoption_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_canonical_memory_promotion_events AS events
        WHERE events.brand_id = (SELECT id FROM selected_brand)
          AND events.adoption_kind = 'operational_v2'
    ), adoption_rows AS MATERIALIZED (
        SELECT events.*
        FROM b3s_history.evidence_vault_canonical_memory_promotion_events AS events
        WHERE events.brand_id = (SELECT id FROM selected_brand)
          AND events.adoption_kind = 'operational_v2'
        ORDER BY events.sequence, events.id
        LIMIT 129
    ), operational_packet_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_canonical_memory_packets AS packets
        WHERE packets.brand_id = (SELECT id FROM selected_brand)
          AND packets.packet_kind = 'operational_v2'
          AND EXISTS (
              SELECT 1
              FROM b3s_history.evidence_vault_canonical_memory_promotion_events
                   AS events
              WHERE events.brand_id = packets.brand_id
                AND events.adoption_kind = 'operational_v2'
                AND events.candidate_packet_fingerprint =
                    packets.packet_fingerprint
          )
    ), operational_packet_rows AS MATERIALIZED (
        SELECT packets.*
        FROM b3s_history.evidence_vault_canonical_memory_packets AS packets
        WHERE packets.brand_id = (SELECT id FROM selected_brand)
          AND packets.packet_kind = 'operational_v2'
          AND EXISTS (
              SELECT 1
              FROM b3s_history.evidence_vault_canonical_memory_promotion_events
                   AS events
              WHERE events.brand_id = packets.brand_id
                AND events.adoption_kind = 'operational_v2'
                AND events.candidate_packet_fingerprint =
                    packets.packet_fingerprint
          )
        ORDER BY packets.created_at, packets.id
        LIMIT 129
    ), source_packet_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_canonical_memory_packets AS packets
        WHERE packets.brand_id = (SELECT id FROM selected_brand)
          AND packets.packet_kind IN (
              'operational_source_v2', 'operational_reviewed_v2'
          )
    ), source_packet_rows AS MATERIALIZED (
        SELECT packets.*
        FROM b3s_history.evidence_vault_canonical_memory_packets AS packets
        WHERE packets.brand_id = (SELECT id FROM selected_brand)
          AND packets.packet_kind IN (
              'operational_source_v2', 'operational_reviewed_v2'
          )
        ORDER BY packets.created_at, packets.id
        LIMIT 33
    ), review_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_operational_relation_reviews AS reviews
        WHERE reviews.brand_id = (SELECT id FROM selected_brand)
          AND EXISTS (
              SELECT 1
              FROM b3s_history.evidence_vault_canonical_memory_packets AS packets
              WHERE packets.id = reviews.source_packet_id
                AND packets.brand_id = reviews.brand_id
                AND packets.packet_kind = 'operational_source_v2'
          )
    ), review_rows AS MATERIALIZED (
        SELECT reviews.*
        FROM b3s_history.evidence_vault_operational_relation_reviews AS reviews
        WHERE reviews.brand_id = (SELECT id FROM selected_brand)
          AND EXISTS (
              SELECT 1
              FROM b3s_history.evidence_vault_canonical_memory_packets AS packets
              WHERE packets.id = reviews.source_packet_id
                AND packets.brand_id = reviews.brand_id
                AND packets.packet_kind = 'operational_source_v2'
          )
        ORDER BY reviews.created_at, reviews.id
        LIMIT 65
    ), score_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_canonical_score_evaluations AS scores
        WHERE scores.brand_id = (SELECT id FROM selected_brand)
          AND scores.evaluation_kind = 'operational_v2'
    ), score_rows AS MATERIALIZED (
        SELECT scores.*
        FROM b3s_history.evidence_vault_canonical_score_evaluations AS scores
        WHERE scores.brand_id = (SELECT id FROM selected_brand)
          AND scores.evaluation_kind = 'operational_v2'
        ORDER BY scores.created_at, scores.id
        LIMIT 17
    )
    SELECT jsonb_build_object(
        'schema_version', 'evidence-vault-c7-shadow-readiness-v1',
        'database_time', statement_timestamp(),
        'workspace_slug', p_workspace_slug,
        'brand_match_count', (SELECT total FROM brand_count),
        'brand', (SELECT to_jsonb(brands) FROM selected_brand AS brands),
        'watermark_count', (SELECT total FROM watermark_count),
        'watermarks', COALESCE((
            SELECT jsonb_agg(
                to_jsonb(rows)
                ORDER BY rows.capture_sequence DESC, rows.id DESC
            )
            FROM latest_watermark_rows AS rows
        ), '[]'::jsonb),
        'lineage_binding_count', (SELECT total FROM binding_count),
        'lineage_bindings', COALESCE((
            SELECT jsonb_agg(
                to_jsonb(rows)
                ORDER BY rows.capture_sequence, rows.id
            )
            FROM binding_rows AS rows
        ), '[]'::jsonb),
        'operational_adoption_count', (SELECT total FROM adoption_count),
        'operational_adoptions', COALESCE((
            SELECT jsonb_agg(to_jsonb(rows) ORDER BY rows.sequence, rows.id)
            FROM adoption_rows AS rows
        ), '[]'::jsonb),
        'operational_packet_count',
            (SELECT total FROM operational_packet_count),
        'operational_packets', COALESCE((
            SELECT jsonb_agg(to_jsonb(rows) ORDER BY rows.created_at, rows.id)
            FROM operational_packet_rows AS rows
        ), '[]'::jsonb),
        'source_packet_count', (SELECT total FROM source_packet_count),
        'source_packets', COALESCE((
            SELECT jsonb_agg(to_jsonb(rows) ORDER BY rows.created_at, rows.id)
            FROM source_packet_rows AS rows
        ), '[]'::jsonb),
        'relation_review_count', (SELECT total FROM review_count),
        'relation_reviews', COALESCE((
            SELECT jsonb_agg(to_jsonb(rows) ORDER BY rows.created_at, rows.id)
            FROM review_rows AS rows
        ), '[]'::jsonb),
        'operational_score_count', (SELECT total FROM score_count),
        'operational_scores', COALESCE((
            SELECT jsonb_agg(to_jsonb(rows) ORDER BY rows.created_at, rows.id)
            FROM score_rows AS rows
        ), '[]'::jsonb),
        'overflow', jsonb_build_object(
            'adoptions', (SELECT total FROM adoption_count) > 128,
            'operational_packets',
                (SELECT total FROM operational_packet_count) > 128,
            'source_packets', (SELECT total FROM source_packet_count) > 32,
            'reviews', (SELECT total FROM review_count) > 64,
            'scores', (SELECT total FROM score_count) > 16,
            'bindings', (SELECT total FROM binding_count) > 8
        )
    );
$$;

-- Bounded raw-provenance proof.  A valid verified binding has one binding row,
-- two members/receipts/raw bindings, and a finite disposition journal.  The
-- disposition cap supports a long governance history but fails closed at 65.
CREATE FUNCTION b3s_history.read_evidence_vault_c7_shadow_provenance(
    p_binding_id uuid
)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    WITH binding_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_verified_c7_lineage_bindings AS bindings
        WHERE bindings.id = p_binding_id
    ), binding_rows AS MATERIALIZED (
        SELECT bindings.*
        FROM b3s_history.evidence_vault_verified_c7_lineage_bindings AS bindings
        WHERE bindings.id = p_binding_id
        ORDER BY bindings.id
        LIMIT 2
    ), selected_binding AS MATERIALIZED (
        SELECT rows.*
        FROM binding_rows AS rows
        WHERE (SELECT total FROM binding_count) = 1
    ), member_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
        WHERE members.binding_id = p_binding_id
    ), member_rows AS MATERIALIZED (
        SELECT members.*
        FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
        WHERE members.binding_id = p_binding_id
        ORDER BY members.channel_role, members.id
        LIMIT 3
    ), receipt_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
        JOIN b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
          ON receipts.id = members.receipt_id
        WHERE members.binding_id = p_binding_id
    ), receipt_rows AS MATERIALIZED (
        SELECT receipts.*, members.channel_role AS member_channel_role
        FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
        JOIN b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
          ON receipts.id = members.receipt_id
        WHERE members.binding_id = p_binding_id
        ORDER BY members.channel_role, receipts.id
        LIMIT 3
    ), evidence_binding_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
        JOIN b3s_history.evidence_vault_raw_evidence_bindings AS raw_bindings
          ON raw_bindings.id = members.raw_evidence_binding_id
        WHERE members.binding_id = p_binding_id
    ), evidence_binding_rows AS MATERIALIZED (
        SELECT raw_bindings.*, members.channel_role AS member_channel_role
        FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
        JOIN b3s_history.evidence_vault_raw_evidence_bindings AS raw_bindings
          ON raw_bindings.id = members.raw_evidence_binding_id
        WHERE members.binding_id = p_binding_id
        ORDER BY members.channel_role, raw_bindings.id
        LIMIT 3
    ), disposition_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_raw_provenance_disposition_events AS events
        WHERE events.binding_id = p_binding_id
    ), disposition_rows AS MATERIALIZED (
        SELECT events.*
        FROM b3s_history.evidence_vault_raw_provenance_disposition_events AS events
        WHERE events.binding_id = p_binding_id
        ORDER BY events.event_sequence, events.id
        LIMIT 65
    )
    SELECT jsonb_build_object(
        'binding_count', (SELECT total FROM binding_count),
        'binding', (SELECT to_jsonb(rows) FROM selected_binding AS rows),
        'member_count', (SELECT total FROM member_count),
        'members', COALESCE((
            SELECT jsonb_agg(to_jsonb(rows) ORDER BY rows.channel_role, rows.id)
            FROM member_rows AS rows
        ), '[]'::jsonb),
        'receipt_count', (SELECT total FROM receipt_count),
        'receipts', COALESCE((
            SELECT jsonb_agg(
                to_jsonb(rows) - 'member_channel_role'
                ORDER BY rows.member_channel_role, rows.id
            )
            FROM receipt_rows AS rows
        ), '[]'::jsonb),
        'evidence_binding_count', (SELECT total FROM evidence_binding_count),
        'evidence_bindings', COALESCE((
            SELECT jsonb_agg(
                to_jsonb(rows) - 'member_channel_role'
                ORDER BY rows.member_channel_role, rows.id
            )
            FROM evidence_binding_rows AS rows
        ), '[]'::jsonb),
        'disposition_count', (SELECT total FROM disposition_count),
        'dispositions', COALESCE((
            SELECT jsonb_agg(
                to_jsonb(rows)
                ORDER BY rows.event_sequence, rows.id
            )
            FROM disposition_rows AS rows
        ), '[]'::jsonb),
        'overflow', jsonb_build_object(
            'bindings', (SELECT total FROM binding_count) > 1,
            'members', (SELECT total FROM member_count) > 2,
            'receipts', (SELECT total FROM receipt_count) > 2,
            'evidence_bindings',
                (SELECT total FROM evidence_binding_count) > 2,
            'dispositions', (SELECT total FROM disposition_count) > 64
        )
    );
$$;

-- Bounded complete raw-acquisition proof.  Parent/plan/watermark selectors use
-- expected-one + one rows; evidence, receipts, and raw bindings use
-- expected-two + one rows.  Counts are exact and overflow is explicit.
CREATE FUNCTION b3s_history.read_evidence_vault_c7_shadow_acquisition(
    p_workspace_slug text,
    p_source_scan_id text
)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    WITH parent_match_count AS (
        SELECT count(*) AS total
        FROM b3s_history.workspaces AS workspaces
        JOIN b3s_history.scan_runs AS scans
          ON scans.workspace_id = workspaces.id
        JOIN b3s_history.brands AS brands
          ON brands.id = scans.brand_id
         AND brands.workspace_id = workspaces.id
        JOIN b3s_history.captures AS captures
          ON captures.scan_run_id = scans.id
         AND captures.brand_id = brands.id
        WHERE workspaces.slug = p_workspace_slug
          AND scans.source_scan_id = p_source_scan_id
          AND EXISTS (
              SELECT 1
              FROM b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
              WHERE receipts.workspace_id = workspaces.id
                AND receipts.scan_run_id = scans.id
                AND receipts.capture_id = captures.id
          )
    ), parent_rows AS MATERIALIZED (
        SELECT
            to_jsonb(workspaces) AS workspace,
            to_jsonb(brands) AS brand,
            to_jsonb(scans) AS scan_run,
            to_jsonb(captures) AS capture,
            workspaces.id AS workspace_id,
            brands.id AS brand_id,
            scans.id AS scan_run_id,
            captures.id AS capture_id
        FROM b3s_history.workspaces AS workspaces
        JOIN b3s_history.scan_runs AS scans
          ON scans.workspace_id = workspaces.id
        JOIN b3s_history.brands AS brands
          ON brands.id = scans.brand_id
         AND brands.workspace_id = workspaces.id
        JOIN b3s_history.captures AS captures
          ON captures.scan_run_id = scans.id
         AND captures.brand_id = brands.id
        WHERE workspaces.slug = p_workspace_slug
          AND scans.source_scan_id = p_source_scan_id
          AND EXISTS (
              SELECT 1
              FROM b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
              WHERE receipts.workspace_id = workspaces.id
                AND receipts.scan_run_id = scans.id
                AND receipts.capture_id = captures.id
          )
        ORDER BY scans.id, captures.id
        LIMIT 2
    ), selected_parent AS MATERIALIZED (
        SELECT rows.*
        FROM parent_rows AS rows
        WHERE (SELECT total FROM parent_match_count) = 1
    ), operation_plan_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_operation_plans AS plans
        WHERE plans.workspace_id = (SELECT workspace_id FROM selected_parent)
          AND plans.brand_id = (SELECT brand_id FROM selected_parent)
          AND plans.scan_run_id = (SELECT scan_run_id FROM selected_parent)
    ), operation_plan_rows AS MATERIALIZED (
        SELECT plans.*
        FROM b3s_history.evidence_vault_operation_plans AS plans
        WHERE plans.workspace_id = (SELECT workspace_id FROM selected_parent)
          AND plans.brand_id = (SELECT brand_id FROM selected_parent)
          AND plans.scan_run_id = (SELECT scan_run_id FROM selected_parent)
        ORDER BY plans.id
        LIMIT 2
    ), selected_operation_plan AS MATERIALIZED (
        SELECT rows.*
        FROM operation_plan_rows AS rows
        WHERE (SELECT total FROM operation_plan_count) = 1
    ), evidence_record_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_records AS evidence
        WHERE evidence.capture_id = (SELECT capture_id FROM selected_parent)
    ), evidence_record_rows AS MATERIALIZED (
        SELECT evidence.*
        FROM b3s_history.evidence_records AS evidence
        WHERE evidence.capture_id = (SELECT capture_id FROM selected_parent)
        ORDER BY evidence.evidence_ref, evidence.id
        LIMIT 3
    ), watermark_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_capture_watermark_events AS watermarks
        WHERE watermarks.brand_id = (SELECT brand_id FROM selected_parent)
          AND watermarks.capture_id = (SELECT capture_id FROM selected_parent)
    ), watermark_rows AS MATERIALIZED (
        SELECT watermarks.*
        FROM b3s_history.evidence_vault_capture_watermark_events AS watermarks
        WHERE watermarks.brand_id = (SELECT brand_id FROM selected_parent)
          AND watermarks.capture_id = (SELECT capture_id FROM selected_parent)
        ORDER BY watermarks.id
        LIMIT 2
    ), selected_watermark AS MATERIALIZED (
        SELECT rows.*
        FROM watermark_rows AS rows
        WHERE (SELECT total FROM watermark_count) = 1
    ), receipt_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
        WHERE receipts.workspace_id = (SELECT workspace_id FROM selected_parent)
          AND receipts.scan_run_id = (SELECT scan_run_id FROM selected_parent)
          AND receipts.capture_id = (SELECT capture_id FROM selected_parent)
    ), receipt_rows AS MATERIALIZED (
        SELECT receipts.*
        FROM b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
        WHERE receipts.workspace_id = (SELECT workspace_id FROM selected_parent)
          AND receipts.scan_run_id = (SELECT scan_run_id FROM selected_parent)
          AND receipts.capture_id = (SELECT capture_id FROM selected_parent)
        ORDER BY receipts.channel_role, receipts.id
        LIMIT 3
    ), evidence_binding_count AS (
        SELECT count(*) AS total
        FROM b3s_history.evidence_vault_raw_evidence_bindings AS raw_bindings
        WHERE raw_bindings.workspace_id =
                (SELECT workspace_id FROM selected_parent)
          AND raw_bindings.scan_run_id =
                (SELECT scan_run_id FROM selected_parent)
          AND raw_bindings.capture_id =
                (SELECT capture_id FROM selected_parent)
    ), evidence_binding_rows AS MATERIALIZED (
        SELECT raw_bindings.*
        FROM b3s_history.evidence_vault_raw_evidence_bindings AS raw_bindings
        WHERE raw_bindings.workspace_id =
                (SELECT workspace_id FROM selected_parent)
          AND raw_bindings.scan_run_id =
                (SELECT scan_run_id FROM selected_parent)
          AND raw_bindings.capture_id =
                (SELECT capture_id FROM selected_parent)
        ORDER BY raw_bindings.channel_role, raw_bindings.id
        LIMIT 3
    )
    SELECT jsonb_build_object(
        'parent_match_count', (SELECT total FROM parent_match_count),
        'workspace', (SELECT workspace FROM selected_parent),
        'brand', (SELECT brand FROM selected_parent),
        'scan_run', (SELECT scan_run FROM selected_parent),
        'operation_plan_count', (SELECT total FROM operation_plan_count),
        'operation_plan', (
            SELECT to_jsonb(rows) FROM selected_operation_plan AS rows
        ),
        'capture', (SELECT capture FROM selected_parent),
        'evidence_record_count', (SELECT total FROM evidence_record_count),
        'evidence_records', COALESCE((
            SELECT jsonb_agg(
                to_jsonb(rows)
                ORDER BY rows.evidence_ref, rows.id
            )
            FROM evidence_record_rows AS rows
        ), '[]'::jsonb),
        'watermark_count', (SELECT total FROM watermark_count),
        'watermark_event', (
            SELECT to_jsonb(rows) FROM selected_watermark AS rows
        ),
        'receipt_count', (SELECT total FROM receipt_count),
        'receipts', COALESCE((
            SELECT jsonb_agg(
                to_jsonb(rows)
                ORDER BY rows.channel_role, rows.id
            )
            FROM receipt_rows AS rows
        ), '[]'::jsonb),
        'evidence_binding_count', (SELECT total FROM evidence_binding_count),
        'evidence_bindings', COALESCE((
            SELECT jsonb_agg(
                to_jsonb(rows)
                ORDER BY rows.channel_role, rows.id
            )
            FROM evidence_binding_rows AS rows
        ), '[]'::jsonb),
        'overflow', jsonb_build_object(
            'parents', (SELECT total FROM parent_match_count) > 1,
            'operation_plans', (SELECT total FROM operation_plan_count) > 1,
            'evidence_records', (SELECT total FROM evidence_record_count) > 2,
            'watermarks', (SELECT total FROM watermark_count) > 1,
            'receipts', (SELECT total FROM receipt_count) > 2,
            'evidence_bindings',
                (SELECT total FROM evidence_binding_count) > 2
        )
    );
$$;

-- PostgreSQL grants EXECUTE to PUBLIC at function creation.  Close that window
-- inside this migration transaction before transferring fixed ownership.
REVOKE ALL ON FUNCTION
    b3s_history.read_evidence_vault_c7_shadow_context(text, text)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    b3s_history.read_evidence_vault_c7_shadow_provenance(uuid)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    b3s_history.read_evidence_vault_c7_shadow_acquisition(text, text)
    FROM PUBLIC;

DO $$
DECLARE
    function_signature text;
    actual_owner text;
BEGIN
    GRANT USAGE, CREATE ON SCHEMA b3s_history
        TO b3s_history_vault_provenance_owner;

    FOREACH function_signature IN ARRAY ARRAY[
        'b3s_history.read_evidence_vault_c7_shadow_context(text, text)',
        'b3s_history.read_evidence_vault_c7_shadow_provenance(uuid)',
        'b3s_history.read_evidence_vault_c7_shadow_acquisition(text, text)'
    ] LOOP
        EXECUTE pg_catalog.format(
            'ALTER FUNCTION %s OWNER TO b3s_history_vault_provenance_owner',
            function_signature
        );
    END LOOP;

    REVOKE CREATE ON SCHEMA b3s_history
        FROM b3s_history_vault_provenance_owner;
    IF pg_catalog.has_schema_privilege(
        'b3s_history_vault_provenance_owner', 'b3s_history', 'CREATE'
    ) THEN
        RAISE EXCEPTION
            'provenance owner retains forbidden schema CREATE';
    END IF;

    FOREACH function_signature IN ARRAY ARRAY[
        'b3s_history.read_evidence_vault_c7_shadow_context(text, text)',
        'b3s_history.read_evidence_vault_c7_shadow_provenance(uuid)',
        'b3s_history.read_evidence_vault_c7_shadow_acquisition(text, text)'
    ] LOOP
        SELECT roles.rolname
        INTO actual_owner
        FROM pg_catalog.pg_proc AS functions
        JOIN pg_catalog.pg_roles AS roles
          ON roles.oid = functions.proowner
        WHERE functions.oid = function_signature::pg_catalog.regprocedure
          AND functions.prosecdef
          AND functions.provolatile = 's'
          AND functions.proconfig @> ARRAY['search_path=pg_catalog']::text[];
        IF actual_owner IS DISTINCT FROM
                'b3s_history_vault_provenance_owner' THEN
            RAISE EXCEPTION
                'bounded SECURITY DEFINER % has unsafe owner or configuration',
                function_signature;
        END IF;
    END LOOP;
END;
$$;

GRANT USAGE ON SCHEMA b3s_history
    TO b3s_history_vault_runtime_read;
GRANT EXECUTE ON FUNCTION
    b3s_history.read_evidence_vault_c7_shadow_context(text, text),
    b3s_history.read_evidence_vault_c7_shadow_provenance(uuid),
    b3s_history.read_evidence_vault_c7_shadow_acquisition(text, text)
    TO b3s_history_vault_runtime_read;

-- Final capability proof: the group owns nothing, inherits nothing, can touch
-- no relation or sequence, cannot create schema objects, and may execute only
-- the three new bounded SECURITY DEFINER reads.
DO $$
DECLARE
    runtime_role record;
    required_signature text;
BEGIN
    SELECT *
    INTO runtime_role
    FROM pg_catalog.pg_roles
    WHERE rolname = 'b3s_history_vault_runtime_read';

    IF runtime_role.oid IS NULL
       OR runtime_role.rolcanlogin
       OR runtime_role.rolinherit
       OR runtime_role.rolsuper
       OR runtime_role.rolcreaterole
       OR runtime_role.rolcreatedb
       OR runtime_role.rolreplication
       OR runtime_role.rolbypassrls THEN
        RAISE EXCEPTION
            'b3s_history_vault_runtime_read final attributes are unsafe';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS memberships
        WHERE memberships.member = runtime_role.oid
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_runtime_read final membership is unsafe';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_shdepend AS dependencies
        WHERE dependencies.refclassid =
                'pg_catalog.pg_authid'::pg_catalog.regclass
          AND dependencies.refobjid = runtime_role.oid
          AND dependencies.deptype = 'o'
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_runtime_read unexpectedly owns objects';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
        'b3s_history_vault_runtime_read', 'b3s_history', 'USAGE'
    ) OR pg_catalog.has_schema_privilege(
        'b3s_history_vault_runtime_read', 'b3s_history', 'CREATE'
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_runtime_read schema scope is unsafe';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS relations
        JOIN pg_catalog.pg_namespace AS schemas
          ON schemas.oid = relations.relnamespace
        WHERE schemas.nspname = 'b3s_history'
          AND relations.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (
              pg_catalog.has_table_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'SELECT'
              ) OR pg_catalog.has_table_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'INSERT'
              ) OR pg_catalog.has_table_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'UPDATE'
              ) OR pg_catalog.has_table_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'DELETE'
              ) OR pg_catalog.has_table_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'TRUNCATE'
              ) OR pg_catalog.has_table_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'REFERENCES'
              ) OR pg_catalog.has_table_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'TRIGGER'
              ) OR pg_catalog.has_any_column_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'SELECT'
              ) OR pg_catalog.has_any_column_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'INSERT'
              ) OR pg_catalog.has_any_column_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'UPDATE'
              ) OR pg_catalog.has_any_column_privilege(
                  'b3s_history_vault_runtime_read', relations.oid, 'REFERENCES'
              )
          )
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_runtime_read final table scope is unsafe';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS sequences
        JOIN pg_catalog.pg_namespace AS schemas
          ON schemas.oid = sequences.relnamespace
        WHERE schemas.nspname = 'b3s_history'
          AND sequences.relkind = 'S'
          AND (
              pg_catalog.has_sequence_privilege(
                  'b3s_history_vault_runtime_read', sequences.oid, 'USAGE'
              ) OR pg_catalog.has_sequence_privilege(
                  'b3s_history_vault_runtime_read', sequences.oid, 'SELECT'
              ) OR pg_catalog.has_sequence_privilege(
                  'b3s_history_vault_runtime_read', sequences.oid, 'UPDATE'
              )
          )
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_runtime_read final sequence scope is unsafe';
    END IF;

    FOREACH required_signature IN ARRAY ARRAY[
        'b3s_history.read_evidence_vault_c7_shadow_context(text, text)',
        'b3s_history.read_evidence_vault_c7_shadow_provenance(uuid)',
        'b3s_history.read_evidence_vault_c7_shadow_acquisition(text, text)'
    ] LOOP
        IF NOT pg_catalog.has_function_privilege(
            'b3s_history_vault_runtime_read',
            required_signature::pg_catalog.regprocedure,
            'EXECUTE'
        ) THEN
            RAISE EXCEPTION
                'b3s_history_vault_runtime_read lacks required bounded EXECUTE %',
                required_signature;
        END IF;
        IF pg_catalog.has_function_privilege(
            'public',
            required_signature::pg_catalog.regprocedure,
            'EXECUTE'
        ) THEN
            RAISE EXCEPTION
                'PUBLIC retains forbidden bounded EXECUTE %',
                required_signature;
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS functions
        WHERE functions.prosecdef
          AND pg_catalog.has_function_privilege(
              'b3s_history_vault_runtime_read', functions.oid, 'EXECUTE'
          )
          AND NOT (
              functions.oid = ANY(ARRAY[
                  'b3s_history.read_evidence_vault_c7_shadow_context(text, text)'::pg_catalog.regprocedure::oid,
                  'b3s_history.read_evidence_vault_c7_shadow_provenance(uuid)'::pg_catalog.regprocedure::oid,
                  'b3s_history.read_evidence_vault_c7_shadow_acquisition(text, text)'::pg_catalog.regprocedure::oid
              ])
          )
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_runtime_read has unexpected SECURITY DEFINER EXECUTE';
    END IF;
END;
$$;

COMMENT ON FUNCTION
    b3s_history.read_evidence_vault_c7_shadow_context(text, text) IS
    'Bounded atomic brand context for read-only C7 shadow readiness; never grants C7 authority.';
COMMENT ON FUNCTION
    b3s_history.read_evidence_vault_c7_shadow_provenance(uuid) IS
    'Bounded exact-count verified provenance projection with explicit fail-closed overflow.';
COMMENT ON FUNCTION
    b3s_history.read_evidence_vault_c7_shadow_acquisition(text, text) IS
    'Bounded exact-count raw acquisition projection with explicit fail-closed overflow.';
