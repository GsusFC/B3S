-- Allow one signed raw-acquisition receipt to bind every deterministic evidence
-- record projected from that receipt. Existing rows remain valid and immutable.

GRANT USAGE, CREATE ON SCHEMA b3s_history
TO b3s_history_vault_provenance_owner;
SET LOCAL ROLE b3s_history_vault_provenance_owner;

ALTER TABLE b3s_history.evidence_vault_raw_evidence_bindings
    DROP CONSTRAINT evidence_vault_raw_evidence_bindings_receipt_id_key;
ALTER TABLE b3s_history.evidence_vault_raw_evidence_bindings
    ADD CONSTRAINT evidence_vault_raw_evidence_bindings_receipt_evidence_key
    UNIQUE (receipt_id, evidence_record_id);

ALTER FUNCTION b3s_history.append_evidence_vault_raw_acquisition(jsonb)
    RENAME TO append_evidence_vault_raw_acquisition_one_to_one_v1;
ALTER FUNCTION
    b3s_history.append_evidence_vault_raw_acquisition_one_to_one_v1(jsonb)
    SECURITY INVOKER;
REVOKE ALL ON FUNCTION
    b3s_history.append_evidence_vault_raw_acquisition_one_to_one_v1(jsonb)
FROM PUBLIC;

