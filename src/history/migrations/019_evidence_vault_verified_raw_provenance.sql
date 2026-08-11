-- Verified raw/live acquisition provenance for Evidence Vault C7 shadow proof.
--
-- PostgreSQL validates durable identity, hashes, journal shape, and transitions.
-- Ed25519 authenticity is deliberately revalidated by trusted Python ingestion and
-- every readiness read; structurally valid SQL alone never grants runtime authority.

-- A release database which may create roles gets a stable NOLOGIN object owner.
-- Managed PostgreSQL installations without CREATEROLE must pre-provision that
-- owner and grant the release migrator real SET/MEMBER capability; never fall back
-- to privileged objects owned by a LOGIN migrator.
DO $$
DECLARE
    actor record;
    can_set_owner boolean := false;
    server_version integer := current_setting('server_version_num')::integer;
BEGIN
    SELECT rolsuper, rolcreaterole INTO actor
    FROM pg_catalog.pg_roles WHERE rolname = current_user;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'b3s_history_vault_provenance_owner'
    ) AND (actor.rolsuper OR actor.rolcreaterole) THEN
        BEGIN
            CREATE ROLE b3s_history_vault_provenance_owner NOLOGIN;
        EXCEPTION
            WHEN duplicate_object THEN
                NULL;
        END;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'b3s_history_vault_provenance_owner'
    ) THEN
        RAISE EXCEPTION
            'required NOLOGIN provenance owner is not provisioned';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'b3s_history_vault_provenance_owner'
          AND rolcanlogin
    ) THEN
        RAISE EXCEPTION
            'b3s_history_vault_provenance_owner must be NOLOGIN';
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
    IF NOT can_set_owner AND (actor.rolsuper OR actor.rolcreaterole) THEN
        BEGIN
            EXECUTE pg_catalog.format(
                'GRANT b3s_history_vault_provenance_owner TO %I', current_user
            );
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE EXCEPTION
                    'migrator cannot grant or SET required provenance owner';
        END;
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
    END IF;
    IF NOT can_set_owner THEN
        RAISE EXCEPTION
            'migrator must be able to SET required provenance owner';
    END IF;
END;
$$;

-- Deferred constraint triggers run after the public SECURITY DEFINER
-- append returns, so their wrappers must retain the same narrow owner boundary.
ALTER FUNCTION b3s_history.validate_evidence_vault_capture_watermark_row()
    SECURITY DEFINER;
ALTER FUNCTION b3s_history.validate_evidence_vault_capture_watermark_row()
    SET search_path = pg_catalog;
REVOKE ALL ON FUNCTION
    b3s_history.validate_evidence_vault_capture_watermark_row() FROM PUBLIC;

-- Existing contamination is a migration conflict.  Do not repair or reparent it.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM b3s_history.scan_runs AS scans
        JOIN b3s_history.brands AS brands ON brands.id = scans.brand_id
        WHERE scans.workspace_id IS DISTINCT FROM brands.workspace_id
    ) THEN
        RAISE EXCEPTION
            'scan workspace/brand composite-parent preflight failed';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM b3s_history.captures AS captures
        JOIN b3s_history.scan_runs AS scans ON scans.id = captures.scan_run_id
        WHERE captures.brand_id IS DISTINCT FROM scans.brand_id
    ) THEN
        RAISE EXCEPTION
            'capture brand/scan composite-parent preflight failed';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_operation_plans AS plans
        JOIN b3s_history.scan_runs AS scans ON scans.id = plans.scan_run_id
        WHERE plans.workspace_id IS DISTINCT FROM scans.workspace_id
           OR plans.brand_id IS DISTINCT FROM scans.brand_id
    ) THEN
        RAISE EXCEPTION
            'operation-plan workspace/brand/scan composite-parent preflight failed';
    END IF;
END;
$$;

ALTER TABLE b3s_history.brands
    ADD CONSTRAINT brands_workspace_id_id_key UNIQUE (workspace_id, id);
ALTER TABLE b3s_history.scan_runs
    ADD CONSTRAINT scan_runs_brand_id_id_key UNIQUE (brand_id, id),
    ADD CONSTRAINT scan_runs_workspace_brand_id_key
        UNIQUE (workspace_id, brand_id, id),
    ADD CONSTRAINT scan_runs_workspace_brand_fk
        FOREIGN KEY (workspace_id, brand_id)
        REFERENCES b3s_history.brands (workspace_id, id)
        ON DELETE RESTRICT;
ALTER TABLE b3s_history.captures
    ADD CONSTRAINT captures_brand_scan_run_key UNIQUE (brand_id, scan_run_id),
    ADD CONSTRAINT captures_brand_scan_run_fk
        FOREIGN KEY (brand_id, scan_run_id)
        REFERENCES b3s_history.scan_runs (brand_id, id)
        ON DELETE RESTRICT;
ALTER TABLE b3s_history.evidence_vault_operation_plans
    ADD CONSTRAINT evidence_vault_operation_plan_parent_key
        UNIQUE (workspace_id, brand_id, scan_run_id, id),
    ADD CONSTRAINT evidence_vault_operation_plan_scan_parent_fk
        FOREIGN KEY (workspace_id, brand_id, scan_run_id)
        REFERENCES b3s_history.scan_runs (workspace_id, brand_id, id)
        ON DELETE RESTRICT;

