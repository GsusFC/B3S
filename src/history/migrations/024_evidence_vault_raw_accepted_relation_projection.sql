-- Project active accepted evidence relations into fresh incremental planning.
--
-- Migration 022 is immutable. This cumulative replacement preserves its lock,
-- replay, bounds, and ACL contract while adding exact durable relation identity.
--
-- The scanner login remains execute-only.  This bounded SECURITY DEFINER
-- projection is called inside the same transaction as the immutable raw append;
-- it acquires the append lock order before reading any planning input.

-- Replace the owner-bound SECURITY DEFINER function under its fixed NOLOGIN
-- owner. The migration actor must have the existing SET-role capability.
GRANT USAGE, CREATE ON SCHEMA b3s_history
TO b3s_history_vault_provenance_owner;
GRANT SELECT ON
    b3s_history.evidence_vault_canonical_memory_promotion_events,
    b3s_history.evidence_vault_verified_c7_lineage_members,
    b3s_history.evidence_vault_operational_source_capture_lineage_members
TO b3s_history_vault_provenance_owner;
SET LOCAL ROLE b3s_history_vault_provenance_owner;

CREATE OR REPLACE FUNCTION b3s_history.read_evidence_vault_raw_planning_context(
    p_workspace_slug text,
    p_source_scan_id text,
    p_canonical_domain text,
    p_workspace_id uuid,
    p_brand_id uuid,
    p_scan_run_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    workspace_row b3s_history.workspaces%ROWTYPE;
    brand_row b3s_history.brands%ROWTYPE;
    latest_event b3s_history.evidence_vault_canonical_memory_promotion_events%ROWTYPE;
    latest_packet b3s_history.evidence_vault_canonical_memory_packets%ROWTYPE;
    capture_ids uuid[] := ARRAY[]::uuid[];
    lineage_capture_ids uuid[] := ARRAY[]::uuid[];
    previous_capture_id uuid;
    evidence_count bigint := 0;
    evidence_bytes bigint := 0;
    canonical_version text;
    existing_plan jsonb;
    authority_projection jsonb;
    authority_adoption_count bigint := 0;
    authority_packet_count bigint := 0;
    authority_serialized_bytes bigint := 0;
    previous_records jsonb := '[]'::jsonb;
    known_records jsonb := '[]'::jsonb;
    accepted_relations jsonb := '[]'::jsonb;
    accepted_basis_count bigint := 0;
    resolved_basis_count bigint := 0;
BEGIN
    IF p_workspace_slug IS NULL
       OR p_workspace_slug !~ '^[a-z0-9][a-z0-9_-]{0,62}$'
       OR p_source_scan_id IS NULL
       OR length(p_source_scan_id) NOT BETWEEN 1 AND 256
       OR p_source_scan_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$'
       OR p_canonical_domain IS NULL
       OR length(p_canonical_domain) NOT BETWEEN 3 AND 253
       OR p_canonical_domain <> lower(p_canonical_domain)
       OR position('.' in p_canonical_domain) = 0
       OR p_workspace_id IS NULL
       OR p_brand_id IS NULL
       OR p_scan_run_id IS NULL THEN
        RAISE EXCEPTION 'raw planning context identity is invalid';
    END IF;

    -- Same global order as the raw append. Re-acquisition by this transaction is
    -- intentional when append_evidence_vault_raw_acquisition() is called later.
    PERFORM pg_advisory_xact_lock(
        b3s_history.evidence_vault_ingest_lock_key(
            p_workspace_id, p_source_scan_id
        )
    );
    PERFORM pg_advisory_xact_lock(
        b3s_history.evidence_vault_brand_lock_key(
            p_workspace_id, p_brand_id
        )
    );
    -- Match the operational repository's canonical-promotion lock exactly.
    PERFORM pg_advisory_xact_lock((
        'x' || substr(
            encode(
                sha256(convert_to(
                    p_brand_id::text || ':evidence-vault-canonical-promotion',
                    'UTF8'
                )),
                'hex'
            ),
            1,
            16
        )
    )::bit(64)::bigint);

    SELECT * INTO workspace_row
    FROM b3s_history.workspaces AS workspaces
    WHERE workspaces.slug = p_workspace_slug;
    IF workspace_row.id IS NOT NULL
       AND workspace_row.id IS DISTINCT FROM p_workspace_id THEN
        RAISE EXCEPTION 'raw planning workspace identity diverges';
    END IF;
    IF EXISTS (
        SELECT 1 FROM b3s_history.workspaces AS workspaces
        WHERE workspaces.id = p_workspace_id
          AND workspaces.slug IS DISTINCT FROM p_workspace_slug
    ) THEN
        RAISE EXCEPTION 'raw planning workspace id is already bound';
    END IF;

    SELECT * INTO brand_row
    FROM b3s_history.brands AS brands
    WHERE brands.workspace_id = p_workspace_id
      AND brands.canonical_domain = p_canonical_domain;
    IF brand_row.id IS NOT NULL
       AND brand_row.id IS DISTINCT FROM p_brand_id THEN
        RAISE EXCEPTION 'raw planning brand identity diverges';
    END IF;
    IF EXISTS (
        SELECT 1 FROM b3s_history.brands AS brands
        WHERE brands.id = p_brand_id
          AND (
              brands.workspace_id IS DISTINCT FROM p_workspace_id
              OR brands.canonical_domain IS DISTINCT FROM p_canonical_domain
          )
    ) THEN
        RAISE EXCEPTION 'raw planning brand id is already bound';
    END IF;
    IF EXISTS (
        SELECT 1 FROM b3s_history.scan_runs AS scans
        WHERE scans.id = p_scan_run_id
          AND (
              scans.workspace_id IS DISTINCT FROM p_workspace_id
              OR scans.brand_id IS DISTINCT FROM p_brand_id
              OR scans.source_scan_id IS DISTINCT FROM p_source_scan_id
          )
    ) OR EXISTS (
        SELECT 1 FROM b3s_history.scan_runs AS scans
        WHERE scans.workspace_id = p_workspace_id
          AND scans.source_scan_id = p_source_scan_id
          AND (
              scans.id IS DISTINCT FROM p_scan_run_id
              OR scans.brand_id IS DISTINCT FROM p_brand_id
          )
    ) THEN
        RAISE EXCEPTION 'raw planning scan identity is already bound';
    END IF;

    SELECT plans.plan_payload INTO existing_plan
    FROM b3s_history.evidence_vault_operation_plans AS plans
    JOIN b3s_history.scan_runs AS scans
      ON scans.id = plans.scan_run_id
     AND scans.workspace_id = plans.workspace_id
     AND scans.brand_id = plans.brand_id
    WHERE plans.workspace_id = p_workspace_id
      AND plans.brand_id = p_brand_id
      AND scans.id = p_scan_run_id
      AND scans.source_scan_id = p_source_scan_id;

    IF existing_plan IS NOT NULL THEN
        IF pg_column_size(existing_plan) > 1048576 THEN
            RAISE EXCEPTION 'raw planning replay plan exceeds bound';
        END IF;
        -- Exact replay must remain independent of later canonical/history growth.
        -- The immutable append/readback path below will revalidate this stored
        -- plan against the exact signed acquisition and observation.
        RETURN jsonb_build_object(
            'schema_version', 'evidence-vault-raw-planning-context-v1',
            'workspace_slug', p_workspace_slug,
            'source_scan_id', p_source_scan_id,
            'canonical_domain', p_canonical_domain,
            'canonical_memory_version', NULL,
            'previous_capture_evidence_records', '[]'::jsonb,
            'known_evidence_records', '[]'::jsonb,
            'accepted_evidence_tile_relations', '[]'::jsonb,
            'operational_authority_context', jsonb_build_object(
                'database_time', clock_timestamp(),
                'operational_adoption_count', 0,
                'operational_adoptions', '[]'::jsonb,
                'operational_packet_count', 0,
                'operational_packets', '[]'::jsonb
            ),
            'existing_operation_plan', existing_plan
        );
    END IF;

    IF brand_row.id IS NOT NULL THEN
        -- Freeze only the operational authority rows needed by the existing
        -- Python `_current_memory` validator.  Count and size preflights run
        -- before jsonb_agg so unrelated C7/source/review data is never touched.
        SELECT count(*), COALESCE(sum(pg_column_size(to_jsonb(events))), 0)
        INTO authority_adoption_count, authority_serialized_bytes
        FROM b3s_history.evidence_vault_canonical_memory_promotion_events
             AS events
        WHERE events.brand_id = p_brand_id
          AND events.adoption_kind = 'operational_v2';
        IF authority_adoption_count > 128
           OR authority_serialized_bytes > 33554432 THEN
            RAISE EXCEPTION 'raw planning operational adoption chain exceeds bound';
        END IF;

        SELECT count(*),
               authority_serialized_bytes
                 + COALESCE(sum(pg_column_size(to_jsonb(packets))), 0)
        INTO authority_packet_count, authority_serialized_bytes
        FROM b3s_history.evidence_vault_canonical_memory_packets AS packets
        WHERE packets.brand_id = p_brand_id
          AND packets.packet_kind = 'operational_v2'
          AND EXISTS (
              SELECT 1
              FROM b3s_history.evidence_vault_canonical_memory_promotion_events
                   AS events
              WHERE events.brand_id = packets.brand_id
                AND events.adoption_kind = 'operational_v2'
                AND events.candidate_packet_fingerprint =
                    packets.packet_fingerprint
          );
        IF authority_packet_count > 128
           OR authority_serialized_bytes > 67108864 THEN
            RAISE EXCEPTION 'raw planning operational authority exceeds bound';
        END IF;

        authority_projection := jsonb_build_object(
            'database_time', clock_timestamp(),
            'operational_adoption_count', authority_adoption_count,
            'operational_adoptions', COALESCE((
                SELECT jsonb_agg(to_jsonb(events) ORDER BY events.sequence)
                FROM b3s_history.evidence_vault_canonical_memory_promotion_events
                     AS events
                WHERE events.brand_id = p_brand_id
                  AND events.adoption_kind = 'operational_v2'
            ), '[]'::jsonb),
            'operational_packet_count', authority_packet_count,
            'operational_packets', COALESCE((
                SELECT jsonb_agg(to_jsonb(packets)
                                 ORDER BY packets.created_at, packets.id)
                FROM b3s_history.evidence_vault_canonical_memory_packets
                     AS packets
                WHERE packets.brand_id = p_brand_id
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
            ), '[]'::jsonb)
        );
        IF pg_column_size(authority_projection) > 67108864 THEN
            RAISE EXCEPTION 'raw planning serialized authority exceeds bound';
        END IF;
        SELECT * INTO latest_event
        FROM b3s_history.evidence_vault_canonical_memory_promotion_events AS events
        WHERE events.brand_id = p_brand_id
          AND events.adoption_kind = 'operational_v2'
        ORDER BY events.sequence DESC
        LIMIT 1;
        IF latest_event.id IS NOT NULL THEN
            canonical_version := latest_event.event_payload ->>
                'promoted_canonical_memory_version';
            IF canonical_version IS NULL
               OR canonical_version !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'raw planning canonical version is invalid';
            END IF;
            SELECT * INTO latest_packet
            FROM b3s_history.evidence_vault_canonical_memory_packets AS packets
            WHERE packets.brand_id = p_brand_id
              AND packets.packet_fingerprint =
                    latest_event.candidate_packet_fingerprint
              AND packets.packet_kind = 'operational_v2';
            IF latest_packet.id IS NULL
               OR latest_packet.packet_payload ->>
                    'proposed_canonical_memory_version'
                    IS DISTINCT FROM canonical_version THEN
                RAISE EXCEPTION 'raw planning canonical packet is invalid';
            END IF;
        END IF;

        SELECT COALESCE(array_agg(history.id ORDER BY history.position), ARRAY[]::uuid[])
        INTO capture_ids
        FROM (
            SELECT captures.id,
                   row_number() OVER (
                       ORDER BY captures.observed_at DESC,
                                captures.recorded_at DESC,
                                captures.id DESC
                   ) AS position
            FROM b3s_history.captures AS captures
            JOIN b3s_history.scan_runs AS scans
              ON scans.id = captures.scan_run_id
             AND scans.brand_id = captures.brand_id
            WHERE scans.workspace_id = p_workspace_id
              AND scans.brand_id = p_brand_id
              AND scans.source_scan_id <> p_source_scan_id
            ORDER BY captures.observed_at DESC,
                     captures.recorded_at DESC,
                     captures.id DESC
            LIMIT 500
        ) AS history;
        previous_capture_id := capture_ids[1];

        -- Active C7 lineage may be older than the rolling 500-capture window.
        -- Add only capture ids whose exact four-part identity is still present
        -- in the accepted head, so its two durable members remain comparable.
        WITH accepted_c7_basis AS (
            SELECT basis.value ->> 'relation_id' AS relation_id,
                   basis.value ->> 'evidence_id' AS evidence_id,
                   basis.value ->> 'source_identity_id' AS source_identity_id
            FROM jsonb_array_elements(COALESCE(
                latest_packet.packet_payload #>
                    '{accepted_memory,accepted_tiles}',
                '[]'::jsonb
            )) AS tiles(value)
            CROSS JOIN LATERAL jsonb_array_elements(COALESCE(
                tiles.value -> 'basis', '[]'::jsonb
            )) AS basis(value)
            WHERE tiles.value ->> 'tile_id' = 'C7'
        ), mapped_captures AS (
            SELECT members.capture_id
            FROM b3s_history.evidence_vault_verified_c7_lineage_members
                 AS members
            JOIN accepted_c7_basis AS basis
              ON basis.relation_id = members.relation_id
             AND basis.evidence_id = members.evidence_id
             AND basis.source_identity_id = members.source_identity_id
            WHERE members.brand_id = p_brand_id
            UNION
            SELECT members.capture_id
            FROM b3s_history.evidence_vault_operational_source_capture_lineage_members
                 AS members
            JOIN accepted_c7_basis AS basis
              ON basis.relation_id = members.relation_id
             AND basis.evidence_id = members.evidence_id
             AND basis.source_identity_id = members.source_identity_id
            WHERE members.brand_id = p_brand_id
        )
        SELECT COALESCE(array_agg(capture_id ORDER BY capture_id), ARRAY[]::uuid[])
        INTO lineage_capture_ids
        FROM mapped_captures;
        capture_ids := capture_ids || lineage_capture_ids;

        -- Bound every projected field before jsonb_agg materializes history.
        -- This aggregate holds only counters; oversized direct-DML metadata or
        -- text therefore fails before it can become one large definer result.
        SELECT count(*), COALESCE(sum(
                   octet_length(COALESCE(
                       evidence.content_raw,
                       convert_to(evidence.content, 'UTF8')
                   ))
                   + octet_length(COALESCE(evidence.metadata, '{}'::jsonb)::text)
                   + octet_length(evidence.evidence_ref)
                   + octet_length(evidence.source)
                   + octet_length(evidence.source_class)
                   + octet_length(evidence.evidence_type)
                   + octet_length(evidence.url)
                   + octet_length(evidence.confidence)
                   + 512
               ), 0)
        INTO evidence_count, evidence_bytes
        FROM b3s_history.evidence_records AS evidence
        WHERE evidence.capture_id = ANY(capture_ids);
        -- The previous capture may also appear in the known set, so its
        -- serialized row can occur twice.  A 32 MiB input ceiling therefore
        -- bounds the pre-aggregation projection to at most 64 MiB.
        IF evidence_count > 5000 OR evidence_bytes > 33554432 THEN
            RAISE EXCEPTION 'raw planning evidence context exceeds bound';
        END IF;

        IF previous_capture_id IS NOT NULL THEN
            SELECT COALESCE(jsonb_agg(
                jsonb_build_object(
                    'ref', evidence.evidence_ref,
                    'source', evidence.source,
                    'evidence_type', evidence.evidence_type,
                    'url', evidence.url,
                    'content', CASE
                        WHEN evidence.content_raw IS NOT NULL
                        THEN convert_from(evidence.content_raw, 'UTF8')
                        ELSE evidence.content
                    END,
                    'confidence', evidence.confidence,
                    'metadata', COALESCE(evidence.metadata, '{}'::jsonb) ||
                        jsonb_build_object('source_class', evidence.source_class)
                ) ORDER BY evidence.evidence_ref, evidence.id
            ), '[]'::jsonb)
            INTO previous_records
            FROM b3s_history.evidence_records AS evidence
            WHERE evidence.capture_id = previous_capture_id;
        END IF;

        SELECT COALESCE(jsonb_agg(rows.payload ORDER BY rows.capture_position,
                                                    rows.evidence_ref,
                                                    rows.evidence_id), '[]'::jsonb)
        INTO known_records
        FROM (
            SELECT array_position(capture_ids, captures.id) AS capture_position,
                   evidence.evidence_ref,
                   evidence.id AS evidence_id,
                   jsonb_build_object(
                       'ref', evidence.evidence_ref,
                       'source', evidence.source,
                       'evidence_type', evidence.evidence_type,
                       'url', evidence.url,
                       'content', CASE
                           WHEN evidence.content_raw IS NOT NULL
                           THEN convert_from(evidence.content_raw, 'UTF8')
                           ELSE evidence.content
                       END,
                       'confidence', evidence.confidence,
                       'metadata', COALESCE(evidence.metadata, '{}'::jsonb) ||
                           jsonb_build_object('source_class', evidence.source_class)
                   ) AS payload
            FROM b3s_history.captures AS captures
            JOIN b3s_history.scan_runs AS scans
              ON scans.id = captures.scan_run_id
             AND scans.brand_id = captures.brand_id
            LEFT JOIN b3s_history.evidence_vault_operation_plans AS plans
              ON plans.workspace_id = scans.workspace_id
             AND plans.brand_id = scans.brand_id
             AND plans.scan_run_id = scans.id
            JOIN b3s_history.evidence_records AS evidence
              ON evidence.capture_id = captures.id
            WHERE captures.id = ANY(capture_ids)
              AND CASE
                    WHEN plans.id IS NOT NULL
                    THEN plans.status IN ('completed', 'not_required')
                    ELSE scans.metadata ->> 'analysis_status'
                            IN ('completed', 'not_required')
                      OR COALESCE(scans.metadata ->> 'report_hash', '') <> ''
                      OR scans.metadata ->> 'imported_from' = 'b3s-report-json'
                      OR scans.metadata ->> 'persisted_as' = 'capture_with_report'
                  END
        ) AS rows;

        IF COALESCE(pg_column_size(previous_records), 0)
             + COALESCE(pg_column_size(known_records), 0) > 67108864 THEN
            RAISE EXCEPTION 'raw planning serialized evidence exceeds bound';
        END IF;

        -- Translate the active accepted memory basis through immutable exact
        -- capture-lineage rows. Operational evidence_id values are deliberately
        -- never treated as capture fingerprints.
        IF latest_packet.id IS NOT NULL THEN
            WITH accepted_basis AS (
                SELECT tiles.value ->> 'tile_id' AS tile_id,
                       basis.value ->> 'relation_id' AS relation_id,
                       basis.value ->> 'evidence_id' AS evidence_id,
                       basis.value ->> 'source_identity_id' AS source_identity_id
                FROM jsonb_array_elements(COALESCE(
                    latest_packet.packet_payload #>
                        '{accepted_memory,accepted_tiles}',
                    '[]'::jsonb
                )) AS tiles(value)
                CROSS JOIN LATERAL jsonb_array_elements(COALESCE(
                    tiles.value -> 'basis', '[]'::jsonb
                )) AS basis(value)
            )
            SELECT count(*) INTO accepted_basis_count FROM accepted_basis;
            IF accepted_basis_count > 5000 THEN
                RAISE EXCEPTION 'raw planning accepted basis exceeds bound';
            END IF;

            WITH accepted_basis AS (
                SELECT tiles.value ->> 'tile_id' AS tile_id,
                       basis.value ->> 'relation_id' AS relation_id,
                       basis.value ->> 'evidence_id' AS evidence_id,
                       basis.value ->> 'source_identity_id' AS source_identity_id
                FROM jsonb_array_elements(COALESCE(
                    latest_packet.packet_payload #>
                        '{accepted_memory,accepted_tiles}',
                    '[]'::jsonb
                )) AS tiles(value)
                CROSS JOIN LATERAL jsonb_array_elements(COALESCE(
                    tiles.value -> 'basis', '[]'::jsonb
                )) AS basis(value)
            ), exact_mappings AS (
                SELECT 'C7'::text AS tile_id, members.relation_id,
                       members.evidence_id, members.source_identity_id,
                       members.evidence_fingerprint
                FROM b3s_history.evidence_vault_verified_c7_lineage_members
                     AS members
                WHERE members.brand_id = p_brand_id
                  AND members.capture_id = ANY(capture_ids)
                UNION ALL
                SELECT 'C7'::text AS tile_id, members.relation_id,
                       members.evidence_id, members.source_identity_id,
                       members.evidence_fingerprint
                FROM b3s_history.evidence_vault_operational_source_capture_lineage_members
                     AS members
                WHERE members.brand_id = p_brand_id
                  AND members.capture_id = ANY(capture_ids)
            ), resolved AS (
                SELECT basis.tile_id,
                       min(mappings.evidence_fingerprint) AS evidence_fingerprint
                FROM accepted_basis AS basis
                JOIN exact_mappings AS mappings
                  ON mappings.tile_id = basis.tile_id
                 AND mappings.relation_id = basis.relation_id
                 AND mappings.evidence_id = basis.evidence_id
                 AND mappings.source_identity_id = basis.source_identity_id
                GROUP BY basis.tile_id, basis.relation_id
                HAVING count(DISTINCT mappings.evidence_fingerprint) = 1
            )
            SELECT count(*) INTO resolved_basis_count FROM resolved;

            -- C7 lineage that is not yet exactly mapped remains local to C7.
            -- Never turn a missing diagnostic mapping into a scan/persistence
            -- blocker; only proven identities enter change detection.

            WITH accepted_basis AS (
                SELECT tiles.value ->> 'tile_id' AS tile_id,
                       basis.value ->> 'relation_id' AS relation_id,
                       basis.value ->> 'evidence_id' AS evidence_id,
                       basis.value ->> 'source_identity_id' AS source_identity_id
                FROM jsonb_array_elements(COALESCE(
                    latest_packet.packet_payload #>
                        '{accepted_memory,accepted_tiles}',
                    '[]'::jsonb
                )) AS tiles(value)
                CROSS JOIN LATERAL jsonb_array_elements(COALESCE(
                    tiles.value -> 'basis', '[]'::jsonb
                )) AS basis(value)
            ), exact_mappings AS (
                SELECT 'C7'::text AS tile_id, members.relation_id,
                       members.evidence_id, members.source_identity_id,
                       members.evidence_fingerprint
                FROM b3s_history.evidence_vault_verified_c7_lineage_members
                     AS members
                WHERE members.brand_id = p_brand_id
                  AND members.capture_id = ANY(capture_ids)
                UNION ALL
                SELECT 'C7'::text AS tile_id, members.relation_id,
                       members.evidence_id, members.source_identity_id,
                       members.evidence_fingerprint
                FROM b3s_history.evidence_vault_operational_source_capture_lineage_members
                     AS members
                WHERE members.brand_id = p_brand_id
                  AND members.capture_id = ANY(capture_ids)
            ), resolved AS (
                SELECT basis.tile_id,
                       min(mappings.evidence_fingerprint) AS evidence_fingerprint
                FROM accepted_basis AS basis
                JOIN exact_mappings AS mappings
                  ON mappings.tile_id = basis.tile_id
                 AND mappings.relation_id = basis.relation_id
                 AND mappings.evidence_id = basis.evidence_id
                 AND mappings.source_identity_id = basis.source_identity_id
                GROUP BY basis.tile_id, basis.relation_id
                HAVING count(DISTINCT mappings.evidence_fingerprint) = 1
            )
            SELECT COALESCE(jsonb_agg(jsonb_build_object(
                       'tile_id', resolved.tile_id,
                       'evidence_fingerprint', resolved.evidence_fingerprint,
                       'evidence_locator', NULL
                   ) ORDER BY resolved.tile_id, resolved.evidence_fingerprint),
                   '[]'::jsonb)
            INTO accepted_relations
            FROM resolved;
            IF resolved_basis_count > 5000
               OR pg_column_size(accepted_relations) > 8388608 THEN
                RAISE EXCEPTION 'raw planning accepted relations exceed bound';
            END IF;
        END IF;
    END IF;

    RETURN jsonb_build_object(
        'schema_version', 'evidence-vault-raw-planning-context-v1',
        'workspace_slug', p_workspace_slug,
        'source_scan_id', p_source_scan_id,
        'canonical_domain', p_canonical_domain,
        'canonical_memory_version', canonical_version,
        'previous_capture_evidence_records', previous_records,
        'known_evidence_records', known_records,
        -- Only identities proven by immutable capture lineage are projected.
        'accepted_evidence_tile_relations', accepted_relations,
        'operational_authority_context', COALESCE(
            authority_projection,
            jsonb_build_object(
                'database_time', clock_timestamp(),
                'operational_adoption_count', 0,
                'operational_adoptions', '[]'::jsonb,
                'operational_packet_count', 0,
                'operational_packets', '[]'::jsonb
            )
        ),
        'existing_operation_plan', existing_plan
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION
    b3s_history.read_evidence_vault_raw_planning_context(
        text, text, text, uuid, uuid, uuid
    ) FROM PUBLIC;

RESET ROLE;
REVOKE CREATE ON SCHEMA b3s_history
FROM b3s_history_vault_provenance_owner;

DO $$
DECLARE
    actual_owner text;
    function_signature text;
    unexpected_grantee text;
    scanner_role_exists boolean;
BEGIN
    scanner_role_exists := EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'b3s_pr71_scanner_ingest'
    );
    IF pg_catalog.has_schema_privilege(
        'b3s_history_vault_provenance_owner',
        'b3s_history',
        'CREATE'
    ) THEN
        RAISE EXCEPTION 'provenance owner retains forbidden schema CREATE';
    END IF;

    -- Migration 019 may have been created under poisoned default function ACLs.
    -- It is immutable, so this cumulative head sanitizes the complete three-call
    -- scanner surface before restoring only the one fixed deployment login.
    FOREACH function_signature IN ARRAY ARRAY[
        'b3s_history.append_evidence_vault_raw_acquisition(jsonb)',
        'b3s_history.read_evidence_vault_raw_acquisition(text,text)',
        'b3s_history.read_evidence_vault_raw_planning_context(text,text,text,uuid,uuid,uuid)'
    ] LOOP
        SELECT roles.rolname INTO actual_owner
        FROM pg_catalog.pg_proc AS functions
        JOIN pg_catalog.pg_roles AS roles ON roles.oid = functions.proowner
        WHERE functions.oid = function_signature::regprocedure
          AND functions.prosecdef;
        IF actual_owner IS DISTINCT FROM
                'b3s_history_vault_provenance_owner' THEN
            RAISE EXCEPTION
                'raw scanner function % has incorrect owner %',
                function_signature, actual_owner;
        END IF;

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
            WHERE functions.oid = function_signature::regprocedure
              AND grants.grantee <> functions.proowner
        LOOP
            EXECUTE pg_catalog.format(
                'REVOKE ALL PRIVILEGES ON FUNCTION %s FROM %I',
                function_signature,
                unexpected_grantee
            );
        END LOOP;

        IF scanner_role_exists THEN
            EXECUTE pg_catalog.format(
                'GRANT EXECUTE ON FUNCTION %s TO b3s_pr71_scanner_ingest',
                function_signature
            );
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
            WHERE functions.oid = function_signature::regprocedure
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
                'raw scanner function % retains an unexpected EXECUTE grant',
                function_signature;
        END IF;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION b3s_history.read_evidence_vault_raw_planning_context(
    text, text, text, uuid, uuid, uuid
) IS
    'Scanner-worker-only bounded planning projection; freezes canonical parent, prior analyzed evidence, and exactly mapped active accepted relations under the raw append lock order.';