CREATE FUNCTION b3s_history.append_evidence_vault_raw_acquisition(payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    legacy_payload jsonb;
    append_result jsonb;
    raw_binding b3s_history.evidence_vault_raw_evidence_bindings%ROWTYPE;
    evidence_binding_ids jsonb;
    existing_capture_id uuid;
    existing_plan b3s_history.evidence_vault_operation_plans%ROWTYPE;
    existing_watermark b3s_history.evidence_vault_capture_watermark_events%ROWTYPE;
BEGIN
    IF jsonb_typeof(payload -> 'evidence_records') IS DISTINCT FROM 'array'
       OR jsonb_typeof(payload -> 'receipts') IS DISTINCT FROM 'array'
       OR jsonb_typeof(payload -> 'evidence_bindings') IS DISTINCT FROM 'array'
       OR jsonb_array_length(payload -> 'evidence_records') < 1
       OR jsonb_array_length(payload -> 'receipts') NOT BETWEEN 1 AND 2
       OR jsonb_array_length(payload -> 'evidence_bindings')
            IS DISTINCT FROM jsonb_array_length(payload -> 'evidence_records')
       OR jsonb_array_length(payload -> 'evidence_records')
            < jsonb_array_length(payload -> 'receipts') THEN
        RAISE EXCEPTION 'raw acquisition append envelope cardinality is invalid';
    END IF;

    -- Every binding must join the exact receipt and evidence identities carried
    -- by this payload. The binding trigger below re-verifies hashes, passages,
    -- fingerprints, and the already-durable foreign-key provenance.
    IF EXISTS (
        SELECT 1
        FROM jsonb_to_recordset(payload -> 'evidence_bindings') AS binding(
            id uuid,
            workspace_id uuid,
            brand_id uuid,
            scan_run_id uuid,
            capture_id uuid,
            receipt_id uuid,
            channel_role text,
            evidence_record_id uuid,
            evidence_ref text,
            source_url text,
            extractor_version text,
            extracted_document_sha256 text,
            evidence_record_content_hash text
        )
        LEFT JOIN jsonb_to_recordset(payload -> 'receipts') AS receipt(
            id uuid,
            workspace_id uuid,
            brand_id uuid,
            scan_run_id uuid,
            capture_id uuid,
            channel_role text,
            extractor_version text,
            extracted_document_sha256 text
        ) ON receipt.id = binding.receipt_id
        LEFT JOIN jsonb_to_recordset(payload -> 'evidence_records') AS evidence(
            id uuid,
            capture_id uuid,
            evidence_ref text,
            url text,
            content_hash text
        ) ON evidence.id = binding.evidence_record_id
        WHERE receipt.id IS NULL
           OR evidence.id IS NULL
           OR binding.workspace_id IS DISTINCT FROM receipt.workspace_id
           OR binding.brand_id IS DISTINCT FROM receipt.brand_id
           OR binding.scan_run_id IS DISTINCT FROM receipt.scan_run_id
           OR binding.capture_id IS DISTINCT FROM receipt.capture_id
           OR binding.channel_role IS DISTINCT FROM receipt.channel_role
           OR binding.extractor_version IS DISTINCT FROM receipt.extractor_version
           OR binding.extracted_document_sha256
                IS DISTINCT FROM receipt.extracted_document_sha256
           OR binding.capture_id IS DISTINCT FROM evidence.capture_id
           OR binding.evidence_ref IS DISTINCT FROM evidence.evidence_ref
           OR binding.source_url IS DISTINCT FROM evidence.url
           OR binding.evidence_record_content_hash IS DISTINCT FROM evidence.content_hash
    ) THEN
        RAISE EXCEPTION 'raw acquisition binding provenance is invalid';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM jsonb_to_recordset(payload -> 'evidence_records') AS evidence(id uuid)
        LEFT JOIN LATERAL (
            SELECT count(*) AS binding_count
            FROM jsonb_to_recordset(payload -> 'evidence_bindings') AS binding(
                evidence_record_id uuid
            )
            WHERE binding.evidence_record_id = evidence.id
        ) AS coverage ON true
        WHERE coverage.binding_count IS DISTINCT FROM 1
    ) OR EXISTS (
        SELECT 1
        FROM jsonb_to_recordset(payload -> 'receipts') AS receipt(id uuid)
        WHERE NOT EXISTS (
            SELECT 1
            FROM jsonb_to_recordset(payload -> 'evidence_bindings') AS binding(
                receipt_id uuid
            )
            WHERE binding.receipt_id = receipt.id
        )
    ) THEN
        RAISE EXCEPTION 'raw acquisition binding coverage is invalid';
    END IF;

    existing_capture_id := NULLIF(payload #>> '{capture,id}', '')::uuid;
    IF existing_capture_id IS NOT NULL AND EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_raw_evidence_bindings AS stored
        WHERE stored.capture_id = existing_capture_id
    ) THEN
        -- Replay cannot call the 1:1 helper: that helper compares the payload
        -- binding set to every stored row, so a truncated 1-per-receipt subset
        -- would look like an illegal upgrade after extras were appended.
        IF (
            SELECT array_agg(
                supplied.binding_fingerprint ORDER BY supplied.binding_fingerprint
            )
            FROM jsonb_populate_recordset(
                NULL::b3s_history.evidence_vault_raw_evidence_bindings,
                payload -> 'evidence_bindings'
            ) AS supplied
        ) IS DISTINCT FROM (
            SELECT array_agg(
                stored.binding_fingerprint ORDER BY stored.binding_fingerprint
            )
            FROM b3s_history.evidence_vault_raw_evidence_bindings AS stored
            WHERE stored.capture_id = existing_capture_id
        ) OR (
            SELECT array_agg(
                supplied.receipt_fingerprint ORDER BY supplied.receipt_fingerprint
            )
            FROM jsonb_populate_recordset(
                NULL::b3s_history.evidence_vault_raw_acquisition_receipts,
                payload -> 'receipts'
            ) AS supplied
        ) IS DISTINCT FROM (
            SELECT array_agg(
                stored.receipt_fingerprint ORDER BY stored.receipt_fingerprint
            )
            FROM b3s_history.evidence_vault_raw_acquisition_receipts AS stored
            WHERE stored.capture_id = existing_capture_id
        ) THEN
            RAISE EXCEPTION
                'pre-existing scan/capture cannot be upgraded with raw provenance';
        END IF;
        FOR raw_binding IN SELECT * FROM jsonb_populate_recordset(
            NULL::b3s_history.evidence_vault_raw_evidence_bindings,
            payload -> 'evidence_bindings'
        ) LOOP
            IF NOT EXISTS (
                SELECT 1
                FROM b3s_history.evidence_vault_raw_evidence_bindings AS stored
                WHERE stored.binding_fingerprint = raw_binding.binding_fingerprint
                  AND stored.id IS NOT DISTINCT FROM raw_binding.id
                  AND stored.workspace_id IS NOT DISTINCT FROM raw_binding.workspace_id
                  AND stored.brand_id IS NOT DISTINCT FROM raw_binding.brand_id
                  AND stored.scan_run_id IS NOT DISTINCT FROM raw_binding.scan_run_id
                  AND stored.capture_id IS NOT DISTINCT FROM raw_binding.capture_id
                  AND stored.receipt_id IS NOT DISTINCT FROM raw_binding.receipt_id
                  AND stored.channel_role IS NOT DISTINCT FROM raw_binding.channel_role
                  AND stored.evidence_record_id IS NOT DISTINCT FROM raw_binding.evidence_record_id
                  AND stored.evidence_ref IS NOT DISTINCT FROM raw_binding.evidence_ref
                  AND stored.source_url IS NOT DISTINCT FROM raw_binding.source_url
                  AND stored.extractor_schema_version IS NOT DISTINCT FROM raw_binding.extractor_schema_version
                  AND stored.extractor_version IS NOT DISTINCT FROM raw_binding.extractor_version
                  AND stored.extracted_document IS NOT DISTINCT FROM raw_binding.extracted_document
                  AND stored.extracted_document_sha256 IS NOT DISTINCT FROM raw_binding.extracted_document_sha256
                  AND stored.passage_locator IS NOT DISTINCT FROM raw_binding.passage_locator
                  AND stored.passage_text IS NOT DISTINCT FROM raw_binding.passage_text
                  AND stored.passage_sha256 IS NOT DISTINCT FROM raw_binding.passage_sha256
                  AND stored.evidence_record_content_hash IS NOT DISTINCT FROM raw_binding.evidence_record_content_hash
                  AND stored.binding_fingerprint IS NOT DISTINCT FROM raw_binding.binding_fingerprint
            ) THEN
                RAISE EXCEPTION
                    'raw evidence binding replay diverges from immutable stored content';
            END IF;
        END LOOP;
        SELECT * INTO existing_plan
        FROM b3s_history.evidence_vault_operation_plans
        WHERE scan_run_id = (payload #>> '{scan_run,id}')::uuid;
        SELECT * INTO existing_watermark
        FROM b3s_history.evidence_vault_capture_watermark_events
        WHERE capture_id = existing_capture_id;
        IF existing_plan.id IS NULL OR existing_watermark.id IS NULL THEN
            RAISE EXCEPTION
                'pre-existing scan/capture cannot be upgraded with raw provenance';
        END IF;
        -- Exact 1:N replay cannot call the 1:1 helper, but it must still fail
        -- closed on inconsistent envelopes and tampered stored identity.
        IF (payload #>> '{scan_run,workspace_id}')::uuid
                IS DISTINCT FROM (payload #>> '{workspace,id}')::uuid
           OR (payload #>> '{brand,workspace_id}')::uuid
                IS DISTINCT FROM (payload #>> '{workspace,id}')::uuid
           OR (payload #>> '{scan_run,brand_id}')::uuid
                IS DISTINCT FROM (payload #>> '{brand,id}')::uuid
           OR (payload #>> '{operation_plan,workspace_id}')::uuid
                IS DISTINCT FROM (payload #>> '{workspace,id}')::uuid
           OR (payload #>> '{operation_plan,brand_id}')::uuid
                IS DISTINCT FROM (payload #>> '{brand,id}')::uuid
           OR (payload #>> '{operation_plan,scan_run_id}')::uuid
                IS DISTINCT FROM (payload #>> '{scan_run,id}')::uuid
           OR (payload #>> '{capture,scan_run_id}')::uuid
                IS DISTINCT FROM (payload #>> '{scan_run,id}')::uuid
           OR (payload #>> '{capture,brand_id}')::uuid
                IS DISTINCT FROM (payload #>> '{brand,id}')::uuid
           OR (
                SELECT first_receipt.workspace_id
                           IS DISTINCT FROM (payload #>> '{workspace,id}')::uuid
                    OR first_receipt.brand_id
                           IS DISTINCT FROM (payload #>> '{brand,id}')::uuid
                    OR first_receipt.scan_run_id
                           IS DISTINCT FROM (payload #>> '{scan_run,id}')::uuid
                    OR first_receipt.capture_id
                           IS DISTINCT FROM (payload #>> '{capture,id}')::uuid
                FROM jsonb_populate_recordset(
                    NULL::b3s_history.evidence_vault_raw_acquisition_receipts,
                    payload -> 'receipts'
                ) AS first_receipt
                LIMIT 1
           ) THEN
            RAISE EXCEPTION
                'raw acquisition base observation identity is inconsistent';
        END IF;
        IF payload #>> '{capture,content_hash}' IS DISTINCT FROM encode(sha256(
                convert_to(
                    b3s_history.evidence_vault_canonical_json(
                        payload -> 'capture' -> 'raw_payload'
                    ),
                    'UTF8'
                )
           ), 'hex')
           OR payload #>> '{scan_run,metadata,observation_hash}'
                IS DISTINCT FROM encode(sha256(convert_to(
                    b3s_history.evidence_vault_canonical_json(
                        COALESCE(
                            payload -> 'scan_run' -> 'request_payload',
                            '{}'::jsonb
                        )
                    ),
                    'UTF8'
                )), 'hex')
           OR payload #>> '{scan_run,request_payload,schema_version}'
                IS DISTINCT FROM 'b3s-capture-observation-v1'
           OR payload #>> '{scan_run,request_payload,source_scan_id}'
                IS DISTINCT FROM payload #>> '{scan_run,source_scan_id}'
           OR payload #>> '{scan_run,request_payload,url}'
                IS DISTINCT FROM payload #>> '{capture,source_url}'
           OR (payload -> 'scan_run' -> 'request_payload' -> 'capture_payload')
                IS DISTINCT FROM (payload -> 'capture' -> 'raw_payload')
           OR (payload #>> '{scan_run,request_payload,observed_at}')::timestamptz
                IS DISTINCT FROM (payload #>> '{capture,observed_at}')::timestamptz
           OR (payload #>> '{scan_run,request_payload,recorded_at}')::timestamptz
                IS DISTINCT FROM (payload #>> '{capture,recorded_at}')::timestamptz
           OR (payload #>> '{scan_run,recorded_at}')::timestamptz
                IS DISTINCT FROM (payload #>> '{capture,recorded_at}')::timestamptz
           OR payload #>> '{scan_run,request_payload,pipeline_version}'
                IS DISTINCT FROM payload #>> '{scan_run,pipeline_version}'
           OR payload #>> '{scan_run,request_payload,acquisition_state}'
                IS DISTINCT FROM payload #>> '{scan_run,acquisition_state}'
           OR payload #>> '{watermark_event,capture_content_hash}'
                IS DISTINCT FROM payload #>> '{capture,content_hash}'
           OR payload #>> '{watermark_event,capture_observation_hash}'
                IS DISTINCT FROM payload #>> '{scan_run,metadata,observation_hash}'
           OR payload #>> '{watermark_event,append_origin}'
                IS DISTINCT FROM 'capture_observation_commit'
           OR payload #>> '{operation_plan,observation_hash}'
                IS DISTINCT FROM payload #>> '{scan_run,metadata,observation_hash}'
        THEN
            RAISE EXCEPTION
                'prepared capture/observation/operation-plan content is invalid';
        END IF;
        -- Exact 1:N replay cannot call the 1:1 helper, but it must still fail
        -- closed when stored scan or receipt identity was tampered with.
        IF NOT EXISTS (
            SELECT 1
            FROM b3s_history.scan_runs AS stored
            JOIN jsonb_populate_record(
                NULL::b3s_history.scan_runs, payload -> 'scan_run'
            ) AS supplied ON stored.id = supplied.id
            WHERE stored.workspace_id IS NOT DISTINCT FROM supplied.workspace_id
              AND stored.brand_id IS NOT DISTINCT FROM supplied.brand_id
              AND stored.source_scan_id IS NOT DISTINCT FROM supplied.source_scan_id
              AND stored.source_run_id
                    IS NOT DISTINCT FROM COALESCE(supplied.source_run_id, '')
              AND stored.status IS NOT DISTINCT FROM supplied.status
              AND stored.pipeline_version IS NOT DISTINCT FROM supplied.pipeline_version
              AND stored.acquisition_state IS NOT DISTINCT FROM COALESCE(
                    supplied.acquisition_state, 'unknown'
              )
              AND stored.requested_at IS NOT DISTINCT FROM supplied.requested_at
              AND stored.started_at IS NOT DISTINCT FROM supplied.started_at
              AND stored.completed_at IS NOT DISTINCT FROM supplied.completed_at
              AND stored.recorded_at IS NOT DISTINCT FROM supplied.recorded_at
              AND stored.error_summary
                    IS NOT DISTINCT FROM COALESCE(supplied.error_summary, '')
              AND stored.request_payload IS NOT DISTINCT FROM COALESCE(
                    supplied.request_payload, '{}'::jsonb
              )
              AND stored.metadata IS NOT DISTINCT FROM COALESCE(
                    supplied.metadata, '{}'::jsonb
              )
        ) THEN
            RAISE EXCEPTION 'scan replay diverges from stored immutable identity';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM jsonb_populate_recordset(
                NULL::b3s_history.evidence_vault_raw_acquisition_receipts,
                payload -> 'receipts'
            ) AS supplied
            WHERE NOT EXISTS (
                SELECT 1
                FROM b3s_history.evidence_vault_raw_acquisition_receipts AS stored
                WHERE stored.receipt_fingerprint = supplied.receipt_fingerprint
                  AND stored.id IS NOT DISTINCT FROM supplied.id
                  AND stored.workspace_id IS NOT DISTINCT FROM supplied.workspace_id
                  AND stored.brand_id IS NOT DISTINCT FROM supplied.brand_id
                  AND stored.scan_run_id IS NOT DISTINCT FROM supplied.scan_run_id
                  AND stored.source_scan_id IS NOT DISTINCT FROM supplied.source_scan_id
                  AND stored.capture_id IS NOT DISTINCT FROM supplied.capture_id
                  AND stored.watermark_event_id
                        IS NOT DISTINCT FROM existing_watermark.id
                  AND stored.capture_sequence
                        IS NOT DISTINCT FROM existing_watermark.capture_sequence
                  AND stored.key_id IS NOT DISTINCT FROM supplied.key_id
                  AND stored.receipt_nonce IS NOT DISTINCT FROM supplied.receipt_nonce
                  AND stored.acquisition_session_id
                        IS NOT DISTINCT FROM supplied.acquisition_session_id
                  AND stored.receipt_schema_version
                        IS NOT DISTINCT FROM supplied.receipt_schema_version
                  AND stored.freshness_policy_version
                        IS NOT DISTINCT FROM supplied.freshness_policy_version
                  AND stored.signature_schema_version
                        IS NOT DISTINCT FROM supplied.signature_schema_version
                  AND stored.workspace_slug IS NOT DISTINCT FROM supplied.workspace_slug
                  AND stored.canonical_brand
                        IS NOT DISTINCT FROM supplied.canonical_brand
                  AND stored.channel_role IS NOT DISTINCT FROM supplied.channel_role
                  AND stored.provider IS NOT DISTINCT FROM supplied.provider
                  AND stored.acquisition_mode
                        IS NOT DISTINCT FROM supplied.acquisition_mode
                  AND stored.pre_receipt_snapshot_sha256
                        IS NOT DISTINCT FROM supplied.pre_receipt_snapshot_sha256
                  AND stored.source_url IS NOT DISTINCT FROM supplied.source_url
                  AND stored.raw_fragment_pointer
                        IS NOT DISTINCT FROM supplied.raw_fragment_pointer
                  AND stored.raw_fragment_sha256
                        IS NOT DISTINCT FROM supplied.raw_fragment_sha256
                  AND stored.extracted_document_sha256
                        IS NOT DISTINCT FROM supplied.extracted_document_sha256
                  AND stored.extractor_version
                        IS NOT DISTINCT FROM supplied.extractor_version
                  AND stored.external_identity_provenance_fingerprint
                        IS NOT DISTINCT FROM
                            supplied.external_identity_provenance_fingerprint
                  AND stored.fetched_at IS NOT DISTINCT FROM supplied.fetched_at
                  AND stored.claims IS NOT DISTINCT FROM supplied.claims
                  AND stored.signed_payload IS NOT DISTINCT FROM supplied.signed_payload
                  AND stored.signature IS NOT DISTINCT FROM supplied.signature
                  AND stored.external_identity_provenance IS NOT DISTINCT FROM
                        supplied.external_identity_provenance
            )
        ) THEN
            RAISE EXCEPTION
                'raw receipt replay diverges from immutable stored content';
        END IF;
        SELECT COALESCE(
            jsonb_agg(binding ->> 'id' ORDER BY ordinality),
            '[]'::jsonb
        )
        INTO evidence_binding_ids
        FROM jsonb_array_elements(payload -> 'evidence_bindings')
             WITH ORDINALITY AS rows(binding, ordinality);
        RETURN jsonb_build_object(
            'capture_id', existing_capture_id,
            'operation_plan_id', existing_plan.id,
            'operation_plan_fingerprint', existing_plan.operation_plan_fingerprint,
            'watermark_event_id', existing_watermark.id,
            'capture_sequence', existing_watermark.capture_sequence,
            'previous_event_id', existing_watermark.previous_event_id,
            'previous_event_fingerprint', existing_watermark.previous_event_fingerprint,
            'watermark_event_fingerprint', existing_watermark.event_fingerprint,
            'receipt_ids', (
                SELECT COALESCE(
                    jsonb_agg(receipt ->> 'id' ORDER BY ordinality),
                    '[]'::jsonb
                )
                FROM jsonb_array_elements(payload -> 'receipts')
                     WITH ORDINALITY AS rows(receipt, ordinality)
            ),
            'evidence_binding_ids', evidence_binding_ids
        );
    END IF;

    -- Reuse the battle-tested v1 transaction for all workspace, brand, scan,
    -- capture, evidence, watermark, receipt, replay, and append-only checks.
    -- It receives one deterministic binding per receipt; this wrapper adds and
    -- re-verifies the remaining bindings in the same transaction.
    SELECT jsonb_agg(chosen.binding ORDER BY chosen.receipt_id, chosen.binding_id)
    INTO legacy_payload
    FROM (
        SELECT DISTINCT ON (binding ->> 'receipt_id')
            binding ->> 'receipt_id' AS receipt_id,
            binding ->> 'id' AS binding_id,
            binding
        FROM jsonb_array_elements(payload -> 'evidence_bindings') AS rows(binding)
        ORDER BY binding ->> 'receipt_id', binding ->> 'id'
    ) AS chosen;
    legacy_payload := payload || jsonb_build_object(
        'evidence_bindings', legacy_payload
    );
    append_result := b3s_history.append_evidence_vault_raw_acquisition_one_to_one_v1(
        legacy_payload
    );

    FOR raw_binding IN SELECT * FROM jsonb_populate_recordset(
        NULL::b3s_history.evidence_vault_raw_evidence_bindings,
        payload -> 'evidence_bindings'
    ) LOOP
        INSERT INTO b3s_history.evidence_vault_raw_evidence_bindings (
            id, workspace_id, brand_id, scan_run_id, capture_id, receipt_id,
            channel_role, evidence_record_id, evidence_ref, source_url,
            extractor_schema_version, extractor_version, extracted_document,
            extracted_document_sha256, passage_locator, passage_text,
            passage_sha256, evidence_record_content_hash, binding_fingerprint
        ) VALUES (
            raw_binding.id, raw_binding.workspace_id, raw_binding.brand_id,
            raw_binding.scan_run_id, raw_binding.capture_id, raw_binding.receipt_id,
            raw_binding.channel_role, raw_binding.evidence_record_id,
            raw_binding.evidence_ref, raw_binding.source_url,
            raw_binding.extractor_schema_version, raw_binding.extractor_version,
            raw_binding.extracted_document, raw_binding.extracted_document_sha256,
            raw_binding.passage_locator, raw_binding.passage_text,
            raw_binding.passage_sha256, raw_binding.evidence_record_content_hash,
            raw_binding.binding_fingerprint
        ) ON CONFLICT (binding_fingerprint) DO NOTHING;
        IF NOT EXISTS (
            SELECT 1
            FROM b3s_history.evidence_vault_raw_evidence_bindings AS stored
            WHERE stored.binding_fingerprint = raw_binding.binding_fingerprint
              AND stored.id IS NOT DISTINCT FROM raw_binding.id
              AND stored.workspace_id IS NOT DISTINCT FROM raw_binding.workspace_id
              AND stored.brand_id IS NOT DISTINCT FROM raw_binding.brand_id
              AND stored.scan_run_id IS NOT DISTINCT FROM raw_binding.scan_run_id
              AND stored.capture_id IS NOT DISTINCT FROM raw_binding.capture_id
              AND stored.receipt_id IS NOT DISTINCT FROM raw_binding.receipt_id
              AND stored.channel_role IS NOT DISTINCT FROM raw_binding.channel_role
              AND stored.evidence_record_id IS NOT DISTINCT FROM raw_binding.evidence_record_id
              AND stored.evidence_ref IS NOT DISTINCT FROM raw_binding.evidence_ref
              AND stored.source_url IS NOT DISTINCT FROM raw_binding.source_url
              AND stored.extractor_schema_version IS NOT DISTINCT FROM raw_binding.extractor_schema_version
              AND stored.extractor_version IS NOT DISTINCT FROM raw_binding.extractor_version
              AND stored.extracted_document IS NOT DISTINCT FROM raw_binding.extracted_document
              AND stored.extracted_document_sha256 IS NOT DISTINCT FROM raw_binding.extracted_document_sha256
              AND stored.passage_locator IS NOT DISTINCT FROM raw_binding.passage_locator
              AND stored.passage_text IS NOT DISTINCT FROM raw_binding.passage_text
              AND stored.passage_sha256 IS NOT DISTINCT FROM raw_binding.passage_sha256
              AND stored.evidence_record_content_hash IS NOT DISTINCT FROM raw_binding.evidence_record_content_hash
              AND stored.binding_fingerprint IS NOT DISTINCT FROM raw_binding.binding_fingerprint
        ) THEN
            RAISE EXCEPTION 'raw evidence binding replay diverges from immutable stored content';
        END IF;
    END LOOP;

    SELECT COALESCE(
        jsonb_agg(binding ->> 'id' ORDER BY ordinality),
        '[]'::jsonb
    )
    INTO evidence_binding_ids
    FROM jsonb_array_elements(payload -> 'evidence_bindings')
         WITH ORDINALITY AS rows(binding, ordinality);
    RETURN jsonb_set(
        append_result,
        '{evidence_binding_ids}',
        evidence_binding_ids
    );
END;
$$;

RESET ROLE;
REVOKE CREATE ON SCHEMA b3s_history
FROM b3s_history_vault_provenance_owner;

REVOKE EXECUTE ON FUNCTION
    b3s_history.append_evidence_vault_raw_acquisition(jsonb)
FROM PUBLIC;

-- Move every explicit non-owner grant from the renamed compatibility helper
-- to the new bounded facade. The helper remains callable only by its owner.
DO $$
DECLARE
    grantee text;
    preserved_grantees text[];
BEGIN
    SELECT array_agg(DISTINCT roles.rolname ORDER BY roles.rolname)
    INTO preserved_grantees
    FROM pg_catalog.pg_proc AS functions
    CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
        functions.proacl,
        pg_catalog.acldefault('f', functions.proowner)
    )) AS grants
    JOIN pg_catalog.pg_roles AS roles ON roles.oid = grants.grantee
    WHERE functions.oid =
        'b3s_history.append_evidence_vault_raw_acquisition_one_to_one_v1(jsonb)'::regprocedure
      AND grants.grantee <> functions.proowner;

    FOR grantee IN
        SELECT DISTINCT roles.rolname
        FROM pg_catalog.pg_proc AS functions
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
            functions.proacl,
            pg_catalog.acldefault('f', functions.proowner)
        )) AS grants
        JOIN pg_catalog.pg_roles AS roles ON roles.oid = grants.grantee
        WHERE functions.oid IN (
            'b3s_history.append_evidence_vault_raw_acquisition_one_to_one_v1(jsonb)'::regprocedure,
            'b3s_history.append_evidence_vault_raw_acquisition(jsonb)'::regprocedure
        )
          AND grants.grantee <> functions.proowner
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL PRIVILEGES ON FUNCTION '
            'b3s_history.append_evidence_vault_raw_acquisition_one_to_one_v1(jsonb) FROM %I',
            grantee
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL PRIVILEGES ON FUNCTION '
            'b3s_history.append_evidence_vault_raw_acquisition(jsonb) FROM %I',
            grantee
        );
    END LOOP;
    FOREACH grantee IN ARRAY COALESCE(preserved_grantees, ARRAY[]::text[])
    LOOP
        EXECUTE pg_catalog.format(
            'GRANT EXECUTE ON FUNCTION '
            'b3s_history.append_evidence_vault_raw_acquisition(jsonb) TO %I',
            grantee
        );
    END LOOP;
END;
$$;