CREATE FUNCTION b3s_history.evidence_vault_json_has_exact_keys(
    value jsonb,
    expected_keys text[]
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT jsonb_typeof(value) = 'object'
       AND (SELECT COALESCE(array_agg(key ORDER BY key COLLATE "C"), ARRAY[]::text[])
            FROM jsonb_object_keys(value) AS keys(key))
           = (SELECT COALESCE(array_agg(key ORDER BY key COLLATE "C"), ARRAY[]::text[])
              FROM unnest(expected_keys) AS keys(key));
$$;

CREATE FUNCTION b3s_history.evidence_vault_strict_json_pointer_get(
    document jsonb,
    pointer text
)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
DECLARE
    current_value jsonb := document;
    token text;
    index_value integer;
BEGIN
    IF pointer = '' THEN
        RETURN current_value;
    END IF;
    IF left(pointer, 1) <> '/' OR pointer ~ '~([^01]|$)' THEN
        RAISE EXCEPTION 'invalid strict JSON pointer';
    END IF;
    FOREACH token IN ARRAY string_to_array(substr(pointer, 2), '/') LOOP
        token := replace(replace(token, '~1', '/'), '~0', '~');
        IF jsonb_typeof(current_value) = 'object' THEN
            IF NOT (current_value ? token) THEN
                RETURN NULL;
            END IF;
            current_value := current_value -> token;
        ELSIF jsonb_typeof(current_value) = 'array' THEN
            IF token !~ '^(0|[1-9][0-9]*)$' THEN
                RAISE EXCEPTION 'JSON pointer array token is not canonical';
            END IF;
            index_value := token::integer;
            IF index_value >= jsonb_array_length(current_value) THEN
                RETURN NULL;
            END IF;
            current_value := current_value -> index_value;
        ELSE
            RETURN NULL;
        END IF;
    END LOOP;
    RETURN current_value;
END;
$$;

CREATE FUNCTION b3s_history.evidence_vault_json_fragment_text(value jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT CASE jsonb_typeof(value)
        WHEN 'string' THEN value #>> '{}'
        ELSE b3s_history.evidence_vault_canonical_json(value)
    END;
$$;

CREATE FUNCTION b3s_history.evidence_vault_url_host(value text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
DECLARE
    authority text;
    host text;
BEGIN
    IF value !~ '^https?://[^/?#]+(?:[/?#]|$)' OR value ~ '[[:space:]]' THEN
        RETURN NULL;
    END IF;
    authority := substring(value FROM '^https?://([^/?#]+)');
    IF authority IS NULL OR authority ~ '[@:]' OR authority !~ '^[A-Za-z0-9.-]+$' THEN
        RETURN NULL;
    END IF;
    host := lower(authority);
    IF host LIKE 'www.%' THEN host := substr(host, 5); END IF;
    IF host ~ '(^|\\.)[0-9]+(\\.[0-9]+){3}$' OR host LIKE 'xn--%' OR host LIKE '%.xn--%' THEN
        RETURN NULL;
    END IF;
    RETURN host;
END;
$$;

CREATE FUNCTION b3s_history.evidence_vault_is_strict_linkedin_company_url(
    value text
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT value ~
        '^https://www[.]linkedin[.]com/company/[a-z0-9]([a-z0-9-]{0,99}[a-z0-9])?$';
$$;

CREATE FUNCTION b3s_history.evidence_vault_source_identity_id(
    source_url text,
    channel_role text
)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
DECLARE
    source_class text;
    normalized_url text;
BEGIN
    IF channel_role = 'owned_web' THEN
        source_class := 'owned_copy';
        normalized_url := CASE
            WHEN source_url ~ '^https?://[^/?#]+$' THEN source_url || '/'
            ELSE source_url
        END;
    ELSIF channel_role = 'external_social_profile' THEN
        source_class := 'external_proof';
        normalized_url := source_url;
    ELSE
        RAISE EXCEPTION 'source identity channel role is not eligible';
    END IF;
    RETURN encode(sha256(convert_to(
        b3s_history.evidence_vault_canonical_json(jsonb_build_object(
            'namespace', 'evidence-memory-document-v2',
            'payload', jsonb_build_object(
                'kind', 'url',
                'source_class', source_class,
                'url', normalized_url
            )
        )), 'UTF8'
    )), 'hex');
END;
$$;

CREATE FUNCTION b3s_history.evidence_vault_ingest_lock_key(
    workspace_id uuid,
    identity text
)
RETURNS bigint
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT ('x' || substr(
        encode(sha256(convert_to(workspace_id::text || ':' || identity, 'UTF8')), 'hex'),
        1, 16
    ))::bit(64)::bigint;
$$;

CREATE TABLE b3s_history.evidence_vault_raw_acquisition_receipts (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    brand_id uuid NOT NULL,
    scan_run_id uuid NOT NULL,
    source_scan_id text NOT NULL CHECK (length(source_scan_id) BETWEEN 1 AND 300),
    capture_id uuid NOT NULL,
    watermark_event_id uuid NOT NULL,
    capture_sequence bigint NOT NULL CHECK (capture_sequence > 0),
    receipt_schema_version text NOT NULL DEFAULT
        'evidence-vault-raw-acquisition-receipt-v1' CHECK (
            receipt_schema_version = 'evidence-vault-raw-acquisition-receipt-v1'
        ),
    freshness_policy_version text NOT NULL DEFAULT
        'evidence-vault-c7-live-freshness-policy-v1' CHECK (
            freshness_policy_version = 'evidence-vault-c7-live-freshness-policy-v1'
        ),
    signature_schema_version text NOT NULL DEFAULT
        'evidence-vault-ed25519-signature-v1' CHECK (
            signature_schema_version = 'evidence-vault-ed25519-signature-v1'
        ),
    key_id text NOT NULL CHECK (length(key_id) BETWEEN 1 AND 200),
    receipt_nonce uuid NOT NULL CHECK (receipt_nonce <> '00000000-0000-0000-0000-000000000000'::uuid),
    acquisition_session_id uuid NOT NULL,
    workspace_slug text NOT NULL CHECK (workspace_slug ~ '^[a-z0-9][a-z0-9-]*$'),
    canonical_brand text NOT NULL CHECK (length(canonical_brand) BETWEEN 1 AND 300),
    channel_role text NOT NULL CHECK (
        channel_role IN ('owned_web', 'external_social_profile')
    ),
    provider text NOT NULL CHECK (
        provider IN ('firecrawl', 'direct_http', 'browser_fallback', 'exa')
    ),
    acquisition_mode text NOT NULL CHECK (
        acquisition_mode IN ('direct_http', 'browser', 'provider_api')
    ),
    pre_receipt_snapshot_sha256 text NOT NULL CHECK (
        pre_receipt_snapshot_sha256 ~ '^[0-9a-f]{64}$'
    ),
    source_url text NOT NULL CHECK (length(source_url) BETWEEN 1 AND 4000),
    raw_fragment_pointer text NOT NULL CHECK (
        length(raw_fragment_pointer) BETWEEN 1 AND 2000
        AND left(raw_fragment_pointer, 1) = '/'
    ),
    raw_fragment_sha256 text NOT NULL CHECK (raw_fragment_sha256 ~ '^[0-9a-f]{64}$'),
    extracted_document_sha256 text NOT NULL CHECK (
        extracted_document_sha256 ~ '^[0-9a-f]{64}$'
    ),
    extractor_version text NOT NULL CHECK (
        extractor_version = 'evidence-vault-deterministic-extractor-v1'
    ),
    external_identity_provenance_fingerprint text CHECK (
        external_identity_provenance_fingerprint IS NULL
        OR external_identity_provenance_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    external_identity_provenance jsonb CHECK (
        external_identity_provenance IS NULL
        OR jsonb_typeof(external_identity_provenance) = 'object'
    ),
    fetched_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    eligible_until timestamptz NOT NULL,
    claims jsonb NOT NULL CHECK (jsonb_typeof(claims) = 'object'),
    receipt_fingerprint text NOT NULL CHECK (receipt_fingerprint ~ '^[0-9a-f]{64}$'),
    signed_payload jsonb NOT NULL CHECK (jsonb_typeof(signed_payload) = 'object'),
    signature text NOT NULL CHECK (
        signature ~ '^[A-Za-z0-9+/]{86}==$'
        AND octet_length(decode(signature, 'base64')) = 64
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (key_id, receipt_nonce),
    UNIQUE (receipt_fingerprint),
    UNIQUE (id, workspace_id, brand_id, scan_run_id, capture_id, channel_role),
    UNIQUE (id, receipt_fingerprint),
    UNIQUE (workspace_id, brand_id, capture_id, acquisition_session_id, channel_role),
    FOREIGN KEY (workspace_id, brand_id, scan_run_id)
        REFERENCES b3s_history.scan_runs (workspace_id, brand_id, id)
        ON DELETE RESTRICT,
    FOREIGN KEY (brand_id, capture_id)
        REFERENCES b3s_history.captures (brand_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (brand_id, watermark_event_id, capture_id, capture_sequence)
        REFERENCES b3s_history.evidence_vault_capture_watermark_events (
            brand_id, id, capture_id, capture_sequence
        ) ON DELETE RESTRICT,
    CHECK (
        (channel_role = 'owned_web'
            AND provider IN ('firecrawl', 'direct_http', 'browser_fallback')
            AND external_identity_provenance_fingerprint IS NULL
            AND external_identity_provenance IS NULL)
        OR (channel_role = 'external_social_profile'
            AND provider = 'exa'
            AND acquisition_mode = 'provider_api'
            AND external_identity_provenance_fingerprint IS NOT NULL
            AND external_identity_provenance IS NOT NULL)
    )
);

CREATE TABLE b3s_history.evidence_vault_raw_evidence_bindings (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    brand_id uuid NOT NULL,
    scan_run_id uuid NOT NULL,
    capture_id uuid NOT NULL,
    receipt_id uuid NOT NULL,
    channel_role text NOT NULL CHECK (
        channel_role IN ('owned_web', 'external_social_profile')
    ),
    evidence_record_id uuid NOT NULL,
    evidence_ref text NOT NULL CHECK (length(evidence_ref) BETWEEN 1 AND 500),
    source_url text NOT NULL CHECK (length(source_url) BETWEEN 1 AND 4000),
    extractor_schema_version text NOT NULL CHECK (
        extractor_schema_version = 'evidence-vault-deterministic-extractor-v1'
    ),
    extractor_version text NOT NULL CHECK (
        extractor_version = 'evidence-vault-deterministic-extractor-v1'
    ),
    extracted_document text NOT NULL CHECK (
        octet_length(convert_to(extracted_document, 'UTF8')) BETWEEN 1 AND 2097152
    ),
    extracted_document_sha256 text NOT NULL CHECK (
        extracted_document_sha256 ~ '^[0-9a-f]{64}$'
    ),
    passage_locator jsonb NOT NULL CHECK (jsonb_typeof(passage_locator) = 'object'),
    passage_text text NOT NULL CHECK (
        octet_length(convert_to(passage_text, 'UTF8')) BETWEEN 1 AND 20000
    ),
    passage_sha256 text NOT NULL CHECK (passage_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_record_content_hash text NOT NULL CHECK (
        evidence_record_content_hash ~ '^[0-9a-f]{64}$'
    ),
    binding_fingerprint text NOT NULL CHECK (binding_fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (receipt_id),
    UNIQUE (evidence_record_id),
    UNIQUE (binding_fingerprint),
    UNIQUE (id, workspace_id, brand_id, scan_run_id, capture_id,
            receipt_id, evidence_record_id, channel_role),
    FOREIGN KEY (receipt_id, workspace_id, brand_id, scan_run_id, capture_id, channel_role)
        REFERENCES b3s_history.evidence_vault_raw_acquisition_receipts (
            id, workspace_id, brand_id, scan_run_id, capture_id, channel_role
        ) ON DELETE RESTRICT,
    FOREIGN KEY (capture_id, evidence_record_id)
        REFERENCES b3s_history.evidence_records (capture_id, id) ON DELETE RESTRICT
);

CREATE TABLE b3s_history.evidence_vault_verified_c7_lineage_bindings (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    brand_id uuid NOT NULL,
    scan_run_id uuid NOT NULL,
    capture_id uuid NOT NULL,
    watermark_event_id uuid NOT NULL,
    capture_sequence bigint NOT NULL CHECK (capture_sequence > 0),
    operational_source_packet_id uuid NOT NULL,
    operation_plan_id uuid NOT NULL,
    composite_group_id text NOT NULL CHECK (composite_group_id ~ '^[0-9a-f]{64}$'),
    canonical_brand text NOT NULL CHECK (length(canonical_brand) BETWEEN 1 AND 300),
    source_packet_fingerprint text NOT NULL CHECK (
        source_packet_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    operation_plan_fingerprint text NOT NULL CHECK (
        operation_plan_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    exact_artifact_fingerprint text NOT NULL CHECK (
        exact_artifact_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    freshness_policy_version text NOT NULL CHECK (
        freshness_policy_version = 'evidence-vault-c7-live-freshness-policy-v1'
    ),
    public_key_registry_fingerprint text NOT NULL CHECK (
        public_key_registry_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    provenance text NOT NULL CHECK (provenance = 'verified_raw_acquisition_receipt'),
    receipt_set_fingerprint text NOT NULL CHECK (
        receipt_set_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    member_set_fingerprint text NOT NULL CHECK (
        member_set_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    eligible_until timestamptz NOT NULL,
    binding_schema_version text NOT NULL DEFAULT
        'evidence-vault-verified-c7-lineage-binding-v1' CHECK (
            binding_schema_version = 'evidence-vault-verified-c7-lineage-binding-v1'
        ),
    binding_fingerprint text NOT NULL CHECK (binding_fingerprint ~ '^[0-9a-f]{64}$'),
    authority boolean NOT NULL DEFAULT false CHECK (authority = false),
    production_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        production_runtime_effect = false
    ),
    scanner_runtime_effect boolean NOT NULL DEFAULT false CHECK (
        scanner_runtime_effect = false
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (binding_fingerprint),
    UNIQUE (brand_id, operational_source_packet_id, composite_group_id, capture_sequence),
    UNIQUE (id, workspace_id, brand_id, scan_run_id, capture_id,
            operational_source_packet_id, composite_group_id),
    FOREIGN KEY (workspace_id, brand_id, scan_run_id)
        REFERENCES b3s_history.scan_runs (workspace_id, brand_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (brand_id, watermark_event_id, capture_id, capture_sequence)
        REFERENCES b3s_history.evidence_vault_capture_watermark_events (
            brand_id, id, capture_id, capture_sequence
        ) ON DELETE RESTRICT,
    FOREIGN KEY (brand_id, operational_source_packet_id)
        REFERENCES b3s_history.evidence_vault_canonical_memory_packets (brand_id, id)
        ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, brand_id, scan_run_id, operation_plan_id)
        REFERENCES b3s_history.evidence_vault_operation_plans (
            workspace_id, brand_id, scan_run_id, id
        ) ON DELETE RESTRICT
);

CREATE TABLE b3s_history.evidence_vault_verified_c7_lineage_members (
    id uuid PRIMARY KEY,
    binding_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    brand_id uuid NOT NULL,
    scan_run_id uuid NOT NULL,
    capture_id uuid NOT NULL,
    operational_source_packet_id uuid NOT NULL,
    composite_group_id text NOT NULL CHECK (composite_group_id ~ '^[0-9a-f]{64}$'),
    raw_evidence_binding_id uuid NOT NULL,
    receipt_id uuid NOT NULL,
    evidence_record_id uuid NOT NULL,
    channel_role text NOT NULL CHECK (
        channel_role IN ('owned_web', 'external_social_profile')
    ),
    relation_id text NOT NULL CHECK (relation_id ~ '^[0-9a-f]{64}$'),
    evidence_id text NOT NULL CHECK (evidence_id ~ '^[0-9a-f]{64}$'),
    source_identity_id text NOT NULL CHECK (source_identity_id ~ '^[0-9a-f]{64}$'),
    evidence_fingerprint text NOT NULL CHECK (evidence_fingerprint ~ '^[0-9a-f]{64}$'),
    evidence_ref text NOT NULL CHECK (length(evidence_ref) BETWEEN 1 AND 500),
    source_ref text NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 1000),
    evidence_quote text NOT NULL CHECK (length(evidence_quote) BETWEEN 1 AND 20000),
    member_fingerprint text NOT NULL CHECK (member_fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (binding_id, channel_role),
    UNIQUE (binding_id, receipt_id),
    UNIQUE (binding_id, raw_evidence_binding_id),
    UNIQUE (binding_id, evidence_record_id),
    UNIQUE (binding_id, source_identity_id),
    UNIQUE (binding_id, relation_id),
    UNIQUE (member_fingerprint),
    FOREIGN KEY (binding_id, workspace_id, brand_id, scan_run_id, capture_id,
                 operational_source_packet_id, composite_group_id)
        REFERENCES b3s_history.evidence_vault_verified_c7_lineage_bindings (
            id, workspace_id, brand_id, scan_run_id, capture_id,
            operational_source_packet_id, composite_group_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (raw_evidence_binding_id, workspace_id, brand_id, scan_run_id,
                 capture_id, receipt_id, evidence_record_id, channel_role)
        REFERENCES b3s_history.evidence_vault_raw_evidence_bindings (
            id, workspace_id, brand_id, scan_run_id, capture_id,
            receipt_id, evidence_record_id, channel_role
        ) ON DELETE RESTRICT
);

CREATE TABLE b3s_history.evidence_vault_raw_provenance_disposition_events (
    id uuid PRIMARY KEY,
    binding_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    brand_id uuid NOT NULL,
    scan_run_id uuid NOT NULL,
    capture_id uuid NOT NULL,
    operational_source_packet_id uuid NOT NULL,
    composite_group_id text NOT NULL CHECK (composite_group_id ~ '^[0-9a-f]{64}$'),
    receipt_set_fingerprint text NOT NULL CHECK (receipt_set_fingerprint ~ '^[0-9a-f]{64}$'),
    event_sequence bigint NOT NULL CHECK (event_sequence > 0),
    previous_event_id uuid,
    previous_event_fingerprint text,
    action text NOT NULL CHECK (
        action IN ('retain', 'place_legal_hold', 'release_legal_hold', 'revoke_runtime')
    ),
    prior_state text NOT NULL CHECK (
        prior_state IN ('none', 'retained', 'legal_hold', 'runtime_revoked')
    ),
    resulting_state text NOT NULL CHECK (
        resulting_state IN ('retained', 'legal_hold', 'runtime_revoked')
    ),
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000),
    actor_id text NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 200),
    idempotency_key_hash text NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    event_schema_version text NOT NULL DEFAULT
        'evidence-vault-raw-provenance-disposition-event-v1' CHECK (
            event_schema_version = 'evidence-vault-raw-provenance-disposition-event-v1'
        ),
    event_fingerprint text NOT NULL CHECK (event_fingerprint ~ '^[0-9a-f]{64}$'),
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (binding_id, event_sequence),
    UNIQUE (binding_id, previous_event_id),
    UNIQUE (binding_id, idempotency_key_hash),
    UNIQUE (binding_id, event_fingerprint),
    UNIQUE (binding_id, id, event_fingerprint),
    FOREIGN KEY (binding_id, workspace_id, brand_id, scan_run_id, capture_id,
                 operational_source_packet_id, composite_group_id)
        REFERENCES b3s_history.evidence_vault_verified_c7_lineage_bindings (
            id, workspace_id, brand_id, scan_run_id, capture_id,
            operational_source_packet_id, composite_group_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (binding_id, previous_event_id, previous_event_fingerprint)
        REFERENCES b3s_history.evidence_vault_raw_provenance_disposition_events (
            binding_id, id, event_fingerprint
        ) ON DELETE RESTRICT,
    CHECK (
        (event_sequence = 1 AND previous_event_id IS NULL
            AND previous_event_fingerprint IS NULL)
        OR (event_sequence > 1 AND previous_event_id IS NOT NULL
            AND previous_event_fingerprint IS NOT NULL)
    )
);

CREATE INDEX evidence_vault_raw_receipts_capture_idx
    ON b3s_history.evidence_vault_raw_acquisition_receipts
       (workspace_id, brand_id, capture_id, acquisition_session_id);
CREATE INDEX evidence_vault_verified_c7_head_idx
    ON b3s_history.evidence_vault_verified_c7_lineage_bindings
       (brand_id, capture_sequence DESC);
CREATE INDEX evidence_vault_raw_disposition_head_idx
    ON b3s_history.evidence_vault_raw_provenance_disposition_events
       (binding_id, event_sequence DESC);


CREATE FUNCTION b3s_history.protect_evidence_vault_operation_plan_parent()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF OLD.workspace_id IS DISTINCT FROM NEW.workspace_id
       OR OLD.brand_id IS DISTINCT FROM NEW.brand_id
       OR OLD.scan_run_id IS DISTINCT FROM NEW.scan_run_id THEN
        RAISE EXCEPTION 'operation-plan workspace/brand/scan identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER evidence_vault_operation_plan_parent_immutable
BEFORE UPDATE ON b3s_history.evidence_vault_operation_plans
FOR EACH ROW EXECUTE FUNCTION b3s_history.protect_evidence_vault_operation_plan_parent();

CREATE FUNCTION b3s_history.validate_evidence_vault_raw_receipt_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    expected_fingerprint text;
    expected_signed_payload jsonb;
    expected_keys text[];
    durable_workspace_slug text;
    durable_source_scan_id text;
    durable_brand text;
    acquisition jsonb;
    hop jsonb;
    current_url text;
    stored_received_at timestamptz;
    stored_eligible_until timestamptz;
BEGIN
    -- PostgreSQL, never the caller, records first arrival. Exact content-addressed
    -- replay preserves that arrival/expiry instead of manufacturing fresh evidence.
    SELECT received_at, eligible_until
    INTO stored_received_at, stored_eligible_until
    FROM b3s_history.evidence_vault_raw_acquisition_receipts
    WHERE receipt_fingerprint = NEW.receipt_fingerprint;
    IF stored_received_at IS NULL THEN
        NEW.received_at := clock_timestamp();
        NEW.eligible_until := LEAST(NEW.fetched_at, NEW.received_at) + interval '24 hours';
    ELSE
        NEW.received_at := stored_received_at;
        NEW.eligible_until := stored_eligible_until;
    END IF;

    SELECT workspaces.slug, scans.source_scan_id, brands.canonical_domain
    INTO durable_workspace_slug, durable_source_scan_id, durable_brand
    FROM b3s_history.scan_runs AS scans
    JOIN b3s_history.workspaces AS workspaces ON workspaces.id = scans.workspace_id
    JOIN b3s_history.brands AS brands ON brands.id = scans.brand_id
    WHERE scans.id = NEW.scan_run_id;
    IF durable_workspace_slug IS DISTINCT FROM NEW.workspace_slug
       OR durable_source_scan_id IS DISTINCT FROM NEW.source_scan_id
       OR durable_brand IS DISTINCT FROM NEW.canonical_brand THEN
        RAISE EXCEPTION 'raw receipt durable scan identity is invalid';
    END IF;

    expected_keys := ARRAY[
        'schema_version', 'freshness_policy_version', 'key_id', 'receipt_nonce',
        'acquisition_session_id', 'workspace_slug', 'source_scan_id',
        'canonical_brand_domain', 'channel_role', 'pre_receipt_snapshot_sha256',
        'provider', 'acquisition', 'fetched_at', 'status_code',
        'selected_headers', 'media_type', 'byte_count',
        'raw_fragment_json_pointer', 'raw_fragment_sha256',
        'extracted_document_sha256', 'extractor_version',
        'external_identity_provenance_fingerprint'
    ];
    acquisition := NEW.claims -> 'acquisition';
    IF NOT b3s_history.evidence_vault_json_has_exact_keys(NEW.claims, expected_keys)
       OR NEW.claims ->> 'schema_version' IS DISTINCT FROM NEW.receipt_schema_version
       OR NEW.claims ->> 'freshness_policy_version' IS DISTINCT FROM NEW.freshness_policy_version
       OR NEW.claims ->> 'key_id' IS DISTINCT FROM NEW.key_id
       OR (NEW.claims ->> 'receipt_nonce')::uuid IS DISTINCT FROM NEW.receipt_nonce
       OR (NEW.claims ->> 'acquisition_session_id')::uuid IS DISTINCT FROM NEW.acquisition_session_id
       OR NEW.claims ->> 'workspace_slug' IS DISTINCT FROM NEW.workspace_slug
       OR NEW.claims ->> 'source_scan_id' IS DISTINCT FROM NEW.source_scan_id
       OR NEW.claims ->> 'canonical_brand_domain' IS DISTINCT FROM NEW.canonical_brand
       OR NEW.claims ->> 'channel_role' IS DISTINCT FROM NEW.channel_role
       OR NEW.claims ->> 'pre_receipt_snapshot_sha256' IS DISTINCT FROM NEW.pre_receipt_snapshot_sha256
       OR NEW.claims ->> 'provider' IS DISTINCT FROM NEW.provider
       OR acquisition ->> 'acquisition_mode' IS DISTINCT FROM NEW.acquisition_mode
       OR (NEW.claims ->> 'fetched_at')::timestamptz IS DISTINCT FROM NEW.fetched_at
       OR NEW.claims ->> 'raw_fragment_json_pointer' IS DISTINCT FROM NEW.raw_fragment_pointer
       OR NEW.claims ->> 'raw_fragment_sha256' IS DISTINCT FROM NEW.raw_fragment_sha256
       OR NEW.claims ->> 'extracted_document_sha256' IS DISTINCT FROM NEW.extracted_document_sha256
       OR NEW.claims ->> 'extractor_version' IS DISTINCT FROM NEW.extractor_version
       OR NEW.claims ->> 'external_identity_provenance_fingerprint'
            IS DISTINCT FROM NEW.external_identity_provenance_fingerprint
       OR jsonb_typeof(acquisition) IS DISTINCT FROM 'object'
       OR jsonb_typeof(NEW.claims -> 'selected_headers') IS DISTINCT FROM 'object'
       OR NEW.claims ->> 'status_code' IS NULL
       OR (NEW.claims ->> 'status_code')::integer NOT BETWEEN 200 AND 299
       OR NEW.claims ->> 'byte_count' IS NULL
       OR (NEW.claims ->> 'byte_count')::bigint NOT BETWEEN 1 AND 100000000 THEN
        RAISE EXCEPTION 'raw receipt claims do not match the exact v1 schema';
    END IF;
    IF NEW.acquisition_mode IN ('direct_http', 'browser') THEN
        IF NOT b3s_history.evidence_vault_json_has_exact_keys(
                acquisition,
                ARRAY['acquisition_mode', 'requested_url', 'redirect_chain', 'final_url']
           )
           OR jsonb_typeof(acquisition -> 'redirect_chain') IS DISTINCT FROM 'array'
           OR acquisition ->> 'final_url' IS DISTINCT FROM NEW.source_url
           OR acquisition ->> 'requested_url' IS NULL
           OR b3s_history.evidence_vault_url_host(acquisition ->> 'requested_url')
                IS DISTINCT FROM NEW.canonical_brand THEN
            RAISE EXCEPTION 'raw direct acquisition union is invalid';
        END IF;
        current_url := acquisition ->> 'requested_url';
        FOR hop IN SELECT value
                   FROM jsonb_array_elements(acquisition -> 'redirect_chain') LOOP
            IF NOT b3s_history.evidence_vault_json_has_exact_keys(
                    hop, ARRAY['request_url', 'status_code', 'location_url']
               )
               OR hop ->> 'request_url' IS DISTINCT FROM current_url
               OR (hop ->> 'status_code')::integer NOT IN (300, 301, 302, 303, 307, 308)
               OR b3s_history.evidence_vault_url_host(hop ->> 'request_url')
                    IS DISTINCT FROM NEW.canonical_brand
               OR b3s_history.evidence_vault_url_host(hop ->> 'location_url')
                    IS DISTINCT FROM NEW.canonical_brand THEN
                RAISE EXCEPTION 'raw direct redirect chain is invalid';
            END IF;
            current_url := hop ->> 'location_url';
        END LOOP;
        IF current_url IS DISTINCT FROM acquisition ->> 'final_url' THEN
            RAISE EXCEPTION 'raw direct redirect chain is incomplete';
        END IF;
    ELSIF NOT b3s_history.evidence_vault_json_has_exact_keys(
                acquisition,
                ARRAY['acquisition_mode', 'provider_request_fingerprint',
                      'result_ordinal', 'reported_source_url', 'redirect_chain']
           )
       OR acquisition ->> 'reported_source_url' IS DISTINCT FROM NEW.source_url
       OR acquisition ->> 'provider_request_fingerprint' IS NULL
       OR acquisition ->> 'provider_request_fingerprint' !~ '^[0-9a-f]{64}$'
       OR (acquisition ->> 'result_ordinal')::integer NOT BETWEEN 0 AND 9999
       OR jsonb_typeof(acquisition -> 'redirect_chain') IS DISTINCT FROM 'array'
       OR jsonb_array_length(acquisition -> 'redirect_chain') <> 0 THEN
        RAISE EXCEPTION 'raw provider acquisition union is invalid';
    END IF;
    IF NEW.channel_role = 'owned_web' THEN
        IF b3s_history.evidence_vault_url_host(NEW.source_url)
                IS DISTINCT FROM NEW.canonical_brand THEN
            RAISE EXCEPTION 'owned receipt final origin differs from canonical brand';
        END IF;
    ELSIF NOT b3s_history.evidence_vault_is_strict_linkedin_company_url(NEW.source_url) THEN
        RAISE EXCEPTION 'external receipt is not a strict LinkedIn company URL';
    END IF;
    IF NEW.channel_role = 'external_social_profile' THEN
        IF NOT b3s_history.evidence_vault_json_has_exact_keys(
                NEW.external_identity_provenance,
                ARRAY['schema_version', 'policy_version', 'association_method',
                      'canonical_brand_domain', 'owned_source_url',
                      'external_source_url', 'proof_receipt_fingerprint',
                      'raw_fact_role', 'raw_fact_json_pointer', 'raw_fact_sha256',
                      'source_identity_schema_version',
                      'owned_source_identity_id', 'external_source_identity_id']
           )
           OR NEW.external_identity_provenance ->> 'schema_version'
                IS DISTINCT FROM 'external-identity-provenance-v1'
           OR NEW.external_identity_provenance ->> 'policy_version'
                IS DISTINCT FROM
                    'evidence-vault-external-identity-association-policy-v1'
           OR NEW.external_identity_provenance ->> 'association_method'
                NOT IN ('owned_raw_links_external_profile',
                        'external_raw_declares_owned_domain')
           OR NEW.external_identity_provenance ->> 'canonical_brand_domain'
                IS DISTINCT FROM NEW.canonical_brand
           OR NEW.external_identity_provenance ->> 'external_source_url'
                IS DISTINCT FROM NEW.source_url
           OR NEW.external_identity_provenance ->> 'owned_source_url'
                IS DISTINCT FROM 'https://' || NEW.canonical_brand
           OR NEW.external_identity_provenance ->> 'proof_receipt_fingerprint'
                !~ '^[0-9a-f]{64}$'
           OR NEW.external_identity_provenance ->> 'raw_fact_role'
                NOT IN ('owned_web', 'external_social_profile')
           OR NEW.external_identity_provenance ->> 'raw_fact_json_pointer' !~ '^/'
           OR NEW.external_identity_provenance ->> 'raw_fact_sha256'
                !~ '^[0-9a-f]{64}$'
           OR NEW.external_identity_provenance ->> 'source_identity_schema_version'
                IS DISTINCT FROM 'evidence-memory-document-v2'
           OR NEW.external_identity_provenance ->> 'owned_source_identity_id'
                !~ '^[0-9a-f]{64}$'
           OR NEW.external_identity_provenance ->> 'external_source_identity_id'
                !~ '^[0-9a-f]{64}$'
           OR NEW.external_identity_provenance_fingerprint IS DISTINCT FROM
                b3s_history.evidence_vault_canonical_fingerprint(
                    'external-identity-provenance-v1',
                    NEW.external_identity_provenance
                ) THEN
            RAISE EXCEPTION 'external identity provenance object/fingerprint is invalid';
        END IF;
    END IF;
    IF NEW.fetched_at > NEW.received_at + interval '5 minutes'
       OR NEW.received_at - NEW.fetched_at > interval '15 minutes' THEN
        RAISE EXCEPTION 'raw receipt violates live freshness arrival policy';
    END IF;

    expected_fingerprint := b3s_history.evidence_vault_canonical_fingerprint(
        'evidence-vault-raw-acquisition-receipt-v1', NEW.claims
    );
    IF NEW.receipt_fingerprint IS DISTINCT FROM expected_fingerprint THEN
        RAISE EXCEPTION 'raw receipt fingerprint is invalid';
    END IF;
    NEW.receipt_fingerprint := expected_fingerprint;
    expected_signed_payload := jsonb_build_object(
        'signature_schema', NEW.signature_schema_version,
        'claims', NEW.claims,
        'receipt_fingerprint', expected_fingerprint
    );
    IF NEW.signed_payload IS DISTINCT FROM expected_signed_payload THEN
        RAISE EXCEPTION 'raw receipt signed payload is invalid';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER evidence_vault_raw_receipts_validate_insert
BEFORE INSERT ON b3s_history.evidence_vault_raw_acquisition_receipts
FOR EACH ROW EXECUTE FUNCTION b3s_history.validate_evidence_vault_raw_receipt_insert();

CREATE FUNCTION b3s_history.validate_evidence_vault_raw_receipt_durable()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    durable_payload jsonb;
    durable_capture_hash text;
    provenance_envelope jsonb;
    fragment jsonb;
    durable_hash text;
    expected_pre_receipt_hash text;
    expected_signed_receipts jsonb;
    expected_receipt_set_fingerprint text;
    proof_receipt record;
    proof_fragment jsonb;
    proof_pointer text;
    fact_receipt record;
    proof_value text;
    proof_method text;
    proof_hash text;
    expected_owned_source_identity_id text;
    expected_external_source_identity_id text;
BEGIN
    SELECT raw_payload, content_hash INTO durable_payload, durable_capture_hash
    FROM b3s_history.captures WHERE id = NEW.capture_id;
    provenance_envelope := durable_payload -> 'evidence_vault_raw_provenance';
    fragment := b3s_history.evidence_vault_strict_json_pointer_get(
        durable_payload, NEW.raw_fragment_pointer
    );
    IF fragment IS NULL THEN
        RAISE EXCEPTION 'raw receipt pointer does not resolve in durable capture';
    END IF;
    durable_hash := encode(sha256(convert_to(
        b3s_history.evidence_vault_canonical_json(fragment), 'UTF8'
    )), 'hex');
    IF NEW.raw_fragment_sha256 IS DISTINCT FROM durable_hash THEN
        RAISE EXCEPTION 'raw receipt fragment hash differs from durable capture';
    END IF;
    expected_pre_receipt_hash := encode(sha256(convert_to(
        b3s_history.evidence_vault_canonical_json(jsonb_build_object(
            'schema_version', 'evidence-vault-pre-receipt-snapshot-v1',
            'workspace_slug', NEW.workspace_slug,
            'source_scan_id', NEW.source_scan_id,
            'acquisition_session_id', NEW.acquisition_session_id::text,
            'canonical_brand_domain', NEW.canonical_brand,
            'canonical_brand_url', (
                SELECT canonical_url FROM b3s_history.brands WHERE id = NEW.brand_id
            ),
            'raw_payload', durable_payload - 'evidence_vault_raw_provenance'
        )), 'UTF8'
    )), 'hex');
    SELECT jsonb_agg(jsonb_build_object(
               'signature_schema', receipts.signature_schema_version,
               'claims', receipts.claims,
               'receipt_fingerprint', receipts.receipt_fingerprint,
               'signature', receipts.signature
           ) ORDER BY receipts.receipt_fingerprint),
           b3s_history.evidence_vault_canonical_fingerprint(
               'evidence-vault-raw-acquisition-receipt-set-v1',
               jsonb_build_object(
                   'schema_version', 'evidence-vault-raw-acquisition-receipt-set-v1',
                   'receipt_fingerprints', jsonb_agg(
                       receipts.receipt_fingerprint ORDER BY receipts.receipt_fingerprint
                   )
               )
           )
    INTO expected_signed_receipts, expected_receipt_set_fingerprint
    FROM b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
    WHERE receipts.workspace_id = NEW.workspace_id
      AND receipts.brand_id = NEW.brand_id
      AND receipts.scan_run_id = NEW.scan_run_id
      AND receipts.capture_id = NEW.capture_id
      AND receipts.acquisition_session_id = NEW.acquisition_session_id;
    IF NOT b3s_history.evidence_vault_json_has_exact_keys(
            provenance_envelope,
            ARRAY['schema_version', 'workspace_slug', 'source_scan_id',
                  'acquisition_session_id', 'canonical_brand_domain',
                  'canonical_brand_url', 'pre_receipt_snapshot_sha256',
                  'signed_receipts', 'receipt_set_fingerprint',
                  'external_identity_provenance']
       )
       OR provenance_envelope ->> 'schema_version' IS DISTINCT FROM
            'evidence-vault-raw-provenance-envelope-v1'
       OR provenance_envelope ->> 'workspace_slug' IS DISTINCT FROM NEW.workspace_slug
       OR provenance_envelope ->> 'source_scan_id' IS DISTINCT FROM NEW.source_scan_id
       OR (provenance_envelope ->> 'acquisition_session_id')::uuid
            IS DISTINCT FROM NEW.acquisition_session_id
       OR provenance_envelope ->> 'canonical_brand_domain'
            IS DISTINCT FROM NEW.canonical_brand
       OR provenance_envelope ->> 'canonical_brand_url' IS DISTINCT FROM (
            SELECT canonical_url FROM b3s_history.brands WHERE id = NEW.brand_id
       )
       OR provenance_envelope ->> 'pre_receipt_snapshot_sha256'
            IS DISTINCT FROM expected_pre_receipt_hash
       OR NEW.pre_receipt_snapshot_sha256 IS DISTINCT FROM expected_pre_receipt_hash
       OR provenance_envelope -> 'signed_receipts'
            IS DISTINCT FROM expected_signed_receipts
       OR provenance_envelope ->> 'receipt_set_fingerprint'
            IS DISTINCT FROM expected_receipt_set_fingerprint
       OR durable_capture_hash IS DISTINCT FROM encode(sha256(convert_to(
            b3s_history.evidence_vault_canonical_json(durable_payload), 'UTF8'
          )), 'hex') THEN
        RAISE EXCEPTION 'capture raw provenance envelope/content hash is invalid';
    END IF;
    IF NEW.channel_role = 'external_social_profile' THEN
        IF provenance_envelope -> 'external_identity_provenance'
                IS DISTINCT FROM NEW.external_identity_provenance THEN
            RAISE EXCEPTION
                'capture external identity provenance differs from durable receipt';
        END IF;
        proof_pointer := NEW.external_identity_provenance ->> 'raw_fact_json_pointer';
        proof_method := NEW.external_identity_provenance ->> 'association_method';
        SELECT * INTO proof_receipt
        FROM b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
        WHERE receipts.receipt_fingerprint =
                NEW.external_identity_provenance ->> 'proof_receipt_fingerprint'
          AND receipts.workspace_id = NEW.workspace_id
          AND receipts.brand_id = NEW.brand_id
          AND receipts.scan_run_id = NEW.scan_run_id
          AND receipts.capture_id = NEW.capture_id
          AND receipts.acquisition_session_id = NEW.acquisition_session_id
          AND receipts.channel_role = 'owned_web';
        SELECT * INTO fact_receipt
        FROM b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
        WHERE receipts.workspace_id = NEW.workspace_id
          AND receipts.brand_id = NEW.brand_id
          AND receipts.scan_run_id = NEW.scan_run_id
          AND receipts.capture_id = NEW.capture_id
          AND receipts.acquisition_session_id = NEW.acquisition_session_id
          AND receipts.channel_role =
                NEW.external_identity_provenance ->> 'raw_fact_role';
        proof_fragment := b3s_history.evidence_vault_strict_json_pointer_get(
            durable_payload, proof_pointer
        );
        IF proof_fragment IS NOT NULL THEN
            proof_value := b3s_history.evidence_vault_json_fragment_text(proof_fragment);
            proof_hash := encode(sha256(convert_to(
                b3s_history.evidence_vault_canonical_json(proof_fragment), 'UTF8'
            )), 'hex');
        END IF;
        expected_owned_source_identity_id :=
            b3s_history.evidence_vault_source_identity_id(
                NEW.external_identity_provenance ->> 'owned_source_url',
                'owned_web'
            );
        expected_external_source_identity_id :=
            b3s_history.evidence_vault_source_identity_id(
                NEW.external_identity_provenance ->> 'external_source_url',
                'external_social_profile'
            );
        IF proof_receipt.id IS NULL OR fact_receipt.id IS NULL
           OR NEW.external_identity_provenance ->> 'owned_source_identity_id'
                IS DISTINCT FROM expected_owned_source_identity_id
           OR NEW.external_identity_provenance ->> 'external_source_identity_id'
                IS DISTINCT FROM expected_external_source_identity_id
           OR b3s_history.evidence_vault_url_host(proof_receipt.source_url)
                IS DISTINCT FROM NEW.canonical_brand
           OR b3s_history.evidence_vault_url_host(
                NEW.external_identity_provenance ->> 'owned_source_url'
              ) IS DISTINCT FROM NEW.canonical_brand
           OR NEW.source_url IS DISTINCT FROM
                NEW.external_identity_provenance ->> 'external_source_url'
           OR proof_fragment IS NULL
           OR NOT (
                proof_pointer = fact_receipt.raw_fragment_pointer
                OR proof_pointer LIKE fact_receipt.raw_fragment_pointer || '/%'
           )
           OR proof_hash IS DISTINCT FROM
                NEW.external_identity_provenance ->> 'raw_fact_sha256'
           OR (proof_method = 'owned_raw_links_external_profile'
               AND (fact_receipt.channel_role IS DISTINCT FROM 'owned_web'
                    OR proof_value IS DISTINCT FROM NEW.source_url))
           OR (proof_method = 'external_raw_declares_owned_domain'
               AND (fact_receipt.channel_role IS DISTINCT FROM 'external_social_profile'
                    OR proof_value IS DISTINCT FROM
                        NEW.external_identity_provenance ->> 'owned_source_url')) THEN
            RAISE EXCEPTION 'external identity signed raw fact is not reproducible';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER evidence_vault_raw_receipts_validate_durable
AFTER INSERT ON b3s_history.evidence_vault_raw_acquisition_receipts
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION b3s_history.validate_evidence_vault_raw_receipt_durable();

CREATE FUNCTION b3s_history.validate_evidence_vault_raw_evidence_binding_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    receipt record;
    evidence record;
    expected_extracted_passage text;
    expected_evidence_passage text;
    extracted_start integer;
    extracted_end integer;
    evidence_start integer;
    evidence_end integer;
    expected_fingerprint text;
BEGIN
    SELECT * INTO receipt
    FROM b3s_history.evidence_vault_raw_acquisition_receipts
    WHERE id = NEW.receipt_id;
    SELECT * INTO evidence
    FROM b3s_history.evidence_records WHERE id = NEW.evidence_record_id;

    IF receipt.id IS NULL OR evidence.id IS NULL
       OR receipt.extractor_version IS DISTINCT FROM NEW.extractor_version
       OR receipt.extracted_document_sha256 IS DISTINCT FROM NEW.extracted_document_sha256
       OR evidence.evidence_ref IS DISTINCT FROM NEW.evidence_ref
       OR evidence.url IS DISTINCT FROM NEW.source_url
       OR evidence.content_hash IS DISTINCT FROM NEW.evidence_record_content_hash
       OR NEW.extracted_document_sha256 IS DISTINCT FROM encode(
            sha256(convert_to(NEW.extracted_document, 'UTF8')), 'hex'
          )
       OR NEW.passage_sha256 IS DISTINCT FROM encode(
            sha256(convert_to(NEW.passage_text, 'UTF8')), 'hex'
          ) THEN
        RAISE EXCEPTION 'raw evidence binding differs from durable receipt/evidence';
    END IF;

    IF NEW.passage_locator ->> 'kind' IS DISTINCT FROM 'utf8_byte_range'
       OR NOT b3s_history.evidence_vault_json_has_exact_keys(
            NEW.passage_locator,
            ARRAY[
                'kind', 'extracted_start', 'extracted_end',
                'evidence_start', 'evidence_end'
            ]
       )
       OR jsonb_typeof(NEW.passage_locator -> 'extracted_start') IS DISTINCT FROM 'number'
       OR jsonb_typeof(NEW.passage_locator -> 'extracted_end') IS DISTINCT FROM 'number'
       OR jsonb_typeof(NEW.passage_locator -> 'evidence_start') IS DISTINCT FROM 'number'
       OR jsonb_typeof(NEW.passage_locator -> 'evidence_end') IS DISTINCT FROM 'number'
       OR NEW.passage_locator ->> 'extracted_start' !~ '^(0|[1-9][0-9]*)$'
       OR NEW.passage_locator ->> 'extracted_end' !~ '^(0|[1-9][0-9]*)$'
       OR NEW.passage_locator ->> 'evidence_start' !~ '^(0|[1-9][0-9]*)$'
       OR NEW.passage_locator ->> 'evidence_end' !~ '^(0|[1-9][0-9]*)$' THEN
        RAISE EXCEPTION 'raw evidence passage locator schema is invalid';
    END IF;
    BEGIN
        extracted_start := (NEW.passage_locator ->> 'extracted_start')::integer;
        extracted_end := (NEW.passage_locator ->> 'extracted_end')::integer;
        evidence_start := (NEW.passage_locator ->> 'evidence_start')::integer;
        evidence_end := (NEW.passage_locator ->> 'evidence_end')::integer;
        IF extracted_end <= extracted_start
           OR evidence_end <= evidence_start
           OR extracted_end > octet_length(convert_to(NEW.extracted_document, 'UTF8'))
           OR evidence_end > octet_length(convert_to(evidence.content, 'UTF8')) THEN
            RAISE EXCEPTION 'raw evidence byte locator is invalid';
        END IF;
        expected_extracted_passage := convert_from(
            substring(
                convert_to(NEW.extracted_document, 'UTF8')
                FROM extracted_start + 1
                FOR extracted_end - extracted_start
            ),
            'UTF8'
        );
        expected_evidence_passage := convert_from(
            substring(
                convert_to(evidence.content, 'UTF8')
                FROM evidence_start + 1
                FOR evidence_end - evidence_start
            ),
            'UTF8'
        );
    EXCEPTION
        WHEN OTHERS THEN
            RAISE EXCEPTION 'raw evidence byte locator is invalid';
    END;
    IF expected_extracted_passage IS DISTINCT FROM NEW.passage_text
       OR expected_evidence_passage IS DISTINCT FROM NEW.passage_text
       OR expected_extracted_passage IS DISTINCT FROM expected_evidence_passage
       OR octet_length(convert_to(NEW.passage_text, 'UTF8')) < 8
       OR char_length(NEW.passage_text) < 4 THEN
        RAISE EXCEPTION 'raw evidence locator does not reproduce a meaningful passage';
    END IF;

    expected_fingerprint := b3s_history.evidence_vault_canonical_fingerprint(
        'evidence-vault-raw-evidence-binding-v1',
        jsonb_build_object(
            'receipt_fingerprint', receipt.receipt_fingerprint,
            'evidence_record_id', NEW.evidence_record_id::text,
            'evidence_ref', NEW.evidence_ref,
            'source_url', NEW.source_url,
            'channel_role', NEW.channel_role,
            'extractor_schema_version', NEW.extractor_schema_version,
            'extractor_version', NEW.extractor_version,
            'extracted_document_sha256', NEW.extracted_document_sha256,
            'passage_locator', NEW.passage_locator,
            'passage_sha256', NEW.passage_sha256,
            'evidence_record_content_hash', NEW.evidence_record_content_hash
        )
    );
    IF NEW.binding_fingerprint IS DISTINCT FROM expected_fingerprint THEN
        RAISE EXCEPTION 'raw evidence binding fingerprint is invalid';
    END IF;
    NEW.binding_fingerprint := expected_fingerprint;
    RETURN NEW;
END;
$$;

CREATE TRIGGER evidence_vault_raw_evidence_bindings_validate_insert
BEFORE INSERT ON b3s_history.evidence_vault_raw_evidence_bindings
FOR EACH ROW EXECUTE FUNCTION b3s_history.validate_evidence_vault_raw_evidence_binding_insert();

CREATE FUNCTION b3s_history.validate_evidence_vault_verified_c7_binding(
    p_binding_id uuid
)
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    binding record;
    packet record;
    plan record;
    watermark record;
    brand record;
    exact_group jsonb;
    artifact jsonb;
    members_payload jsonb;
    receipts_payload jsonb;
    expected_member_set text;
    expected_receipt_set text;
    expected_binding text;
    member_count integer;
    acquisition_count integer;
    c7_group_count integer;
BEGIN
    SELECT * INTO binding
    FROM b3s_history.evidence_vault_verified_c7_lineage_bindings
    WHERE id = p_binding_id;
    IF binding.id IS NULL THEN
        RAISE EXCEPTION 'verified C7 binding disappeared before validation';
    END IF;
    SELECT * INTO packet FROM b3s_history.evidence_vault_canonical_memory_packets
    WHERE id = binding.operational_source_packet_id;
    SELECT * INTO plan FROM b3s_history.evidence_vault_operation_plans
    WHERE id = binding.operation_plan_id;
    SELECT * INTO watermark FROM b3s_history.evidence_vault_capture_watermark_events
    WHERE id = binding.watermark_event_id;
    SELECT * INTO brand FROM b3s_history.brands WHERE id = binding.brand_id;
    artifact := packet.reference_resolution -> 'artifact';

    SELECT count(*), min(groups.value::text)::jsonb
    INTO c7_group_count, exact_group
    FROM jsonb_array_elements(COALESCE(artifact -> 'groups', '[]'::jsonb)) AS groups(value)
    WHERE groups.value ->> 'tile_id' = 'C7';
    IF c7_group_count IS DISTINCT FROM 1
       OR packet.packet_kind IS DISTINCT FROM 'operational_source_v2'
       OR packet.brand_id IS DISTINCT FROM binding.brand_id
       OR packet.packet_fingerprint IS DISTINCT FROM binding.source_packet_fingerprint
       OR packet.reference_resolution ->> 'source_kind' IS DISTINCT FROM 'exact_relation_supplement'
       OR packet.reference_resolution ->> 'artifact_fingerprint'
            IS DISTINCT FROM binding.exact_artifact_fingerprint
       OR artifact ->> 'artifact_fingerprint' IS DISTINCT FROM binding.exact_artifact_fingerprint
       OR packet.brand_identity IS DISTINCT FROM binding.canonical_brand
       OR packet.manifest ->> 'brand_identity' IS DISTINCT FROM binding.canonical_brand
       OR artifact ->> 'brand_identity' IS DISTINCT FROM binding.canonical_brand
       OR plan.operation_plan_fingerprint IS DISTINCT FROM binding.operation_plan_fingerprint
       OR plan.plan_payload ->> 'brand_identity' IS DISTINCT FROM binding.canonical_brand
       OR brand.canonical_domain IS DISTINCT FROM binding.canonical_brand
       OR b3s_history.evidence_vault_url_host(artifact ->> 'subject_url')
            IS DISTINCT FROM binding.canonical_brand
       OR b3s_history.evidence_vault_url_host(plan.plan_payload ->> 'subject_url')
            IS DISTINCT FROM binding.canonical_brand
       OR watermark.brand_id IS DISTINCT FROM binding.brand_id
       OR watermark.capture_id IS DISTINCT FROM binding.capture_id
       OR watermark.capture_sequence IS DISTINCT FROM binding.capture_sequence
       OR exact_group IS NULL
       OR exact_group ->> 'group_id' IS DISTINCT FROM binding.composite_group_id
       OR exact_group ->> 'decision_rule' IS DISTINCT FROM 'all_of'
       OR jsonb_array_length(COALESCE(exact_group -> 'relations', '[]'::jsonb)) <> 2
       OR EXISTS (
            SELECT 1 FROM b3s_history.evidence_vault_capture_watermark_events AS newer
            WHERE newer.brand_id = binding.brand_id
              AND newer.capture_sequence > binding.capture_sequence
       ) THEN
        RAISE EXCEPTION 'verified C7 binding identity/current-watermark contract is invalid';
    END IF;

    SELECT count(*), count(DISTINCT receipts.acquisition_session_id),
           jsonb_agg(jsonb_build_object(
               'channel_role', members.channel_role,
               'relation_id', members.relation_id,
               'evidence_id', members.evidence_id,
               'source_identity_id', members.source_identity_id,
               'evidence_fingerprint', members.evidence_fingerprint,
               'evidence_ref', members.evidence_ref,
               'source_ref', members.source_ref,
               'evidence_quote', members.evidence_quote,
               'receipt_fingerprint', receipts.receipt_fingerprint,
               'raw_evidence_binding_fingerprint', raw_bindings.binding_fingerprint
           ) ORDER BY members.relation_id)
    INTO member_count, acquisition_count, members_payload
    FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
    JOIN b3s_history.evidence_vault_raw_evidence_bindings AS raw_bindings
      ON raw_bindings.id = members.raw_evidence_binding_id
    JOIN b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
      ON receipts.id = members.receipt_id
    WHERE members.binding_id = binding.id;

    SELECT jsonb_build_object(
        'schema_version', 'evidence-vault-raw-acquisition-receipt-set-v1',
        'receipt_fingerprints', jsonb_agg(
            receipts.receipt_fingerprint ORDER BY receipts.receipt_fingerprint
        )
    )
    INTO receipts_payload
    FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
    JOIN b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
      ON receipts.id = members.receipt_id
    WHERE members.binding_id = binding.id;

    IF member_count IS DISTINCT FROM 2 OR acquisition_count IS DISTINCT FROM 1
       OR (SELECT array_agg(channel_role ORDER BY channel_role)
           FROM b3s_history.evidence_vault_verified_c7_lineage_members
           WHERE binding_id = binding.id)
          IS DISTINCT FROM ARRAY['external_social_profile', 'owned_web']::text[]
       OR EXISTS (
            SELECT 1
            FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
            JOIN b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
              ON receipts.id = members.receipt_id
            JOIN b3s_history.evidence_vault_raw_evidence_bindings AS raw_bindings
              ON raw_bindings.id = members.raw_evidence_binding_id
            LEFT JOIN LATERAL (
                SELECT relations.value
                FROM jsonb_array_elements(exact_group -> 'relations') AS relations(value)
                WHERE relations.value ->> 'relation_id' = members.relation_id
            ) AS relation ON true
            WHERE members.binding_id = binding.id
              AND (receipts.workspace_id IS DISTINCT FROM binding.workspace_id
                OR receipts.brand_id IS DISTINCT FROM binding.brand_id
                OR receipts.scan_run_id IS DISTINCT FROM binding.scan_run_id
                OR receipts.capture_id IS DISTINCT FROM binding.capture_id
                OR receipts.watermark_event_id IS DISTINCT FROM binding.watermark_event_id
                OR receipts.capture_sequence IS DISTINCT FROM binding.capture_sequence
                OR receipts.freshness_policy_version IS DISTINCT FROM binding.freshness_policy_version
                OR receipts.eligible_until < clock_timestamp()
                OR raw_bindings.passage_text IS DISTINCT FROM members.evidence_quote
                OR relation.value IS NULL
                OR relation.value ->> 'evidence_id' IS DISTINCT FROM members.evidence_id
                OR relation.value ->> 'source_identity_id' IS DISTINCT FROM members.source_identity_id
                OR relation.value ->> 'evidence_fingerprint' IS DISTINCT FROM members.evidence_fingerprint
                OR relation.value ->> 'channel_role' IS DISTINCT FROM members.channel_role
                OR relation.value ->> 'ref' IS DISTINCT FROM members.evidence_ref
                OR relation.value ->> 'ref' IS DISTINCT FROM members.source_ref
                OR relation.value ->> 'literal_quote' IS DISTINCT FROM members.evidence_quote
                OR members.member_fingerprint IS DISTINCT FROM
                    b3s_history.evidence_vault_canonical_fingerprint(
                        'evidence-vault-verified-c7-lineage-member-v1',
                        jsonb_build_object(
                            'group_id', members.composite_group_id,
                            'relation_id', members.relation_id,
                            'evidence_id', members.evidence_id,
                            'source_identity_id', members.source_identity_id,
                            'evidence_fingerprint', members.evidence_fingerprint,
                            'channel_role', members.channel_role,
                            'evidence_ref', members.evidence_ref,
                            'source_ref', members.source_ref,
                            'evidence_quote', members.evidence_quote,
                            'receipt_fingerprint', receipts.receipt_fingerprint,
                            'raw_evidence_binding_fingerprint', raw_bindings.binding_fingerprint
                        )
                    ))
       )
       OR EXISTS (
            SELECT 1
            FROM b3s_history.evidence_vault_raw_acquisition_receipts AS external
            WHERE external.id = (
                SELECT members.receipt_id
                FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
                WHERE members.binding_id = binding.id
                  AND members.channel_role = 'external_social_profile'
            )
              AND (
                external.external_identity_provenance ->> 'owned_source_identity_id'
                    IS DISTINCT FROM (
                        SELECT members.source_identity_id
                        FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
                        WHERE members.binding_id = binding.id
                          AND members.channel_role = 'owned_web'
                    )
                OR external.external_identity_provenance ->> 'external_source_identity_id'
                    IS DISTINCT FROM (
                        SELECT members.source_identity_id
                        FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
                        WHERE members.binding_id = binding.id
                          AND members.channel_role = 'external_social_profile'
                    )
              )
       )
       OR (SELECT max(receipts.fetched_at) - min(receipts.fetched_at)
           FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
           JOIN b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
             ON receipts.id = members.receipt_id
           WHERE members.binding_id = binding.id) > interval '15 minutes' THEN
        RAISE EXCEPTION 'verified C7 binding requires exactly two valid independent members';
    END IF;

    expected_receipt_set := b3s_history.evidence_vault_canonical_fingerprint(
        'evidence-vault-raw-acquisition-receipt-set-v1', receipts_payload
    );
    expected_member_set := b3s_history.evidence_vault_canonical_fingerprint(
        'evidence-vault-verified-c7-lineage-member-set-v1', members_payload
    );
    IF binding.receipt_set_fingerprint IS DISTINCT FROM expected_receipt_set
       OR binding.member_set_fingerprint IS DISTINCT FROM expected_member_set
       OR binding.eligible_until IS DISTINCT FROM (
            SELECT min(receipts.eligible_until)
            FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
            JOIN b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
              ON receipts.id = members.receipt_id
            WHERE members.binding_id = binding.id
       ) THEN
        RAISE EXCEPTION 'verified C7 binding set/freshness fingerprint is invalid';
    END IF;
    expected_binding := b3s_history.evidence_vault_canonical_fingerprint(
        'evidence-vault-verified-c7-lineage-binding-v1',
        jsonb_build_object(
            'schema_version', binding.binding_schema_version,
            'canonical_brand', binding.canonical_brand,
            'source_packet_fingerprint', binding.source_packet_fingerprint,
            'operation_plan_fingerprint', binding.operation_plan_fingerprint,
            'exact_artifact_fingerprint', binding.exact_artifact_fingerprint,
            'watermark_event_fingerprint', watermark.event_fingerprint,
            'capture_id', binding.capture_id::text,
            'capture_sequence', binding.capture_sequence,
            'composite_group_id', binding.composite_group_id,
            'freshness_policy_version', binding.freshness_policy_version,
            'public_key_registry_fingerprint',
                binding.public_key_registry_fingerprint,
            'provenance', binding.provenance,
            'receipt_set_fingerprint', binding.receipt_set_fingerprint,
            'member_set_fingerprint', binding.member_set_fingerprint,
            'eligible_until', binding.eligible_until
        )
    );
    IF binding.binding_fingerprint IS DISTINCT FROM expected_binding THEN
        RAISE EXCEPTION 'verified C7 lineage binding fingerprint is invalid';
    END IF;
END;
$$;

CREATE FUNCTION b3s_history.validate_evidence_vault_verified_c7_binding_trigger()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_TABLE_NAME = 'evidence_vault_verified_c7_lineage_bindings' THEN
        PERFORM b3s_history.validate_evidence_vault_verified_c7_binding(
            COALESCE(NEW.id, OLD.id)
        );
    ELSE
        PERFORM b3s_history.validate_evidence_vault_verified_c7_binding(
            COALESCE(NEW.binding_id, OLD.binding_id)
        );
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$;

CREATE CONSTRAINT TRIGGER evidence_vault_verified_c7_binding_validate
AFTER INSERT ON b3s_history.evidence_vault_verified_c7_lineage_bindings
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION b3s_history.validate_evidence_vault_verified_c7_binding_trigger();

CREATE CONSTRAINT TRIGGER evidence_vault_verified_c7_member_set_validate
AFTER INSERT OR UPDATE OR DELETE ON b3s_history.evidence_vault_verified_c7_lineage_members
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION b3s_history.validate_evidence_vault_verified_c7_binding_trigger();

CREATE FUNCTION b3s_history.validate_evidence_vault_disposition_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    target record;
    prior record;
    expected_prior_state text;
    expected_resulting_state text;
    expected_fingerprint text;
BEGIN
    SELECT * INTO target
    FROM b3s_history.evidence_vault_verified_c7_lineage_bindings
    WHERE id = NEW.binding_id;
    NEW.occurred_at := clock_timestamp();
    IF target.id IS NULL
       OR NEW.receipt_set_fingerprint IS DISTINCT FROM target.receipt_set_fingerprint THEN
        RAISE EXCEPTION 'disposition target receipt set is invalid';
    END IF;
    IF NEW.event_sequence = 1 THEN
        IF NEW.action IS DISTINCT FROM 'retain' THEN
            RAISE EXCEPTION 'initial raw provenance disposition must retain';
        END IF;
        expected_prior_state := 'none';
        expected_resulting_state := 'retained';
        IF EXISTS (
            SELECT 1 FROM b3s_history.evidence_vault_raw_provenance_disposition_events
            WHERE binding_id = NEW.binding_id
        ) THEN
            RAISE EXCEPTION 'disposition sequence one requires an empty target journal';
        END IF;
    ELSE
        SELECT * INTO prior
        FROM b3s_history.evidence_vault_raw_provenance_disposition_events
        WHERE binding_id = NEW.binding_id
          AND id = NEW.previous_event_id
          AND event_fingerprint = NEW.previous_event_fingerprint;
        IF prior.id IS NULL OR prior.event_sequence IS DISTINCT FROM NEW.event_sequence - 1
           OR EXISTS (
                SELECT 1 FROM b3s_history.evidence_vault_raw_provenance_disposition_events AS later
                WHERE later.binding_id = NEW.binding_id
                  AND later.event_sequence > prior.event_sequence
           ) THEN
            RAISE EXCEPTION 'disposition predecessor must be the target head N-1';
        END IF;
        expected_prior_state := prior.resulting_state;
        IF prior.resulting_state = 'retained' AND NEW.action = 'place_legal_hold' THEN
            expected_resulting_state := 'legal_hold';
        ELSIF prior.resulting_state = 'legal_hold' AND NEW.action = 'release_legal_hold' THEN
            expected_resulting_state := 'retained';
        ELSIF prior.resulting_state IN ('retained', 'legal_hold')
              AND NEW.action = 'revoke_runtime' THEN
            expected_resulting_state := 'runtime_revoked';
        ELSE
            RAISE EXCEPTION 'raw provenance disposition transition is invalid or terminal';
        END IF;
    END IF;
    IF NEW.prior_state IS DISTINCT FROM expected_prior_state
       OR NEW.resulting_state IS DISTINCT FROM expected_resulting_state THEN
        RAISE EXCEPTION 'raw provenance disposition state projection is invalid';
    END IF;
    expected_fingerprint := b3s_history.evidence_vault_canonical_fingerprint(
        'evidence-vault-raw-provenance-disposition-event-v1',
        jsonb_build_object(
            'schema_version', NEW.event_schema_version,
            'binding_fingerprint', target.binding_fingerprint,
            'receipt_set_fingerprint', NEW.receipt_set_fingerprint,
            'event_sequence', NEW.event_sequence,
            'previous_event_fingerprint', NEW.previous_event_fingerprint,
            'action', NEW.action,
            'prior_state', NEW.prior_state,
            'resulting_state', NEW.resulting_state,
            'reason', NEW.reason,
            'actor_id', NEW.actor_id,
            'idempotency_key_hash', NEW.idempotency_key_hash
        )
    );
    IF NEW.event_fingerprint IS DISTINCT FROM expected_fingerprint THEN
        RAISE EXCEPTION 'raw provenance disposition fingerprint is invalid';
    END IF;
    NEW.event_fingerprint := expected_fingerprint;
    RETURN NEW;
END;
$$;

CREATE TRIGGER evidence_vault_raw_dispositions_validate_insert
BEFORE INSERT ON b3s_history.evidence_vault_raw_provenance_disposition_events
FOR EACH ROW EXECUTE FUNCTION b3s_history.validate_evidence_vault_disposition_insert();

CREATE FUNCTION b3s_history.reject_evidence_vault_raw_provenance_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'verified raw Evidence Vault journals are append-only';
END;
$$;

CREATE TRIGGER evidence_vault_raw_receipts_append_only
BEFORE UPDATE OR DELETE ON b3s_history.evidence_vault_raw_acquisition_receipts
FOR EACH ROW EXECUTE FUNCTION b3s_history.reject_evidence_vault_raw_provenance_mutation();
CREATE TRIGGER evidence_vault_raw_evidence_bindings_append_only
BEFORE UPDATE OR DELETE ON b3s_history.evidence_vault_raw_evidence_bindings
FOR EACH ROW EXECUTE FUNCTION b3s_history.reject_evidence_vault_raw_provenance_mutation();
CREATE TRIGGER evidence_vault_verified_c7_bindings_append_only
BEFORE UPDATE OR DELETE ON b3s_history.evidence_vault_verified_c7_lineage_bindings
FOR EACH ROW EXECUTE FUNCTION b3s_history.reject_evidence_vault_raw_provenance_mutation();
CREATE TRIGGER evidence_vault_verified_c7_members_append_only
BEFORE UPDATE OR DELETE ON b3s_history.evidence_vault_verified_c7_lineage_members
FOR EACH ROW EXECUTE FUNCTION b3s_history.reject_evidence_vault_raw_provenance_mutation();
CREATE TRIGGER evidence_vault_raw_dispositions_append_only
BEFORE UPDATE OR DELETE ON b3s_history.evidence_vault_raw_provenance_disposition_events
FOR EACH ROW EXECUTE FUNCTION b3s_history.reject_evidence_vault_raw_provenance_mutation();

CREATE TRIGGER evidence_vault_raw_receipts_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_vault_raw_acquisition_receipts
FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();
CREATE TRIGGER evidence_vault_raw_evidence_bindings_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_vault_raw_evidence_bindings
FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();
CREATE TRIGGER evidence_vault_verified_c7_bindings_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_vault_verified_c7_lineage_bindings
FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();
CREATE TRIGGER evidence_vault_verified_c7_members_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_vault_verified_c7_lineage_members
FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();
CREATE TRIGGER evidence_vault_raw_dispositions_no_truncate
BEFORE TRUNCATE ON b3s_history.evidence_vault_raw_provenance_disposition_events
FOR EACH STATEMENT EXECUTE FUNCTION b3s_history.reject_evidence_vault_truncate();


CREATE FUNCTION b3s_history.append_evidence_vault_raw_acquisition(payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    workspace_row b3s_history.workspaces%ROWTYPE;
    brand_row b3s_history.brands%ROWTYPE;
    scan_row b3s_history.scan_runs%ROWTYPE;
    plan_row b3s_history.evidence_vault_operation_plans%ROWTYPE;
    capture_row b3s_history.captures%ROWTYPE;
    evidence_row b3s_history.evidence_records%ROWTYPE;
    watermark_row b3s_history.evidence_vault_capture_watermark_events%ROWTYPE;
    watermark_head b3s_history.evidence_vault_capture_watermark_events%ROWTYPE;
    receipt b3s_history.evidence_vault_raw_acquisition_receipts%ROWTYPE;
    first_receipt b3s_history.evidence_vault_raw_acquisition_receipts%ROWTYPE;
    raw_binding b3s_history.evidence_vault_raw_evidence_bindings%ROWTYPE;
    receipt_ids jsonb := '[]'::jsonb;
    evidence_binding_ids jsonb := '[]'::jsonb;
    scan_preexisted boolean;
    capture_preexisted boolean;
BEGIN
    IF NOT b3s_history.evidence_vault_json_has_exact_keys(
        payload, ARRAY[
            'workspace', 'brand', 'scan_run', 'operation_plan', 'capture',
            'evidence_records', 'watermark_event', 'receipts', 'evidence_bindings'
        ]
    ) OR jsonb_typeof(payload -> 'evidence_records') IS DISTINCT FROM 'array'
      OR jsonb_array_length(payload -> 'evidence_records') < 1
      OR jsonb_typeof(payload -> 'receipts') IS DISTINCT FROM 'array'
      OR jsonb_typeof(payload -> 'evidence_bindings') IS DISTINCT FROM 'array'
      OR jsonb_array_length(payload -> 'receipts') NOT BETWEEN 1 AND 2
      OR jsonb_array_length(payload -> 'evidence_bindings')
            IS DISTINCT FROM jsonb_array_length(payload -> 'receipts') THEN
        RAISE EXCEPTION 'raw acquisition append envelope is invalid';
    END IF;
    workspace_row := jsonb_populate_record(
        NULL::b3s_history.workspaces, payload -> 'workspace'
    );
    brand_row := jsonb_populate_record(
        NULL::b3s_history.brands, payload -> 'brand'
    );
    scan_row := jsonb_populate_record(
        NULL::b3s_history.scan_runs, payload -> 'scan_run'
    );
    plan_row := jsonb_populate_record(
        NULL::b3s_history.evidence_vault_operation_plans,
        payload -> 'operation_plan'
    );
    capture_row := jsonb_populate_record(
        NULL::b3s_history.captures, payload -> 'capture'
    );
    watermark_row := jsonb_populate_record(
        NULL::b3s_history.evidence_vault_capture_watermark_events,
        payload -> 'watermark_event'
    );
    SELECT * INTO first_receipt
    FROM jsonb_populate_recordset(
        NULL::b3s_history.evidence_vault_raw_acquisition_receipts,
        payload -> 'receipts'
    ) LIMIT 1;
    IF scan_row.workspace_id IS DISTINCT FROM workspace_row.id
       OR brand_row.workspace_id IS DISTINCT FROM workspace_row.id
       OR scan_row.brand_id IS DISTINCT FROM brand_row.id
       OR plan_row.workspace_id IS DISTINCT FROM workspace_row.id
       OR plan_row.brand_id IS DISTINCT FROM brand_row.id
       OR plan_row.scan_run_id IS DISTINCT FROM scan_row.id
       OR capture_row.scan_run_id IS DISTINCT FROM scan_row.id
       OR capture_row.brand_id IS DISTINCT FROM brand_row.id
       OR first_receipt.workspace_id IS DISTINCT FROM workspace_row.id
       OR first_receipt.brand_id IS DISTINCT FROM brand_row.id
       OR first_receipt.scan_run_id IS DISTINCT FROM scan_row.id
       OR first_receipt.capture_id IS DISTINCT FROM capture_row.id THEN
        RAISE EXCEPTION 'raw acquisition base observation identity is inconsistent';
    END IF;
    IF capture_row.content_hash IS DISTINCT FROM encode(sha256(convert_to(
            b3s_history.evidence_vault_canonical_json(capture_row.raw_payload), 'UTF8'
       )), 'hex')
       OR scan_row.metadata ->> 'observation_hash' IS DISTINCT FROM
            encode(sha256(convert_to(
                b3s_history.evidence_vault_canonical_json(
                    COALESCE(scan_row.request_payload, '{}'::jsonb)
                ), 'UTF8'
            )), 'hex')
       OR scan_row.request_payload ->> 'schema_version' IS DISTINCT FROM
            'b3s-capture-observation-v1'
       OR scan_row.request_payload ->> 'source_scan_id' IS DISTINCT FROM
            scan_row.source_scan_id
       OR scan_row.request_payload ->> 'url' IS DISTINCT FROM capture_row.source_url
       OR scan_row.request_payload -> 'capture_payload' IS DISTINCT FROM
            capture_row.raw_payload
       OR (scan_row.request_payload ->> 'observed_at')::timestamptz
            IS DISTINCT FROM capture_row.observed_at
       OR (scan_row.request_payload ->> 'recorded_at')::timestamptz
            IS DISTINCT FROM capture_row.recorded_at
       OR scan_row.recorded_at IS DISTINCT FROM capture_row.recorded_at
       OR scan_row.request_payload ->> 'pipeline_version' IS DISTINCT FROM
            scan_row.pipeline_version
       OR scan_row.request_payload ->> 'acquisition_state' IS DISTINCT FROM
            scan_row.acquisition_state
       OR watermark_row.capture_content_hash IS DISTINCT FROM capture_row.content_hash
       OR watermark_row.capture_observation_hash IS DISTINCT FROM
            scan_row.metadata ->> 'observation_hash'
       OR watermark_row.append_origin IS DISTINCT FROM
            'capture_observation_commit'
       OR plan_row.observation_hash IS DISTINCT FROM
            scan_row.metadata ->> 'observation_hash'
       OR plan_row.mode IS DISTINCT FROM plan_row.plan_payload ->> 'mode'
       OR plan_row.canonical_memory_version IS DISTINCT FROM
            plan_row.plan_payload ->> 'canonical_memory_version'
       OR plan_row.operation_plan_fingerprint IS DISTINCT FROM
            plan_row.plan_payload ->> 'operation_plan_fingerprint'
       OR plan_row.operation_plan_fingerprint IS DISTINCT FROM
            b3s_history.evidence_vault_canonical_fingerprint(
                'evidence-vault-operation-plan-v1',
                plan_row.plan_payload - 'operation_plan_fingerprint'
            )
       OR plan_row.plan_payload ->> 'schema_version' IS DISTINCT FROM
            'evidence-vault-operation-plan-v1'
       OR plan_row.plan_payload ->> 'authority_scope' IS DISTINCT FROM 'b3s-vault'
       OR plan_row.plan_payload -> 'authority' IS DISTINCT FROM 'false'::jsonb
       OR plan_row.plan_payload -> 'runtime_effect' IS DISTINCT FROM 'false'::jsonb
       OR COALESCE(plan_row.authority, false) <> false
       OR COALESCE(plan_row.authority_scope, 'b3s-vault') <> 'b3s-vault'
       OR COALESCE(plan_row.production_runtime_effect, false) <> false
       OR COALESCE(plan_row.scanner_runtime_effect, false) <> false
       THEN
        RAISE EXCEPTION 'prepared capture/observation/operation-plan content is invalid';
    END IF;
    -- Global order used by existing repository capture paths.
    PERFORM pg_advisory_xact_lock(
        b3s_history.evidence_vault_ingest_lock_key(
            scan_row.workspace_id, scan_row.source_scan_id
        )
    );
    PERFORM pg_advisory_xact_lock(
        b3s_history.evidence_vault_brand_lock_key(
            scan_row.workspace_id, scan_row.brand_id
        )
    );
    SELECT EXISTS (
        SELECT 1 FROM b3s_history.scan_runs WHERE id = scan_row.id
    ), EXISTS (
        SELECT 1 FROM b3s_history.captures WHERE id = capture_row.id
    ) INTO scan_preexisted, capture_preexisted;
    IF scan_preexisted OR capture_preexisted THEN
        SELECT * INTO watermark_row
        FROM b3s_history.evidence_vault_capture_watermark_events AS stored
        WHERE stored.brand_id = brand_row.id
          AND stored.capture_id = capture_row.id;
        IF NOT (scan_preexisted AND capture_preexisted)
           OR watermark_row.id IS NULL
           OR watermark_row.capture_content_hash IS DISTINCT FROM capture_row.content_hash
           OR watermark_row.capture_observation_hash IS DISTINCT FROM
                scan_row.metadata ->> 'observation_hash'
           OR NOT EXISTS (
                SELECT 1
                FROM b3s_history.evidence_vault_operation_plans AS stored_plan
                WHERE stored_plan.id = plan_row.id
                  AND stored_plan.workspace_id = plan_row.workspace_id
                  AND stored_plan.brand_id = plan_row.brand_id
                  AND stored_plan.scan_run_id = plan_row.scan_run_id
                  AND stored_plan.operation_plan_fingerprint =
                        plan_row.operation_plan_fingerprint
           )
           OR EXISTS (
                SELECT 1
                FROM jsonb_populate_recordset(
                    NULL::b3s_history.evidence_records,
                    payload -> 'evidence_records'
                ) AS supplied
                WHERE NOT EXISTS (
                    SELECT 1 FROM b3s_history.evidence_records AS stored
                    WHERE stored.id = supplied.id
                )
           )
           OR EXISTS (
                SELECT 1
                FROM jsonb_populate_recordset(
                    NULL::b3s_history.evidence_vault_raw_acquisition_receipts,
                    payload -> 'receipts'
                ) AS supplied
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM b3s_history.evidence_vault_raw_acquisition_receipts AS stored
                    WHERE stored.id = supplied.id
                      AND stored.receipt_fingerprint = supplied.receipt_fingerprint
                )
           )
           OR EXISTS (
                SELECT 1
                FROM jsonb_populate_recordset(
                    NULL::b3s_history.evidence_vault_raw_evidence_bindings,
                    payload -> 'evidence_bindings'
                ) AS supplied
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM b3s_history.evidence_vault_raw_evidence_bindings AS stored
                    WHERE stored.id = supplied.id
                      AND stored.binding_fingerprint = supplied.binding_fingerprint
                )
           )
           OR (SELECT array_agg(
                    supplied.receipt_fingerprint ORDER BY supplied.receipt_fingerprint
               )
               FROM jsonb_populate_recordset(
                    NULL::b3s_history.evidence_vault_raw_acquisition_receipts,
                    payload -> 'receipts'
               ) AS supplied)
              IS DISTINCT FROM (
                SELECT array_agg(
                    signed.value ->> 'receipt_fingerprint'
                    ORDER BY signed.value ->> 'receipt_fingerprint' COLLATE "C"
                )
                FROM b3s_history.captures AS durable_capture
                CROSS JOIN LATERAL jsonb_array_elements(
                    durable_capture.raw_payload #>
                        '{evidence_vault_raw_provenance,signed_receipts}'
                ) AS signed(value)
                WHERE durable_capture.id = capture_row.id
              )
           OR (SELECT array_agg(
                    supplied.receipt_fingerprint ORDER BY supplied.receipt_fingerprint
               )
               FROM jsonb_populate_recordset(
                    NULL::b3s_history.evidence_vault_raw_acquisition_receipts,
                    payload -> 'receipts'
               ) AS supplied)
              IS DISTINCT FROM (
                SELECT array_agg(
                    stored_receipt.receipt_fingerprint
                    ORDER BY stored_receipt.receipt_fingerprint
                )
                FROM b3s_history.evidence_vault_raw_acquisition_receipts AS stored_receipt
                WHERE stored_receipt.capture_id = capture_row.id
              )
           OR (SELECT array_agg(
                    supplied.binding_fingerprint ORDER BY supplied.binding_fingerprint
               )
               FROM jsonb_populate_recordset(
                    NULL::b3s_history.evidence_vault_raw_evidence_bindings,
                    payload -> 'evidence_bindings'
               ) AS supplied)
              IS DISTINCT FROM (
                SELECT array_agg(
                    stored_binding.binding_fingerprint
                    ORDER BY stored_binding.binding_fingerprint
                )
                FROM b3s_history.evidence_vault_raw_evidence_bindings AS stored_binding
                WHERE stored_binding.capture_id = capture_row.id
              ) THEN
            RAISE EXCEPTION
                'pre-existing scan/capture cannot be upgraded with raw provenance';
        END IF;
    ELSE
        IF plan_row.status NOT IN ('pending', 'not_required')
           OR COALESCE(plan_row.attempt_count, 0) <> 0
           OR plan_row.lease_owner IS NOT NULL
           OR plan_row.lease_token IS NOT NULL
           OR COALESCE(plan_row.lease_generation, 0) <> 0
           OR plan_row.lease_expires_at IS NOT NULL
           OR plan_row.claimed_at IS NOT NULL
           OR plan_row.started_at IS NOT NULL
           OR plan_row.heartbeat_at IS NOT NULL
           OR plan_row.result_fingerprint IS NOT NULL
           OR plan_row.result_payload IS NOT NULL
           OR plan_row.result_persisted_at IS NOT NULL
           OR plan_row.candidate_packet_fingerprint IS NOT NULL
           OR plan_row.completed_at IS NOT NULL
           OR plan_row.superseded_at IS NOT NULL
           OR COALESCE(plan_row.last_error, '') <> '' THEN
            RAISE EXCEPTION
                'new raw acquisition operation plan is not pre-interpretation';
        END IF;
        SELECT * INTO watermark_head
        FROM b3s_history.evidence_vault_capture_watermark_events AS stored
        WHERE stored.brand_id = brand_row.id
        ORDER BY stored.capture_sequence DESC
        LIMIT 1;
        watermark_row.brand_id := brand_row.id;
        watermark_row.capture_id := capture_row.id;
        watermark_row.capture_sequence :=
            COALESCE(watermark_head.capture_sequence, 0) + 1;
        watermark_row.previous_event_id := watermark_head.id;
        watermark_row.previous_event_fingerprint := watermark_head.event_fingerprint;
        watermark_row.event_schema_version :=
            'evidence-vault-capture-watermark-event-v1';
        watermark_row.event_fingerprint :=
            b3s_history.evidence_vault_canonical_fingerprint(
                'evidence-vault-capture-watermark-event-v1',
                jsonb_build_object(
                    'schema_version', watermark_row.event_schema_version,
                    'brand_id', watermark_row.brand_id::text,
                    'capture_id', watermark_row.capture_id::text,
                    'capture_sequence', watermark_row.capture_sequence,
                    'previous_event_id', CASE
                        WHEN watermark_row.previous_event_id IS NULL THEN NULL
                        ELSE to_jsonb(watermark_row.previous_event_id::text)
                    END,
                    'previous_event_fingerprint', CASE
                        WHEN watermark_row.previous_event_fingerprint IS NULL THEN NULL
                        ELSE to_jsonb(watermark_row.previous_event_fingerprint)
                    END,
                    'capture_content_hash', watermark_row.capture_content_hash,
                    'capture_observation_hash',
                        watermark_row.capture_observation_hash,
                    'append_origin', watermark_row.append_origin
                )
            );
        watermark_row.id :=
            substr(watermark_row.event_fingerprint, 1, 32)::uuid;
    END IF;

    INSERT INTO b3s_history.workspaces (id, slug, name)
    VALUES (workspace_row.id, workspace_row.slug, workspace_row.name)
    ON CONFLICT (id) DO NOTHING;
    IF NOT EXISTS (
        SELECT 1 FROM b3s_history.workspaces AS stored
        WHERE stored.id = workspace_row.id
          AND stored.slug IS NOT DISTINCT FROM workspace_row.slug
          AND stored.name IS NOT DISTINCT FROM workspace_row.name
    ) THEN RAISE EXCEPTION 'workspace replay diverges from stored identity'; END IF;

    INSERT INTO b3s_history.brands (
        id, workspace_id, canonical_domain, display_name, canonical_url,
        first_observed_at, latest_observed_at
    ) VALUES (
        brand_row.id, brand_row.workspace_id, brand_row.canonical_domain,
        brand_row.display_name, brand_row.canonical_url,
        brand_row.first_observed_at, brand_row.latest_observed_at
    ) ON CONFLICT (workspace_id, canonical_domain) DO UPDATE SET
        display_name = CASE
            WHEN EXCLUDED.latest_observed_at >=
                    b3s_history.brands.latest_observed_at
            THEN EXCLUDED.display_name
            ELSE b3s_history.brands.display_name
        END,
        canonical_url = CASE
            WHEN EXCLUDED.latest_observed_at >=
                    b3s_history.brands.latest_observed_at
            THEN EXCLUDED.canonical_url
            ELSE b3s_history.brands.canonical_url
        END,
        first_observed_at = LEAST(
            b3s_history.brands.first_observed_at,
            EXCLUDED.first_observed_at
        ),
        latest_observed_at = GREATEST(
            b3s_history.brands.latest_observed_at,
            EXCLUDED.latest_observed_at
        ),
        updated_at = now();
    IF NOT EXISTS (
        SELECT 1 FROM b3s_history.brands AS stored
        WHERE stored.id = brand_row.id
          AND stored.workspace_id IS NOT DISTINCT FROM brand_row.workspace_id
          AND stored.canonical_domain IS NOT DISTINCT FROM brand_row.canonical_domain
    ) THEN RAISE EXCEPTION 'brand replay diverges from stored identity'; END IF;

    INSERT INTO b3s_history.scan_runs (
        id, workspace_id, brand_id, source_scan_id, source_run_id, status,
        pipeline_version, acquisition_state, requested_at, started_at,
        completed_at, recorded_at, error_summary, request_payload, metadata
    ) VALUES (
        scan_row.id, scan_row.workspace_id, scan_row.brand_id,
        scan_row.source_scan_id, COALESCE(scan_row.source_run_id, ''),
        scan_row.status, scan_row.pipeline_version,
        COALESCE(scan_row.acquisition_state, 'unknown'), scan_row.requested_at,
        scan_row.started_at, scan_row.completed_at, scan_row.recorded_at,
        COALESCE(scan_row.error_summary, ''),
        COALESCE(scan_row.request_payload, '{}'::jsonb),
        COALESCE(scan_row.metadata, '{}'::jsonb)
    ) ON CONFLICT (id) DO NOTHING;
    IF NOT EXISTS (
        SELECT 1 FROM b3s_history.scan_runs AS stored
        WHERE stored.id = scan_row.id
          AND stored.workspace_id IS NOT DISTINCT FROM scan_row.workspace_id
          AND stored.brand_id IS NOT DISTINCT FROM scan_row.brand_id
          AND stored.source_scan_id IS NOT DISTINCT FROM scan_row.source_scan_id
          AND stored.source_run_id IS NOT DISTINCT FROM COALESCE(scan_row.source_run_id, '')
          AND stored.status IS NOT DISTINCT FROM scan_row.status
          AND stored.pipeline_version IS NOT DISTINCT FROM scan_row.pipeline_version
          AND stored.acquisition_state IS NOT DISTINCT FROM COALESCE(scan_row.acquisition_state, 'unknown')
          AND stored.requested_at IS NOT DISTINCT FROM scan_row.requested_at
          AND stored.started_at IS NOT DISTINCT FROM scan_row.started_at
          AND stored.completed_at IS NOT DISTINCT FROM scan_row.completed_at
          AND stored.recorded_at IS NOT DISTINCT FROM scan_row.recorded_at
          AND stored.error_summary IS NOT DISTINCT FROM COALESCE(scan_row.error_summary, '')
          AND stored.request_payload IS NOT DISTINCT FROM COALESCE(scan_row.request_payload, '{}'::jsonb)
          AND stored.metadata IS NOT DISTINCT FROM
                COALESCE(scan_row.metadata, '{}'::jsonb)
    ) THEN RAISE EXCEPTION 'scan replay diverges from stored immutable identity'; END IF;

    INSERT INTO b3s_history.evidence_vault_operation_plans (
        id, workspace_id, brand_id, scan_run_id, observation_hash,
        operation_plan_fingerprint, canonical_memory_version, mode, status,
        plan_payload, authority, authority_scope, production_runtime_effect,
        scanner_runtime_effect
    ) VALUES (
        plan_row.id, plan_row.workspace_id, plan_row.brand_id,
        plan_row.scan_run_id, plan_row.observation_hash,
        plan_row.operation_plan_fingerprint, plan_row.canonical_memory_version,
        plan_row.mode, plan_row.status, plan_row.plan_payload,
        false, 'b3s-vault', false, false
    ) ON CONFLICT (id) DO NOTHING;
    IF NOT EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_operation_plans AS stored
        WHERE stored.id = plan_row.id
          AND stored.workspace_id IS NOT DISTINCT FROM plan_row.workspace_id
          AND stored.brand_id IS NOT DISTINCT FROM plan_row.brand_id
          AND stored.scan_run_id IS NOT DISTINCT FROM plan_row.scan_run_id
          AND stored.observation_hash IS NOT DISTINCT FROM plan_row.observation_hash
          AND stored.operation_plan_fingerprint IS NOT DISTINCT FROM
                plan_row.operation_plan_fingerprint
          AND stored.canonical_memory_version IS NOT DISTINCT FROM
                plan_row.canonical_memory_version
          AND stored.mode IS NOT DISTINCT FROM plan_row.mode
          AND stored.plan_payload IS NOT DISTINCT FROM plan_row.plan_payload
          -- Lifecycle columns are database-owned mutable execution state. Exact
          -- replay compares only the immutable parent contract and never resets
          -- a claimed/completed/failed plan to the caller's initial status.
          AND stored.authority IS NOT DISTINCT FROM COALESCE(plan_row.authority, false)
          AND stored.authority_scope IS NOT DISTINCT FROM COALESCE(plan_row.authority_scope, 'b3s-vault')
          AND stored.production_runtime_effect IS NOT DISTINCT FROM COALESCE(plan_row.production_runtime_effect, false)
          AND stored.scanner_runtime_effect IS NOT DISTINCT FROM COALESCE(plan_row.scanner_runtime_effect, false)
    ) THEN
        RAISE EXCEPTION 'operation plan replay diverges from immutable stored content';
    END IF;

    INSERT INTO b3s_history.captures (
        id, scan_run_id, brand_id, observed_at, recorded_at, source_url,
        content_hash, acquisition_summary, limitations, raw_payload
    ) VALUES (
        capture_row.id, capture_row.scan_run_id, capture_row.brand_id,
        capture_row.observed_at, capture_row.recorded_at, capture_row.source_url,
        capture_row.content_hash,
        COALESCE(capture_row.acquisition_summary, '{}'::jsonb),
        COALESCE(capture_row.limitations, '[]'::jsonb), capture_row.raw_payload
    ) ON CONFLICT (id) DO NOTHING;
    IF NOT EXISTS (
        SELECT 1 FROM b3s_history.captures AS stored
        WHERE stored.id = capture_row.id
          AND stored.scan_run_id IS NOT DISTINCT FROM capture_row.scan_run_id
          AND stored.brand_id IS NOT DISTINCT FROM capture_row.brand_id
          AND stored.observed_at IS NOT DISTINCT FROM capture_row.observed_at
          AND stored.recorded_at IS NOT DISTINCT FROM capture_row.recorded_at
          AND stored.source_url IS NOT DISTINCT FROM capture_row.source_url
          AND stored.content_hash IS NOT DISTINCT FROM capture_row.content_hash
          AND stored.acquisition_summary IS NOT DISTINCT FROM COALESCE(capture_row.acquisition_summary, '{}'::jsonb)
          AND stored.limitations IS NOT DISTINCT FROM COALESCE(capture_row.limitations, '[]'::jsonb)
          AND stored.raw_payload IS NOT DISTINCT FROM capture_row.raw_payload
    ) THEN RAISE EXCEPTION 'capture replay diverges from immutable stored content'; END IF;

    FOR evidence_row IN SELECT * FROM jsonb_populate_recordset(
        NULL::b3s_history.evidence_records, payload -> 'evidence_records'
    ) LOOP
        IF evidence_row.capture_id IS DISTINCT FROM capture_row.id THEN
            RAISE EXCEPTION 'raw acquisition evidence crosses its capture parent';
        END IF;
        IF evidence_row.content_hash IS DISTINCT FROM encode(
            sha256(convert_to(evidence_row.content, 'UTF8')), 'hex'
        ) THEN
            RAISE EXCEPTION 'prepared evidence content hash is invalid';
        END IF;
        IF EXISTS (
            SELECT 1 FROM b3s_history.evidence_records WHERE id = evidence_row.id
        ) THEN
            IF NOT EXISTS (
                SELECT 1 FROM b3s_history.evidence_records AS stored
                WHERE stored.id = evidence_row.id
                  AND stored.capture_id IS NOT DISTINCT FROM evidence_row.capture_id
                  AND stored.evidence_ref IS NOT DISTINCT FROM evidence_row.evidence_ref
                  AND stored.url IS NOT DISTINCT FROM COALESCE(evidence_row.url, '')
                  AND stored.content IS NOT DISTINCT FROM evidence_row.content
                  AND stored.source IS NOT DISTINCT FROM evidence_row.source
                  AND stored.source_class IS NOT DISTINCT FROM COALESCE(evidence_row.source_class, 'other')
                  AND stored.evidence_type IS NOT DISTINCT FROM evidence_row.evidence_type
                  AND stored.content_raw IS NOT DISTINCT FROM evidence_row.content_raw
                  AND stored.content_hash IS NOT DISTINCT FROM evidence_row.content_hash
                  AND stored.confidence IS NOT DISTINCT FROM COALESCE(evidence_row.confidence, 'medium')
                  AND stored.metadata IS NOT DISTINCT FROM COALESCE(evidence_row.metadata, '{}'::jsonb)
            ) THEN RAISE EXCEPTION 'evidence replay diverges from immutable stored content'; END IF;
        ELSE
            INSERT INTO b3s_history.evidence_records (
                id, capture_id, evidence_ref, source, source_class, evidence_type,
                url, content, content_raw, content_hash, confidence, metadata
            ) VALUES (
                evidence_row.id, evidence_row.capture_id, evidence_row.evidence_ref,
                evidence_row.source, COALESCE(evidence_row.source_class, 'other'),
                evidence_row.evidence_type, COALESCE(evidence_row.url, ''),
                evidence_row.content, evidence_row.content_raw,
                evidence_row.content_hash, COALESCE(evidence_row.confidence, 'medium'),
                COALESCE(evidence_row.metadata, '{}'::jsonb)
            );
        END IF;
    END LOOP;
    IF jsonb_typeof(scan_row.request_payload -> 'evidence_records')
            IS DISTINCT FROM 'array'
       OR jsonb_array_length(scan_row.request_payload -> 'evidence_records')
            IS DISTINCT FROM jsonb_array_length(payload -> 'evidence_records')
       OR (SELECT count(*) FROM b3s_history.evidence_records AS durable
           WHERE durable.capture_id = capture_row.id)
            IS DISTINCT FROM jsonb_array_length(payload -> 'evidence_records')::bigint
       OR EXISTS (
            SELECT 1
            FROM jsonb_array_elements(
                scan_row.request_payload -> 'evidence_records'
            ) AS requested(value)
            LEFT JOIN b3s_history.evidence_records AS durable
              ON durable.capture_id = capture_row.id
             AND durable.evidence_ref = requested.value ->> 'ref'
            WHERE durable.id IS NULL
               OR durable.source IS DISTINCT FROM requested.value ->> 'source'
               OR durable.source_class IS DISTINCT FROM COALESCE(
                    requested.value #>> '{metadata,source_class}', 'other'
               )
               OR durable.evidence_type IS DISTINCT FROM
                    requested.value ->> 'evidence_type'
               OR durable.url IS DISTINCT FROM COALESCE(
                    requested.value ->> 'url', ''
               )
               OR durable.content IS DISTINCT FROM requested.value ->> 'content'
               OR durable.confidence IS DISTINCT FROM COALESCE(
                    requested.value ->> 'confidence', 'medium'
               )
               OR durable.metadata IS DISTINCT FROM COALESCE(
                    requested.value -> 'metadata', '{}'::jsonb
               )
       ) THEN
        RAISE EXCEPTION 'durable capture evidence set differs from raw observation';
    END IF;

    IF EXISTS (
        SELECT 1 FROM b3s_history.evidence_vault_capture_watermark_events
        WHERE id = watermark_row.id
    ) THEN
        IF NOT EXISTS (
            SELECT 1
            FROM b3s_history.evidence_vault_capture_watermark_events AS stored
            WHERE stored.id = watermark_row.id
              AND stored.brand_id IS NOT DISTINCT FROM watermark_row.brand_id
              AND stored.capture_id IS NOT DISTINCT FROM watermark_row.capture_id
              AND stored.capture_sequence IS NOT DISTINCT FROM watermark_row.capture_sequence
              AND stored.previous_event_id IS NOT DISTINCT FROM watermark_row.previous_event_id
              AND stored.previous_event_fingerprint IS NOT DISTINCT FROM watermark_row.previous_event_fingerprint
              AND stored.capture_content_hash IS NOT DISTINCT FROM watermark_row.capture_content_hash
              AND stored.capture_observation_hash IS NOT DISTINCT FROM watermark_row.capture_observation_hash
              AND stored.append_origin IS NOT DISTINCT FROM watermark_row.append_origin
              AND stored.event_schema_version IS NOT DISTINCT FROM watermark_row.event_schema_version
              AND stored.event_fingerprint IS NOT DISTINCT FROM watermark_row.event_fingerprint
        ) THEN RAISE EXCEPTION 'watermark replay diverges from immutable stored content'; END IF;
    ELSE
        INSERT INTO b3s_history.evidence_vault_capture_watermark_events (
            id, brand_id, capture_id, capture_sequence, previous_event_id,
            previous_event_fingerprint, capture_content_hash,
            capture_observation_hash, append_origin, event_schema_version,
            event_fingerprint
        ) VALUES (
            watermark_row.id, watermark_row.brand_id, watermark_row.capture_id,
            watermark_row.capture_sequence, watermark_row.previous_event_id,
            watermark_row.previous_event_fingerprint,
            watermark_row.capture_content_hash,
            watermark_row.capture_observation_hash, watermark_row.append_origin,
            watermark_row.event_schema_version, watermark_row.event_fingerprint
        );
    END IF;

    FOR receipt IN SELECT * FROM jsonb_populate_recordset(
        NULL::b3s_history.evidence_vault_raw_acquisition_receipts,
        payload -> 'receipts'
    ) LOOP
        receipt.watermark_event_id := watermark_row.id;
        receipt.capture_sequence := watermark_row.capture_sequence;
        INSERT INTO b3s_history.evidence_vault_raw_acquisition_receipts (
            id, workspace_id, brand_id, scan_run_id, source_scan_id, capture_id,
            watermark_event_id, capture_sequence, receipt_schema_version,
            freshness_policy_version, signature_schema_version, key_id,
            receipt_nonce, acquisition_session_id, workspace_slug, canonical_brand,
            channel_role, provider, acquisition_mode, pre_receipt_snapshot_sha256,
            source_url, raw_fragment_pointer, raw_fragment_sha256,
            extracted_document_sha256, extractor_version,
            external_identity_provenance_fingerprint, external_identity_provenance,
            fetched_at, eligible_until, claims, receipt_fingerprint,
            signed_payload, signature
        ) VALUES (
            receipt.id, receipt.workspace_id, receipt.brand_id, receipt.scan_run_id,
            receipt.source_scan_id, receipt.capture_id, receipt.watermark_event_id,
            receipt.capture_sequence, receipt.receipt_schema_version,
            receipt.freshness_policy_version, receipt.signature_schema_version,
            receipt.key_id, receipt.receipt_nonce, receipt.acquisition_session_id,
            receipt.workspace_slug, receipt.canonical_brand, receipt.channel_role,
            receipt.provider, receipt.acquisition_mode,
            receipt.pre_receipt_snapshot_sha256, receipt.source_url,
            receipt.raw_fragment_pointer, receipt.raw_fragment_sha256,
            receipt.extracted_document_sha256, receipt.extractor_version,
            receipt.external_identity_provenance_fingerprint,
            receipt.external_identity_provenance, receipt.fetched_at,
            receipt.eligible_until, receipt.claims, receipt.receipt_fingerprint,
            receipt.signed_payload, receipt.signature
        ) ON CONFLICT (receipt_fingerprint) DO NOTHING;
        -- received_at, eligible_until, and created_at are derived by the
        -- database receipt trigger; every caller-owned stored column is exact.
        IF NOT EXISTS (
            SELECT 1 FROM b3s_history.evidence_vault_raw_acquisition_receipts AS stored
            WHERE stored.receipt_fingerprint = receipt.receipt_fingerprint
              AND stored.id IS NOT DISTINCT FROM receipt.id
              AND stored.workspace_id IS NOT DISTINCT FROM receipt.workspace_id
              AND stored.brand_id IS NOT DISTINCT FROM receipt.brand_id
              AND stored.scan_run_id IS NOT DISTINCT FROM receipt.scan_run_id
              AND stored.source_scan_id IS NOT DISTINCT FROM receipt.source_scan_id
              AND stored.capture_id IS NOT DISTINCT FROM receipt.capture_id
              AND stored.watermark_event_id IS NOT DISTINCT FROM receipt.watermark_event_id
              AND stored.capture_sequence IS NOT DISTINCT FROM receipt.capture_sequence
              AND stored.key_id IS NOT DISTINCT FROM receipt.key_id
              AND stored.receipt_nonce IS NOT DISTINCT FROM receipt.receipt_nonce
              AND stored.acquisition_session_id IS NOT DISTINCT FROM receipt.acquisition_session_id
              AND stored.receipt_schema_version IS NOT DISTINCT FROM receipt.receipt_schema_version
              AND stored.freshness_policy_version IS NOT DISTINCT FROM receipt.freshness_policy_version
              AND stored.signature_schema_version IS NOT DISTINCT FROM receipt.signature_schema_version
              AND stored.workspace_slug IS NOT DISTINCT FROM receipt.workspace_slug
              AND stored.canonical_brand IS NOT DISTINCT FROM receipt.canonical_brand
              AND stored.channel_role IS NOT DISTINCT FROM receipt.channel_role
              AND stored.provider IS NOT DISTINCT FROM receipt.provider
              AND stored.acquisition_mode IS NOT DISTINCT FROM receipt.acquisition_mode
              AND stored.pre_receipt_snapshot_sha256 IS NOT DISTINCT FROM receipt.pre_receipt_snapshot_sha256
              AND stored.source_url IS NOT DISTINCT FROM receipt.source_url
              AND stored.raw_fragment_pointer IS NOT DISTINCT FROM receipt.raw_fragment_pointer
              AND stored.raw_fragment_sha256 IS NOT DISTINCT FROM receipt.raw_fragment_sha256
              AND stored.extracted_document_sha256 IS NOT DISTINCT FROM receipt.extracted_document_sha256
              AND stored.extractor_version IS NOT DISTINCT FROM receipt.extractor_version
              AND stored.external_identity_provenance_fingerprint IS NOT DISTINCT FROM
                    receipt.external_identity_provenance_fingerprint
              AND stored.fetched_at IS NOT DISTINCT FROM receipt.fetched_at
              AND stored.claims IS NOT DISTINCT FROM receipt.claims
              AND stored.signed_payload IS NOT DISTINCT FROM receipt.signed_payload
              AND stored.signature IS NOT DISTINCT FROM receipt.signature
              AND stored.external_identity_provenance IS NOT DISTINCT FROM
                    receipt.external_identity_provenance
        ) THEN RAISE EXCEPTION 'raw receipt replay diverges from immutable stored content'; END IF;
        receipt_ids := receipt_ids || to_jsonb(receipt.id::text);
    END LOOP;

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
            SELECT 1 FROM b3s_history.evidence_vault_raw_evidence_bindings AS stored
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
        ) THEN RAISE EXCEPTION 'raw evidence binding replay diverges from immutable stored content'; END IF;
        evidence_binding_ids := evidence_binding_ids || to_jsonb(raw_binding.id::text);
    END LOOP;
    RETURN jsonb_build_object(
        'capture_id', capture_row.id,
        'operation_plan_id', plan_row.id,
        'operation_plan_fingerprint', plan_row.operation_plan_fingerprint,
        'watermark_event_id', watermark_row.id,
        'capture_sequence', watermark_row.capture_sequence,
        'previous_event_id', watermark_row.previous_event_id,
        'previous_event_fingerprint', watermark_row.previous_event_fingerprint,
        'watermark_event_fingerprint', watermark_row.event_fingerprint,
        'receipt_ids', receipt_ids,
        'evidence_binding_ids', evidence_binding_ids
    );
END;
$$;

CREATE FUNCTION b3s_history.bind_evidence_vault_verified_c7_lineage(payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    receipt b3s_history.evidence_vault_raw_acquisition_receipts%ROWTYPE;
    first_receipt b3s_history.evidence_vault_raw_acquisition_receipts%ROWTYPE;
    raw_binding b3s_history.evidence_vault_raw_evidence_bindings%ROWTYPE;
    verified_binding b3s_history.evidence_vault_verified_c7_lineage_bindings%ROWTYPE;
    member b3s_history.evidence_vault_verified_c7_lineage_members%ROWTYPE;
    receipt_ids jsonb := '[]'::jsonb;
    evidence_binding_ids jsonb := '[]'::jsonb;
    initial_idempotency text;
    initial_event_fingerprint text;
BEGIN
    IF NOT b3s_history.evidence_vault_json_has_exact_keys(
        payload, ARRAY['receipts', 'evidence_bindings', 'verified_binding', 'members']
    ) OR jsonb_typeof(payload -> 'receipts') IS DISTINCT FROM 'array'
      OR jsonb_typeof(payload -> 'evidence_bindings') IS DISTINCT FROM 'array'
      OR jsonb_typeof(payload -> 'members') IS DISTINCT FROM 'array'
      OR jsonb_array_length(payload -> 'receipts') IS DISTINCT FROM 2
      OR jsonb_array_length(payload -> 'evidence_bindings') IS DISTINCT FROM 2
      OR jsonb_array_length(payload -> 'members') IS DISTINCT FROM 2
      OR (SELECT count(DISTINCT supplied.receipt_fingerprint)
          FROM jsonb_populate_recordset(
              NULL::b3s_history.evidence_vault_raw_acquisition_receipts,
              payload -> 'receipts'
          ) AS supplied) IS DISTINCT FROM 2::bigint
      OR (SELECT count(DISTINCT supplied.binding_fingerprint)
          FROM jsonb_populate_recordset(
              NULL::b3s_history.evidence_vault_raw_evidence_bindings,
              payload -> 'evidence_bindings'
          ) AS supplied) IS DISTINCT FROM 2::bigint
      OR (SELECT count(DISTINCT supplied.member_fingerprint)
          FROM jsonb_populate_recordset(
              NULL::b3s_history.evidence_vault_verified_c7_lineage_members,
              payload -> 'members'
          ) AS supplied) IS DISTINCT FROM 2::bigint
      OR (SELECT array_agg(DISTINCT supplied.channel_role ORDER BY supplied.channel_role)
          FROM jsonb_populate_recordset(
              NULL::b3s_history.evidence_vault_verified_c7_lineage_members,
              payload -> 'members'
          ) AS supplied) IS DISTINCT FROM
            ARRAY['external_social_profile', 'owned_web']::text[] THEN
        RAISE EXCEPTION 'raw provenance ingest envelope is invalid';
    END IF;
    SELECT * INTO first_receipt
    FROM jsonb_populate_recordset(
        NULL::b3s_history.evidence_vault_raw_acquisition_receipts,
        payload -> 'receipts'
    ) LIMIT 1;
    verified_binding := jsonb_populate_record(
        NULL::b3s_history.evidence_vault_verified_c7_lineage_bindings,
        payload -> 'verified_binding'
    );
    -- Global order: source-scan/idempotency lock, then canonical-brand lock.
    PERFORM pg_advisory_xact_lock(
        b3s_history.evidence_vault_ingest_lock_key(
            first_receipt.workspace_id, first_receipt.source_scan_id
        )
    );
    PERFORM pg_advisory_xact_lock(
        b3s_history.evidence_vault_brand_lock_key(
            first_receipt.workspace_id, first_receipt.brand_id
        )
    );

    FOR receipt IN SELECT * FROM jsonb_populate_recordset(
        NULL::b3s_history.evidence_vault_raw_acquisition_receipts,
        payload -> 'receipts'
    ) LOOP
        INSERT INTO b3s_history.evidence_vault_raw_acquisition_receipts (
            id, workspace_id, brand_id, scan_run_id, source_scan_id, capture_id,
            watermark_event_id, capture_sequence, receipt_schema_version,
            freshness_policy_version, signature_schema_version, key_id,
            receipt_nonce, acquisition_session_id, workspace_slug, canonical_brand,
            channel_role, provider, acquisition_mode, pre_receipt_snapshot_sha256,
            source_url, raw_fragment_pointer, raw_fragment_sha256,
            extracted_document_sha256, extractor_version,
            external_identity_provenance_fingerprint, external_identity_provenance,
            fetched_at, eligible_until,
            claims, receipt_fingerprint, signed_payload, signature
        ) VALUES (
            receipt.id, receipt.workspace_id, receipt.brand_id, receipt.scan_run_id,
            receipt.source_scan_id, receipt.capture_id, receipt.watermark_event_id,
            receipt.capture_sequence, receipt.receipt_schema_version,
            receipt.freshness_policy_version, receipt.signature_schema_version,
            receipt.key_id, receipt.receipt_nonce, receipt.acquisition_session_id,
            receipt.workspace_slug, receipt.canonical_brand, receipt.channel_role,
            receipt.provider, receipt.acquisition_mode,
            receipt.pre_receipt_snapshot_sha256, receipt.source_url,
            receipt.raw_fragment_pointer, receipt.raw_fragment_sha256,
            receipt.extracted_document_sha256, receipt.extractor_version,
            receipt.external_identity_provenance_fingerprint,
            receipt.external_identity_provenance, receipt.fetched_at,
            receipt.eligible_until, receipt.claims, receipt.receipt_fingerprint,
            receipt.signed_payload, receipt.signature
        ) ON CONFLICT (receipt_fingerprint) DO NOTHING;
        IF NOT EXISTS (
            SELECT 1
            FROM b3s_history.evidence_vault_raw_acquisition_receipts AS stored
            WHERE stored.receipt_fingerprint = receipt.receipt_fingerprint
              AND stored.id IS NOT DISTINCT FROM receipt.id
              AND stored.workspace_id IS NOT DISTINCT FROM receipt.workspace_id
              AND stored.brand_id IS NOT DISTINCT FROM receipt.brand_id
              AND stored.scan_run_id IS NOT DISTINCT FROM receipt.scan_run_id
              AND stored.source_scan_id IS NOT DISTINCT FROM receipt.source_scan_id
              AND stored.capture_id IS NOT DISTINCT FROM receipt.capture_id
              AND stored.watermark_event_id IS NOT DISTINCT FROM receipt.watermark_event_id
              AND stored.capture_sequence IS NOT DISTINCT FROM receipt.capture_sequence
              AND stored.key_id IS NOT DISTINCT FROM receipt.key_id
              AND stored.receipt_nonce IS NOT DISTINCT FROM receipt.receipt_nonce
              AND stored.acquisition_session_id IS NOT DISTINCT FROM receipt.acquisition_session_id
              AND stored.receipt_schema_version IS NOT DISTINCT FROM receipt.receipt_schema_version
              AND stored.freshness_policy_version IS NOT DISTINCT FROM receipt.freshness_policy_version
              AND stored.signature_schema_version IS NOT DISTINCT FROM receipt.signature_schema_version
              AND stored.workspace_slug IS NOT DISTINCT FROM receipt.workspace_slug
              AND stored.canonical_brand IS NOT DISTINCT FROM receipt.canonical_brand
              AND stored.channel_role IS NOT DISTINCT FROM receipt.channel_role
              AND stored.provider IS NOT DISTINCT FROM receipt.provider
              AND stored.acquisition_mode IS NOT DISTINCT FROM receipt.acquisition_mode
              AND stored.pre_receipt_snapshot_sha256 IS NOT DISTINCT FROM receipt.pre_receipt_snapshot_sha256
              AND stored.source_url IS NOT DISTINCT FROM receipt.source_url
              AND stored.raw_fragment_pointer IS NOT DISTINCT FROM receipt.raw_fragment_pointer
              AND stored.raw_fragment_sha256 IS NOT DISTINCT FROM receipt.raw_fragment_sha256
              AND stored.extracted_document_sha256 IS NOT DISTINCT FROM receipt.extracted_document_sha256
              AND stored.extractor_version IS NOT DISTINCT FROM receipt.extractor_version
              AND stored.external_identity_provenance_fingerprint IS NOT DISTINCT FROM receipt.external_identity_provenance_fingerprint
              AND stored.fetched_at IS NOT DISTINCT FROM receipt.fetched_at
              AND stored.eligible_until IS NOT DISTINCT FROM receipt.eligible_until
              AND stored.claims IS NOT DISTINCT FROM receipt.claims
              AND stored.signed_payload IS NOT DISTINCT FROM receipt.signed_payload
              AND stored.signature IS NOT DISTINCT FROM receipt.signature
              AND stored.external_identity_provenance IS NOT DISTINCT FROM
                    receipt.external_identity_provenance
        ) THEN
            RAISE EXCEPTION 'raw receipt replay diverges from immutable stored content';
        END IF;
        receipt_ids := receipt_ids || to_jsonb(receipt.id::text);
    END LOOP;

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
        ) THEN
            RAISE EXCEPTION 'raw evidence binding replay diverges from immutable stored content';
        END IF;
        evidence_binding_ids := evidence_binding_ids || to_jsonb(raw_binding.id::text);
    END LOOP;

    INSERT INTO b3s_history.evidence_vault_verified_c7_lineage_bindings (
        id, workspace_id, brand_id, scan_run_id, capture_id, watermark_event_id,
        capture_sequence, operational_source_packet_id, operation_plan_id,
        composite_group_id, canonical_brand, source_packet_fingerprint,
        operation_plan_fingerprint, exact_artifact_fingerprint,
        freshness_policy_version, public_key_registry_fingerprint,
        provenance, receipt_set_fingerprint, member_set_fingerprint, eligible_until, binding_schema_version,
        binding_fingerprint
    ) VALUES (
        verified_binding.id, verified_binding.workspace_id,
        verified_binding.brand_id, verified_binding.scan_run_id,
        verified_binding.capture_id, verified_binding.watermark_event_id,
        verified_binding.capture_sequence,
        verified_binding.operational_source_packet_id,
        verified_binding.operation_plan_id, verified_binding.composite_group_id,
        verified_binding.canonical_brand,
        verified_binding.source_packet_fingerprint,
        verified_binding.operation_plan_fingerprint,
        verified_binding.exact_artifact_fingerprint,
        verified_binding.freshness_policy_version,
        verified_binding.public_key_registry_fingerprint,
        verified_binding.provenance, verified_binding.receipt_set_fingerprint,
        verified_binding.member_set_fingerprint, verified_binding.eligible_until,
        verified_binding.binding_schema_version,
        verified_binding.binding_fingerprint
    ) ON CONFLICT (binding_fingerprint) DO NOTHING;
    IF NOT EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_verified_c7_lineage_bindings AS stored
        WHERE stored.binding_fingerprint = verified_binding.binding_fingerprint
          AND stored.id IS NOT DISTINCT FROM verified_binding.id
          AND stored.workspace_id IS NOT DISTINCT FROM verified_binding.workspace_id
          AND stored.brand_id IS NOT DISTINCT FROM verified_binding.brand_id
          AND stored.scan_run_id IS NOT DISTINCT FROM verified_binding.scan_run_id
          AND stored.capture_id IS NOT DISTINCT FROM verified_binding.capture_id
          AND stored.watermark_event_id IS NOT DISTINCT FROM verified_binding.watermark_event_id
          AND stored.capture_sequence IS NOT DISTINCT FROM verified_binding.capture_sequence
          AND stored.operational_source_packet_id IS NOT DISTINCT FROM verified_binding.operational_source_packet_id
          AND stored.operation_plan_id IS NOT DISTINCT FROM verified_binding.operation_plan_id
          AND stored.composite_group_id IS NOT DISTINCT FROM verified_binding.composite_group_id
          AND stored.canonical_brand IS NOT DISTINCT FROM verified_binding.canonical_brand
          AND stored.source_packet_fingerprint IS NOT DISTINCT FROM verified_binding.source_packet_fingerprint
          AND stored.operation_plan_fingerprint IS NOT DISTINCT FROM verified_binding.operation_plan_fingerprint
          AND stored.exact_artifact_fingerprint IS NOT DISTINCT FROM verified_binding.exact_artifact_fingerprint
          AND stored.freshness_policy_version IS NOT DISTINCT FROM verified_binding.freshness_policy_version
          AND stored.public_key_registry_fingerprint IS NOT DISTINCT FROM
                verified_binding.public_key_registry_fingerprint
          AND stored.provenance IS NOT DISTINCT FROM verified_binding.provenance
          AND stored.receipt_set_fingerprint IS NOT DISTINCT FROM verified_binding.receipt_set_fingerprint
          AND stored.member_set_fingerprint IS NOT DISTINCT FROM verified_binding.member_set_fingerprint
          AND stored.eligible_until IS NOT DISTINCT FROM verified_binding.eligible_until
          AND stored.binding_schema_version IS NOT DISTINCT FROM verified_binding.binding_schema_version
          AND stored.authority = false
          AND stored.production_runtime_effect = false
          AND stored.scanner_runtime_effect = false
    ) THEN
        RAISE EXCEPTION 'verified C7 binding replay diverges from immutable stored content';
    END IF;

    FOR member IN SELECT * FROM jsonb_populate_recordset(
        NULL::b3s_history.evidence_vault_verified_c7_lineage_members,
        payload -> 'members'
    ) LOOP
        INSERT INTO b3s_history.evidence_vault_verified_c7_lineage_members (
            id, binding_id, workspace_id, brand_id, scan_run_id, capture_id,
            operational_source_packet_id, composite_group_id,
            raw_evidence_binding_id, receipt_id, evidence_record_id, channel_role,
            relation_id, evidence_id, source_identity_id, evidence_fingerprint,
            evidence_ref, source_ref, evidence_quote, member_fingerprint
        ) VALUES (
            member.id, member.binding_id, member.workspace_id, member.brand_id,
            member.scan_run_id, member.capture_id,
            member.operational_source_packet_id, member.composite_group_id,
            member.raw_evidence_binding_id, member.receipt_id,
            member.evidence_record_id, member.channel_role, member.relation_id,
            member.evidence_id, member.source_identity_id,
            member.evidence_fingerprint, member.evidence_ref, member.source_ref,
            member.evidence_quote, member.member_fingerprint
        ) ON CONFLICT (member_fingerprint) DO NOTHING;
        IF NOT EXISTS (
            SELECT 1
            FROM b3s_history.evidence_vault_verified_c7_lineage_members AS stored
            WHERE stored.member_fingerprint = member.member_fingerprint
              AND stored.id IS NOT DISTINCT FROM member.id
              AND stored.binding_id IS NOT DISTINCT FROM member.binding_id
              AND stored.workspace_id IS NOT DISTINCT FROM member.workspace_id
              AND stored.brand_id IS NOT DISTINCT FROM member.brand_id
              AND stored.scan_run_id IS NOT DISTINCT FROM member.scan_run_id
              AND stored.capture_id IS NOT DISTINCT FROM member.capture_id
              AND stored.operational_source_packet_id IS NOT DISTINCT FROM member.operational_source_packet_id
              AND stored.composite_group_id IS NOT DISTINCT FROM member.composite_group_id
              AND stored.raw_evidence_binding_id IS NOT DISTINCT FROM member.raw_evidence_binding_id
              AND stored.receipt_id IS NOT DISTINCT FROM member.receipt_id
              AND stored.evidence_record_id IS NOT DISTINCT FROM member.evidence_record_id
              AND stored.channel_role IS NOT DISTINCT FROM member.channel_role
              AND stored.relation_id IS NOT DISTINCT FROM member.relation_id
              AND stored.evidence_id IS NOT DISTINCT FROM member.evidence_id
              AND stored.source_identity_id IS NOT DISTINCT FROM member.source_identity_id
              AND stored.evidence_fingerprint IS NOT DISTINCT FROM member.evidence_fingerprint
              AND stored.evidence_ref IS NOT DISTINCT FROM member.evidence_ref
              AND stored.source_ref IS NOT DISTINCT FROM member.source_ref
              AND stored.evidence_quote IS NOT DISTINCT FROM member.evidence_quote
        ) THEN
            RAISE EXCEPTION 'verified C7 member replay diverges from immutable stored content';
        END IF;
    END LOOP;
    initial_idempotency := b3s_history.evidence_vault_canonical_fingerprint(
        'evidence-vault-raw-provenance-initial-retain-idempotency-v1',
        jsonb_build_object('binding_fingerprint', verified_binding.binding_fingerprint)
    );
    initial_event_fingerprint := b3s_history.evidence_vault_canonical_fingerprint(
        'evidence-vault-raw-provenance-disposition-event-v1',
        jsonb_build_object(
            'schema_version', 'evidence-vault-raw-provenance-disposition-event-v1',
            'binding_fingerprint', verified_binding.binding_fingerprint,
            'receipt_set_fingerprint', verified_binding.receipt_set_fingerprint,
            'event_sequence', 1,
            'previous_event_fingerprint', NULL,
            'action', 'retain',
            'prior_state', 'none',
            'resulting_state', 'retained',
            'reason', 'initial verified raw provenance retention',
            'actor_id', 'trusted-acquisition-ingest',
            'idempotency_key_hash', initial_idempotency
        )
    );
    IF NOT EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_raw_provenance_disposition_events
        WHERE binding_id = verified_binding.id
          AND idempotency_key_hash = initial_idempotency
    ) THEN
        INSERT INTO b3s_history.evidence_vault_raw_provenance_disposition_events (
            id, binding_id, workspace_id, brand_id, scan_run_id, capture_id,
        operational_source_packet_id, composite_group_id,
        receipt_set_fingerprint, event_sequence, previous_event_id,
        previous_event_fingerprint, action, prior_state, resulting_state,
        reason, actor_id, idempotency_key_hash, event_schema_version,
        event_fingerprint
    ) VALUES (
        substr(verified_binding.binding_fingerprint, 1, 32)::uuid,
        verified_binding.id, verified_binding.workspace_id,
        verified_binding.brand_id, verified_binding.scan_run_id,
        verified_binding.capture_id,
        verified_binding.operational_source_packet_id,
        verified_binding.composite_group_id,
        verified_binding.receipt_set_fingerprint, 1, NULL, NULL,
        'retain', 'none', 'retained',
        'initial verified raw provenance retention',
        'trusted-acquisition-ingest', initial_idempotency,
        'evidence-vault-raw-provenance-disposition-event-v1',
        initial_event_fingerprint
    );
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM b3s_history.evidence_vault_raw_provenance_disposition_events AS stored
        WHERE stored.binding_id = verified_binding.id
          AND stored.idempotency_key_hash = initial_idempotency
          AND stored.id = substr(verified_binding.binding_fingerprint, 1, 32)::uuid
          AND stored.workspace_id = verified_binding.workspace_id
          AND stored.brand_id = verified_binding.brand_id
          AND stored.scan_run_id = verified_binding.scan_run_id
          AND stored.capture_id = verified_binding.capture_id
          AND stored.operational_source_packet_id =
                verified_binding.operational_source_packet_id
          AND stored.composite_group_id = verified_binding.composite_group_id
          AND stored.receipt_set_fingerprint = verified_binding.receipt_set_fingerprint
          AND stored.event_sequence = 1
          AND stored.previous_event_id IS NULL
          AND stored.previous_event_fingerprint IS NULL
          AND stored.action = 'retain'
          AND stored.prior_state = 'none'
          AND stored.resulting_state = 'retained'
          AND stored.reason = 'initial verified raw provenance retention'
          AND stored.actor_id = 'trusted-acquisition-ingest'
          AND stored.event_schema_version =
                'evidence-vault-raw-provenance-disposition-event-v1'
          AND stored.event_fingerprint = initial_event_fingerprint
    ) THEN
        RAISE EXCEPTION 'initial disposition replay diverges from immutable stored content';
    END IF;

    RETURN jsonb_build_object(
        'receipt_ids', receipt_ids,
        'evidence_binding_ids', evidence_binding_ids,
        'verified_binding_id', verified_binding.id
    );
END;
$$;

CREATE FUNCTION b3s_history.append_evidence_vault_raw_provenance_disposition(
    payload jsonb
)
RETURNS b3s_history.evidence_vault_raw_provenance_disposition_events
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    event b3s_history.evidence_vault_raw_provenance_disposition_events%ROWTYPE;
    stored b3s_history.evidence_vault_raw_provenance_disposition_events%ROWTYPE;
BEGIN
    event := jsonb_populate_record(
        NULL::b3s_history.evidence_vault_raw_provenance_disposition_events,
        payload
    );
    PERFORM pg_advisory_xact_lock(
        b3s_history.evidence_vault_ingest_lock_key(
            event.workspace_id, event.idempotency_key_hash
        )
    );
    PERFORM pg_advisory_xact_lock(
        b3s_history.evidence_vault_brand_lock_key(event.workspace_id, event.brand_id)
    );
    SELECT * INTO stored
    FROM b3s_history.evidence_vault_raw_provenance_disposition_events
    WHERE binding_id = event.binding_id
      AND idempotency_key_hash = event.idempotency_key_hash;
    IF stored.id IS NOT NULL THEN
        IF stored.id IS DISTINCT FROM event.id
           OR stored.binding_id IS DISTINCT FROM event.binding_id
           OR stored.workspace_id IS DISTINCT FROM event.workspace_id
           OR stored.brand_id IS DISTINCT FROM event.brand_id
           OR stored.scan_run_id IS DISTINCT FROM event.scan_run_id
           OR stored.capture_id IS DISTINCT FROM event.capture_id
           OR stored.operational_source_packet_id IS DISTINCT FROM
                event.operational_source_packet_id
           OR stored.composite_group_id IS DISTINCT FROM event.composite_group_id
           OR stored.receipt_set_fingerprint IS DISTINCT FROM
                event.receipt_set_fingerprint
           OR stored.event_sequence IS DISTINCT FROM event.event_sequence
           OR stored.previous_event_id IS DISTINCT FROM event.previous_event_id
           OR stored.previous_event_fingerprint IS DISTINCT FROM
                event.previous_event_fingerprint
           OR stored.action IS DISTINCT FROM event.action
           OR stored.prior_state IS DISTINCT FROM event.prior_state
           OR stored.resulting_state IS DISTINCT FROM event.resulting_state
           OR stored.reason IS DISTINCT FROM event.reason
           OR stored.actor_id IS DISTINCT FROM event.actor_id
           OR stored.idempotency_key_hash IS DISTINCT FROM event.idempotency_key_hash
           OR stored.event_schema_version IS DISTINCT FROM event.event_schema_version
           OR stored.event_fingerprint IS DISTINCT FROM event.event_fingerprint THEN
            RAISE EXCEPTION 'disposition idempotency key was reused with divergent content';
        END IF;
        RETURN stored;
    END IF;
    INSERT INTO b3s_history.evidence_vault_raw_provenance_disposition_events (
        id, binding_id, workspace_id, brand_id, scan_run_id, capture_id,
        operational_source_packet_id, composite_group_id,
        receipt_set_fingerprint, event_sequence, previous_event_id,
        previous_event_fingerprint, action, prior_state, resulting_state,
        reason, actor_id, idempotency_key_hash, event_schema_version,
        event_fingerprint
    ) VALUES (
        event.id, event.binding_id, event.workspace_id, event.brand_id,
        event.scan_run_id, event.capture_id, event.operational_source_packet_id,
        event.composite_group_id, event.receipt_set_fingerprint,
        event.event_sequence, event.previous_event_id,
        event.previous_event_fingerprint, event.action, event.prior_state,
        event.resulting_state, event.reason, event.actor_id,
        event.idempotency_key_hash, event.event_schema_version,
        event.event_fingerprint
    ) RETURNING * INTO stored;
    RETURN stored;
END;
$$;

CREATE FUNCTION b3s_history.read_evidence_vault_raw_acquisition(
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
        'scan_run', to_jsonb(scans),
        'operation_plan', (
            SELECT to_jsonb(plans)
            FROM b3s_history.evidence_vault_operation_plans AS plans
            WHERE plans.workspace_id = workspaces.id
              AND plans.brand_id = brands.id
              AND plans.scan_run_id = scans.id
        ),
        'capture', to_jsonb(captures),
        'evidence_records', COALESCE((
            SELECT jsonb_agg(to_jsonb(evidence) ORDER BY evidence.evidence_ref)
            FROM b3s_history.evidence_records AS evidence
            WHERE evidence.capture_id = captures.id
        ), '[]'::jsonb),
        'watermark_event', (
            SELECT to_jsonb(watermarks)
            FROM b3s_history.evidence_vault_capture_watermark_events AS watermarks
            WHERE watermarks.brand_id = captures.brand_id
              AND watermarks.capture_id = captures.id
        ),
        'receipts', COALESCE((
            SELECT jsonb_agg(to_jsonb(receipts) ORDER BY receipts.channel_role)
            FROM b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
            WHERE receipts.workspace_id = workspaces.id
              AND receipts.scan_run_id = scans.id
              AND receipts.capture_id = captures.id
        ), '[]'::jsonb),
        'evidence_bindings', COALESCE((
            SELECT jsonb_agg(to_jsonb(raw_bindings) ORDER BY raw_bindings.channel_role)
            FROM b3s_history.evidence_vault_raw_evidence_bindings AS raw_bindings
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

CREATE FUNCTION b3s_history.read_evidence_vault_raw_provenance(p_binding_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT jsonb_build_object(
        'binding', to_jsonb(bindings),
        'members', COALESCE((
            SELECT jsonb_agg(to_jsonb(members) ORDER BY members.channel_role)
            FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
            WHERE members.binding_id = bindings.id
        ), '[]'::jsonb),
        'receipts', COALESCE((
            SELECT jsonb_agg(to_jsonb(receipts) ORDER BY members.channel_role)
            FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
            JOIN b3s_history.evidence_vault_raw_acquisition_receipts AS receipts
              ON receipts.id = members.receipt_id
            WHERE members.binding_id = bindings.id
        ), '[]'::jsonb),
        'evidence_bindings', COALESCE((
            SELECT jsonb_agg(to_jsonb(raw_bindings) ORDER BY members.channel_role)
            FROM b3s_history.evidence_vault_verified_c7_lineage_members AS members
            JOIN b3s_history.evidence_vault_raw_evidence_bindings AS raw_bindings
              ON raw_bindings.id = members.raw_evidence_binding_id
            WHERE members.binding_id = bindings.id
        ), '[]'::jsonb),
        'dispositions', COALESCE((
            SELECT jsonb_agg(to_jsonb(events) ORDER BY events.event_sequence)
            FROM b3s_history.evidence_vault_raw_provenance_disposition_events AS events
            WHERE events.binding_id = bindings.id
        ), '[]'::jsonb)
    )
    FROM b3s_history.evidence_vault_verified_c7_lineage_bindings AS bindings
    WHERE bindings.id = p_binding_id;
$$;

REVOKE ALL ON b3s_history.evidence_vault_raw_acquisition_receipts FROM PUBLIC;
REVOKE ALL ON b3s_history.evidence_vault_raw_evidence_bindings FROM PUBLIC;
REVOKE ALL ON b3s_history.evidence_vault_verified_c7_lineage_bindings FROM PUBLIC;
REVOKE ALL ON b3s_history.evidence_vault_verified_c7_lineage_members FROM PUBLIC;
REVOKE ALL ON b3s_history.evidence_vault_raw_provenance_disposition_events FROM PUBLIC;
REVOKE ALL ON FUNCTION b3s_history.validate_evidence_vault_raw_receipt_durable() FROM PUBLIC;
REVOKE ALL ON FUNCTION b3s_history.validate_evidence_vault_verified_c7_binding_trigger() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION b3s_history.append_evidence_vault_raw_acquisition(jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION b3s_history.bind_evidence_vault_verified_c7_lineage(jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION b3s_history.append_evidence_vault_raw_provenance_disposition(jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION b3s_history.read_evidence_vault_raw_provenance(uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION b3s_history.read_evidence_vault_raw_acquisition(text, text) FROM PUBLIC;

-- Transfer only when the fixed role exists and the release actor may SET ROLE.
DO $$
DECLARE
    table_name text;
    function_signature text;
    actual_owner text;
BEGIN
    GRANT USAGE, CREATE ON SCHEMA b3s_history
        TO b3s_history_vault_provenance_owner;
    GRANT SELECT, INSERT ON b3s_history.workspaces,
        b3s_history.scan_runs, b3s_history.captures,
        b3s_history.evidence_records,
        b3s_history.evidence_vault_capture_watermark_events,
        b3s_history.evidence_vault_canonical_memory_packets,
        b3s_history.evidence_vault_operation_plans
        TO b3s_history_vault_provenance_owner;
    GRANT SELECT, INSERT, UPDATE ON b3s_history.brands
        TO b3s_history_vault_provenance_owner;
    FOREACH table_name IN ARRAY ARRAY[
        'evidence_vault_raw_acquisition_receipts',
        'evidence_vault_raw_evidence_bindings',
        'evidence_vault_verified_c7_lineage_bindings',
        'evidence_vault_verified_c7_lineage_members',
        'evidence_vault_raw_provenance_disposition_events'
    ] LOOP
        EXECUTE format(
            'ALTER TABLE b3s_history.%I OWNER TO b3s_history_vault_provenance_owner',
            table_name
        );
    END LOOP;
    FOREACH function_signature IN ARRAY ARRAY[
        'b3s_history.append_evidence_vault_raw_acquisition(jsonb)',
        'b3s_history.bind_evidence_vault_verified_c7_lineage(jsonb)',
        'b3s_history.append_evidence_vault_raw_provenance_disposition(jsonb)',
        'b3s_history.read_evidence_vault_raw_provenance(uuid)',
        'b3s_history.read_evidence_vault_raw_acquisition(text, text)',
        'b3s_history.validate_evidence_vault_capture_watermark_row()',
        'b3s_history.validate_evidence_vault_raw_receipt_durable()',
        'b3s_history.validate_evidence_vault_verified_c7_binding_trigger()'
    ] LOOP
        EXECUTE format(
            'ALTER FUNCTION %s OWNER TO b3s_history_vault_provenance_owner',
            function_signature
        );
    END LOOP;
    REVOKE CREATE ON SCHEMA b3s_history
        FROM b3s_history_vault_provenance_owner;

    IF pg_catalog.has_schema_privilege(
        'b3s_history_vault_provenance_owner', 'b3s_history', 'CREATE'
    ) THEN
        RAISE EXCEPTION 'provenance owner retains forbidden schema CREATE';
    END IF;
    FOREACH table_name IN ARRAY ARRAY[
        'evidence_vault_raw_acquisition_receipts',
        'evidence_vault_raw_evidence_bindings',
        'evidence_vault_verified_c7_lineage_bindings',
        'evidence_vault_verified_c7_lineage_members',
        'evidence_vault_raw_provenance_disposition_events'
    ] LOOP
        SELECT roles.rolname INTO actual_owner
        FROM pg_catalog.pg_class AS relations
        JOIN pg_catalog.pg_namespace AS schemas
          ON schemas.oid = relations.relnamespace
        JOIN pg_catalog.pg_roles AS roles ON roles.oid = relations.relowner
        WHERE schemas.nspname = 'b3s_history'
          AND relations.relname = table_name;
        IF actual_owner IS DISTINCT FROM 'b3s_history_vault_provenance_owner' THEN
            RAISE EXCEPTION '019 journal % has incorrect owner %',
                table_name, actual_owner;
        END IF;
    END LOOP;
    FOREACH function_signature IN ARRAY ARRAY[
        'b3s_history.append_evidence_vault_raw_acquisition(jsonb)',
        'b3s_history.bind_evidence_vault_verified_c7_lineage(jsonb)',
        'b3s_history.append_evidence_vault_raw_provenance_disposition(jsonb)',
        'b3s_history.read_evidence_vault_raw_provenance(uuid)',
        'b3s_history.read_evidence_vault_raw_acquisition(text, text)',
        'b3s_history.validate_evidence_vault_capture_watermark_row()',
        'b3s_history.validate_evidence_vault_raw_receipt_durable()',
        'b3s_history.validate_evidence_vault_verified_c7_binding_trigger()'
    ] LOOP
        SELECT roles.rolname INTO actual_owner
        FROM pg_catalog.pg_proc AS functions
        JOIN pg_catalog.pg_roles AS roles ON roles.oid = functions.proowner
        WHERE functions.oid = function_signature::regprocedure
          AND functions.prosecdef;
        IF actual_owner IS DISTINCT FROM 'b3s_history_vault_provenance_owner' THEN
            RAISE EXCEPTION 'SECURITY DEFINER % has incorrect owner %',
                function_signature, actual_owner;
        END IF;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION b3s_history.append_evidence_vault_raw_acquisition(jsonb) IS
    'Scanner-worker-only atomic base-capture phase: prepared scan/capture/evidence/watermark plus one or two signed receipts/bindings; it cannot create a verified group.';
COMMENT ON FUNCTION b3s_history.bind_evidence_vault_verified_c7_lineage(jsonb) IS
    'Later accepted-group phase: exact-replays the two immutable raw rows, then atomically appends one verified binding and two members.';
COMMENT ON FUNCTION b3s_history.append_evidence_vault_raw_provenance_disposition(jsonb) IS
    'Governance-only append surface. Deployments grant EXECUTE to a distinct provenance-governance role.';
COMMENT ON FUNCTION b3s_history.read_evidence_vault_raw_acquisition(text, text) IS
    'EXECUTE-only lookup of one complete raw acquisition by workspace and source scan';
COMMENT ON FUNCTION b3s_history.read_evidence_vault_raw_provenance(uuid) IS
    'Read-only raw provenance projection; Ed25519 must still be reverified by Python before readiness.';
