from __future__ import annotations

import os
from importlib import resources

import pytest


def _sql() -> str:
    return (
        resources.files("src.history")
        .joinpath("migrations/019_evidence_vault_verified_raw_provenance.sql")
        .read_text(encoding="utf-8")
    )


def _signed_owned_receipt(
    *,
    fetched_at: str = "2026-08-08T20:00:00Z",
    source_scan_id: str = "scan-pure-shape",
    acquisition_session_id: str | None = None,
    raw_fragment_sha256: str = "2" * 64,
    extracted_document_sha256: str = "3" * 64,
    pre_receipt_snapshot_sha256: str = "1" * 64,
) -> object:
    import base64
    from uuid import uuid4

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from src.services.evidence_vault_raw_provenance import (
        C7_LIVE_FRESHNESS_POLICY_VERSION,
        ED25519_SIGNATURE_VERSION,
        PUBLIC_KEY_REGISTRY_VERSION,
        RAW_ACQUISITION_RECEIPT_VERSION,
        DirectAcquisition,
        RawAcquisitionReceiptClaims,
        sign_raw_acquisition_receipt,
    )

    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    claims = RawAcquisitionReceiptClaims(
        schema_version=RAW_ACQUISITION_RECEIPT_VERSION,
        freshness_policy_version=C7_LIVE_FRESHNESS_POLICY_VERSION,
        key_id="test-key-v1",
        receipt_nonce=str(uuid4()),
        acquisition_session_id=acquisition_session_id or str(uuid4()),
        workspace_slug="b3s",
        source_scan_id=source_scan_id,
        canonical_brand_domain="example.com",
        channel_role="owned_web",
        pre_receipt_snapshot_sha256=pre_receipt_snapshot_sha256,
        provider="direct_http",
        acquisition=DirectAcquisition(
            acquisition_mode="direct_http",
            requested_url="https://example.com",
            redirect_chain=[],
            final_url="https://example.com",
        ),
        fetched_at=fetched_at,
        status_code=200,
        selected_headers={"content-type": "text/html"},
        media_type="text/html",
        byte_count=128,
        raw_fragment_json_pointer="/sources/owned",
        raw_fragment_sha256=raw_fragment_sha256,
        extracted_document_sha256=extracted_document_sha256,
        extractor_version="evidence-vault-deterministic-extractor-v1",
        external_identity_provenance_fingerprint=None,
    )
    return sign_raw_acquisition_receipt(
        claims,
        private_key=private_key,
        public_key_registry={
            "schema_version": PUBLIC_KEY_REGISTRY_VERSION,
            "current_key_id": "test-key-v1",
            "keys": {
                "test-key-v1": {
                    "version": 1,
                    "status": "current",
                    "signing_not_before": "2026-01-01T00:00:00Z",
                    "signing_ended_at": None,
                    "public_key_base64": base64.b64encode(public_bytes).decode("ascii"),
                }
            },
        },
    )


def test_sql_receipt_shape_matches_signed_pure_v1_contract() -> None:
    from src.services.evidence_vault_raw_provenance import receipt_set_fingerprint

    receipt = _signed_owned_receipt()
    dumped = receipt.model_dump(mode="json")
    claims = dumped["claims"]
    assert set(claims) == {
        "schema_version", "freshness_policy_version", "key_id", "receipt_nonce",
        "acquisition_session_id", "workspace_slug", "source_scan_id",
        "canonical_brand_domain", "channel_role", "pre_receipt_snapshot_sha256",
        "provider", "acquisition", "fetched_at", "status_code",
        "selected_headers", "media_type", "byte_count",
        "raw_fragment_json_pointer", "raw_fragment_sha256",
        "extracted_document_sha256", "extractor_version",
        "external_identity_provenance_fingerprint",
    }
    assert set(claims["acquisition"]) == {
        "acquisition_mode", "requested_url", "redirect_chain", "final_url"
    }
    sql = _sql()
    assert "receipt_nonce uuid NOT NULL" in sql
    assert "canonical_brand_domain' IS DISTINCT FROM NEW.canonical_brand" in sql
    assert "raw_fragment_json_pointer' IS DISTINCT FROM NEW.raw_fragment_pointer" in sql
    assert "NEW.claims ->> 'status_code'" in sql
    assert "NEW.claims -> 'acquisition'" in sql
    assert "'receipt_fingerprints', jsonb_agg(" in sql
    assert receipt_set_fingerprint([dumped])


def test_external_association_objects_are_non_circular_and_signable() -> None:
    import base64
    from uuid import uuid4

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from src.services.evidence_vault_canonical_core import canonical_fingerprint
    from src.services.evidence_vault_raw_provenance import (
        C7_LIVE_FRESHNESS_POLICY_VERSION,
        PUBLIC_KEY_REGISTRY_VERSION,
        RAW_ACQUISITION_RECEIPT_VERSION,
        ExternalIdentityProvenance,
        ProviderApiAcquisition,
        RawAcquisitionReceiptClaims,
        evidence_memory_source_identity_id,
        sign_raw_acquisition_receipt,
    )

    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    registry = {
        "schema_version": PUBLIC_KEY_REGISTRY_VERSION,
        "current_key_id": "test-key-v1",
        "keys": {"test-key-v1": {
            "version": 1,
            "status": "current",
            "signing_not_before": "2026-01-01T00:00:00Z",
            "signing_ended_at": None,
            "public_key_base64": base64.b64encode(public_bytes).decode("ascii"),
        }},
    }
    owned_receipt = _signed_owned_receipt()
    owned_fingerprint = owned_receipt.receipt_fingerprint
    for method, raw_fact_role, fact_pointer, fact_value in (
        (
            "owned_raw_links_external_profile", "owned_web",
            "/sources/owned/linkedin", "https://www.linkedin.com/company/example",
        ),
        (
            "external_raw_declares_owned_domain", "external_social_profile",
            "/sources/external/website", "https://example.com",
        ),
    ):
        import hashlib
        from src.services.evidence_vault_canonical_core import canonical_json

        provenance = {
            "schema_version": "external-identity-provenance-v1",
            "policy_version": "evidence-vault-external-identity-association-policy-v1",
            "association_method": method,
            "canonical_brand_domain": "example.com",
            "owned_source_url": "https://example.com",
            "external_source_url": "https://www.linkedin.com/company/example",
            "proof_receipt_fingerprint": owned_fingerprint,
            "raw_fact_role": raw_fact_role,
            "raw_fact_json_pointer": fact_pointer,
            "raw_fact_sha256": hashlib.sha256(
                canonical_json(fact_value).encode("utf-8")
            ).hexdigest(),
            "source_identity_schema_version": "evidence-memory-document-v2",
            "owned_source_identity_id": evidence_memory_source_identity_id(
                source_url="https://example.com", raw_fact_role="owned_web"
            ),
            "external_source_identity_id": evidence_memory_source_identity_id(
                source_url="https://www.linkedin.com/company/example",
                raw_fact_role="external_social_profile",
            ),
        }
        assert provenance["proof_receipt_fingerprint"] == owned_fingerprint
        ExternalIdentityProvenance.model_validate(provenance)
        provenance_fingerprint = canonical_fingerprint(
            "external-identity-provenance-v1", provenance
        )
        claims = RawAcquisitionReceiptClaims(
            schema_version=RAW_ACQUISITION_RECEIPT_VERSION,
            freshness_policy_version=C7_LIVE_FRESHNESS_POLICY_VERSION,
            key_id="test-key-v1",
            receipt_nonce=str(uuid4()),
            acquisition_session_id=str(uuid4()),
            workspace_slug="b3s",
            source_scan_id="scan-external-shape",
            canonical_brand_domain="example.com",
            channel_role="external_social_profile",
            pre_receipt_snapshot_sha256="1" * 64,
            provider="exa",
            acquisition=ProviderApiAcquisition(
                acquisition_mode="provider_api",
                provider_request_fingerprint="5" * 64,
                result_ordinal=0,
                reported_source_url="https://www.linkedin.com/company/example",
                redirect_chain=[],
            ),
            fetched_at="2026-08-08T20:00:00Z",
            status_code=200,
            selected_headers={"content-type": "text/html"},
            media_type="text/html",
            byte_count=128,
            raw_fragment_json_pointer="/sources/external",
            raw_fragment_sha256="4" * 64,
            extracted_document_sha256="3" * 64,
            extractor_version="evidence-vault-deterministic-extractor-v1",
            external_identity_provenance_fingerprint=provenance_fingerprint,
        )
        signed = sign_raw_acquisition_receipt(
            claims, private_key=private_key, public_key_registry=registry
        )
        assert signed.claims.external_identity_provenance_fingerprint == provenance_fingerprint


def test_verified_raw_provenance_migration_contract_is_complete() -> None:
    sql = _sql()
    names = (
        "evidence_vault_raw_acquisition_receipts",
        "evidence_vault_raw_evidence_bindings",
        "evidence_vault_verified_c7_lineage_bindings",
        "evidence_vault_verified_c7_lineage_members",
        "evidence_vault_raw_provenance_disposition_events",
    )
    for name in names:
        assert f"CREATE TABLE b3s_history.{name}" in sql
    assert "UNIQUE (key_id, receipt_nonce)" in sql
    assert "UNIQUE (receipt_fingerprint)" in sql
    assert "IS DISTINCT FROM" in sql
    assert 'ORDER BY key COLLATE "C"' in sql
    assert sql.count("DEFERRABLE INITIALLY DEFERRED") >= 3
    assert "member_count IS DISTINCT FROM 2" in sql
    assert "ARRAY['external_social_profile', 'owned_web']::text[]" in sql
    assert "report_derived_candidate_capture" not in sql


def test_verified_raw_sql_rejects_forgery_cross_parent_replay_and_partial_groups() -> None:
    sql = _sql()
    assert "raw receipt fingerprint is invalid" in sql
    assert "raw receipt signed payload is invalid" in sql
    assert "raw receipt fragment hash differs from durable capture" in sql
    assert "raw evidence locator does not reproduce a meaningful passage" in sql
    assert "'utf8_byte_range'" in sql
    assert "'extracted_start', 'extracted_end'" in sql
    assert "'evidence_start', 'evidence_end'" in sql
    assert "octet_length(convert_to(NEW.passage_text, 'UTF8')) < 8" in sql
    assert "char_length(NEW.passage_text) < 4" in sql
    assert "extracted_end > octet_length(convert_to(NEW.extracted_document, 'UTF8'))" in sql
    assert "evidence_end > octet_length(convert_to(evidence.content, 'UTF8'))" in sql
    assert "regexp_matches(NEW.passage_text" not in sql
    assert "'byte_range'" not in sql
    assert "'json_pointer'" not in sql
    assert "external identity signed raw fact is not reproducible" in sql
    assert "external-identity-provenance-v1" in sql
    assert "proof_receipt_fingerprint" in sql
    assert "raw_fact_role" in sql
    assert "evidence-vault-external-identity-association-policy-v1" in sql
    assert sql.count("extractor_version = 'evidence-vault-deterministic-extractor-v1'") == 2
    assert "source_class := 'owned_copy'" in sql
    assert "source_class := 'external_proof'" in sql
    assert "evidence-memory-document-v2" in sql
    assert "proof_parent_raw_fragment_sha256" not in sql
    assert "raw_fact_json_pointer" in sql
    assert "[a-z0-9]([a-z0-9-]{0,99}[a-z0-9])?$" in sql
    assert "company/[^/?#]+" not in sql
    assert "scan workspace/brand composite-parent preflight failed" in sql
    assert "capture brand/scan composite-parent preflight failed" in sql
    assert "operation-plan workspace/brand/scan composite-parent preflight failed" in sql
    assert "request_payload -> 'capture_payload' IS DISTINCT FROM" in sql
    assert "ON CONFLICT (workspace_id, canonical_domain) DO UPDATE SET" in sql
    assert "latest_observed_at = GREATEST(" in sql
    assert "GRANT SELECT, INSERT, UPDATE ON b3s_history.brands" in sql
    assert "durable capture evidence set differs from raw observation" in sql
    assert "pre-existing scan/capture cannot be upgraded with raw provenance" in sql
    assert "COALESCE(watermark_head.capture_sequence, 0) + 1" in sql
    assert "receipt.watermark_event_id := watermark_row.id" in sql
    assert "read_evidence_vault_raw_acquisition" in sql
    assert "'operation_plan', (" in sql
    assert "signed_receipts" in sql
    assert "ON CONFLICT (receipt_fingerprint) DO NOTHING" in sql
    assert "idempotency key was reused with divergent content" in sql
    assert sql.count("replay diverges from immutable stored content") >= 6
    bind_phase = sql[
        sql.index("CREATE FUNCTION b3s_history.bind_evidence_vault_verified_c7_lineage"):
        sql.index("CREATE FUNCTION b3s_history.append_evidence_vault_raw_provenance_disposition")
    ]
    for complete_replay_field in (
        "stored.provider IS NOT DISTINCT FROM receipt.provider",
        "stored.raw_fragment_sha256 IS NOT DISTINCT FROM receipt.raw_fragment_sha256",
        "stored.extractor_version IS NOT DISTINCT FROM receipt.extractor_version",
        "stored.evidence_ref IS NOT DISTINCT FROM raw_binding.evidence_ref",
        "stored.passage_sha256 IS NOT DISTINCT FROM raw_binding.passage_sha256",
        "stored.source_packet_fingerprint IS NOT DISTINCT FROM verified_binding.source_packet_fingerprint",
        "stored.binding_schema_version IS NOT DISTINCT FROM verified_binding.binding_schema_version",
        "stored.public_key_registry_fingerprint IS NOT DISTINCT FROM",
        "stored.source_identity_id IS NOT DISTINCT FROM member.source_identity_id",
        "stored.source_ref IS NOT DISTINCT FROM member.source_ref",
    ):
        assert complete_replay_field in bind_phase
    assert "stored.reason IS DISTINCT FROM event.reason" in sql
    assert "requires exactly two valid independent members" in sql
    assert "c7_group_count IS DISTINCT FROM 1" in sql
    assert "evidence_vault_url_host(proof_receipt.source_url)" in sql
    assert "proof_receipt.source_url IS DISTINCT FROM" not in sql
    assert "'receipts', COALESCE" in sql
    assert "'evidence_bindings', COALESCE" in sql
    assert "jsonb_agg(to_jsonb(receipts) ORDER BY members.channel_role)" in sql
    assert "jsonb_agg(to_jsonb(raw_bindings) ORDER BY members.channel_role)" in sql
    assert "'watermark_event', 'receipts', 'evidence_bindings'" in sql
    assert "jsonb_array_length(payload -> 'receipts') NOT BETWEEN 1 AND 2" in sql
    assert "IS DISTINCT FROM jsonb_array_length(payload -> 'receipts')" in sql
    assert "ARRAY['receipts', 'evidence_bindings', 'verified_binding', 'members']" in sql
    assert "jsonb_array_length(payload -> 'receipts') IS DISTINCT FROM 2" in sql
    assert "jsonb_array_length(payload -> 'evidence_bindings') IS DISTINCT FROM 2" in sql
    assert "FOR receipt IN SELECT * FROM jsonb_populate_recordset" in sql
    assert "FOR raw_binding IN SELECT * FROM jsonb_populate_recordset" in sql
    raw_phase = sql[
        sql.index("CREATE FUNCTION b3s_history.append_evidence_vault_raw_acquisition"):
        sql.index("CREATE FUNCTION b3s_history.bind_evidence_vault_verified_c7_lineage")
    ]
    assert "evidence_vault_verified_c7_lineage_bindings (" not in raw_phase
    assert "operation plan replay diverges from immutable stored content" in raw_phase
    for receipt_replay_field in (
        "stored.receipt_schema_version IS NOT DISTINCT FROM receipt.receipt_schema_version",
        "stored.freshness_policy_version IS NOT DISTINCT FROM receipt.freshness_policy_version",
        "stored.signature_schema_version IS NOT DISTINCT FROM receipt.signature_schema_version",
        "stored.workspace_slug IS NOT DISTINCT FROM receipt.workspace_slug",
        "stored.canonical_brand IS NOT DISTINCT FROM receipt.canonical_brand",
        "stored.external_identity_provenance_fingerprint IS NOT DISTINCT FROM",
        "stored.external_identity_provenance IS NOT DISTINCT FROM",
    ):
        assert receipt_replay_field in raw_phase
    assert "plan_row.plan_payload - 'operation_plan_fingerprint'" in raw_phase
    assert "authority := true" not in raw_phase


def test_verified_raw_sql_protects_mutation_truncate_and_disposition_chain() -> None:
    sql = _sql()
    assert sql.count("BEFORE UPDATE OR DELETE ON b3s_history.evidence_vault_") == 5
    assert sql.count("BEFORE TRUNCATE ON b3s_history.evidence_vault_") == 5
    for action in ("retain", "place_legal_hold", "release_legal_hold", "revoke_runtime"):
        assert action in sql
    assert "initial raw provenance disposition must retain" in sql
    assert "initial verified raw provenance retention" in sql
    assert "trusted-acquisition-ingest" in sql
    assert "transition is invalid or terminal" in sql
    assert "predecessor must be the target head N-1" in sql


def test_verified_raw_sql_lock_order_and_privilege_surface_are_safe() -> None:
    sql = _sql()
    ingest = sql[sql.index("CREATE FUNCTION b3s_history.append_evidence_vault_raw_acquisition"):]
    source_lock = ingest.index("evidence_vault_ingest_lock_key")
    brand_lock = ingest.index("evidence_vault_brand_lock_key")
    assert source_lock < brand_lock
    mutation = sql[sql.index("CREATE FUNCTION b3s_history.reject_evidence_vault_raw_provenance_mutation"):]
    mutation = mutation[: mutation.index("CREATE TRIGGER evidence_vault_raw_receipts_append_only")]
    assert "pg_advisory" not in mutation
    assert sql.count("SECURITY DEFINER") >= 7
    assert sql.count("SET search_path = pg_catalog") >= 3
    assert "REVOKE EXECUTE ON FUNCTION" in sql
    assert "NOLOGIN" in sql
    assert "rolcreaterole" in sql
    assert "WHEN duplicate_object THEN" in sql
    assert "WHEN insufficient_privilege THEN" in sql
    assert "required NOLOGIN provenance owner is not provisioned" in sql
    assert "migrator must be able to SET required provenance owner" in sql
    assert "ownership transfer skipped" not in sql
    assert "provenance owner retains forbidden schema CREATE" in sql
    assert "SECURITY DEFINER % has incorrect owner %" in sql
    assert "pg_has_role" in sql
    assert "server_version >= 160000" in sql
    assert sql.count("''SET''") >= 2
    assert "IF NOT can_set_owner THEN" in sql
    assert "IF can_set_owner THEN" not in sql
    assert "GRANT USAGE, CREATE ON SCHEMA" in sql
    assert "REVOKE CREATE ON SCHEMA" in sql


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL")
    or os.environ.get("B3S_ALLOW_SCHEMA_DROP") != "1",
    reason="destructive PostgreSQL integration requires B3S_TEST_DATABASE_URL and B3S_ALLOW_SCHEMA_DROP=1",
)
def test_postgres_upgrade_from_committed_019_applies_later_migrations() -> None:
    import hashlib

    import psycopg
    from psycopg import sql as psycopg_sql
    from src.history.repository import PostgresHistoryRepository, _migration_files

    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    owner = "b3s_history_vault_provenance_owner"
    runtime_read = "b3s_history_vault_runtime_read"
    writer = "b3s_history_vault_sv9_shadow_writer"

    def reset(admin) -> None:
        admin.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
        for role in (runtime_read, owner, writer):
            if admin.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                (role,),
            ).fetchone()[0]:
                admin.execute(
                    psycopg_sql.SQL("REASSIGN OWNED BY {} TO CURRENT_USER").format(
                        psycopg_sql.Identifier(role)
                    )
                )
                admin.execute(
                    psycopg_sql.SQL("DROP OWNED BY {}").format(
                        psycopg_sql.Identifier(role)
                    )
                )
                admin.execute(
                    psycopg_sql.SQL("DROP ROLE {}").format(
                        psycopg_sql.Identifier(role)
                    )
                )

    with psycopg.connect(dsn, autocommit=True) as admin:
        reset(admin)
    try:
        with psycopg.connect(dsn) as admin:
            admin.execute("CREATE SCHEMA b3s_history")
            admin.execute(
                """
                CREATE TABLE b3s_history.schema_migrations (
                    version text PRIMARY KEY,
                    filename text NOT NULL,
                    checksum text NOT NULL CHECK (length(checksum) = 64),
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            for filename, sql_text in _migration_files():
                if filename == "020_evidence_vault_c7_shadow_readiness.sql":
                    break
                admin.execute(sql_text, prepare=False)
                admin.execute(
                    """
                    INSERT INTO b3s_history.schema_migrations
                        (version, filename, checksum)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        filename.split("_", 1)[0],
                        filename,
                        hashlib.sha256(sql_text.encode("utf-8")).hexdigest(),
                    ),
                )

        repository = PostgresHistoryRepository(dsn)
        assert repository.migrate() == [
            "020_evidence_vault_c7_shadow_readiness.sql",
            "021_evidence_vault_cumulative_landing_hardening.sql",
            "022_evidence_vault_raw_incremental_planning.sql",
            "023_evidence_vault_raw_replay_projection.sql",
            "024_evidence_vault_raw_accepted_relation_projection.sql",
            "025_evidence_vault_operational_sv9_shadow_assessments.sql",
            "026_evidence_vault_operational_sv9_shadow_hardening.sql",
            "027_evidence_vault_operational_sv9_shadow_writer.sql",
            "028_evidence_vault_operational_sv9_shadow_diagnostics.sql",
            "029_evidence_vault_semantic_analysis_claims.sql",
            "030_evidence_vault_receipt_evidence_cardinality.sql",
            "031_evidence_vault_sv9_judgment_candidates.sql",
            "032_evidence_vault_sv9_judgment_authority.sql",
        ]
        assert repository.migrate() == []
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            reset(admin)


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL")
    or os.environ.get("B3S_ALLOW_SCHEMA_DROP") != "1",
    reason="destructive PostgreSQL integration requires B3S_TEST_DATABASE_URL and B3S_ALLOW_SCHEMA_DROP=1",
)
def test_postgres16_createrole_migrator_migrates_with_preexisting_unadministrable_writer() -> None:
    import psycopg
    from psycopg import sql as psycopg_sql
    from psycopg.conninfo import make_conninfo
    from src.history.repository import PostgresHistoryRepository

    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    migrator = "b3s_provenance_pg16_migrator_test"
    owner = "b3s_history_vault_provenance_owner"
    runtime_read = "b3s_history_vault_runtime_read"
    writer = "b3s_history_vault_sv9_shadow_writer"

    def reset_roles(conn) -> None:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
        for role in (migrator, runtime_read, owner, writer):
            if conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                (role,),
            ).fetchone()[0]:
                conn.execute(
                    psycopg_sql.SQL("REASSIGN OWNED BY {} TO CURRENT_USER").format(
                        psycopg_sql.Identifier(role)
                    )
                )
                conn.execute(
                    psycopg_sql.SQL("DROP OWNED BY {}").format(
                        psycopg_sql.Identifier(role)
                    )
                )
                conn.execute(
                    psycopg_sql.SQL("DROP ROLE {}").format(
                        psycopg_sql.Identifier(role)
                    )
                )

    with psycopg.connect(dsn, autocommit=True) as admin:
        if admin.execute("SHOW server_version_num").fetchone()[0] < "160000":
            pytest.skip("SET membership capability exists on PostgreSQL 16+")
        reset_roles(admin)
        # This role belongs to another cluster administrator.  The CREATEROLE
        # migrator below has neither membership nor ADMIN on it; migration 027
        # must still grant its table capability without trying to mutate global
        # role membership.
        admin.execute(
            psycopg_sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(
                psycopg_sql.Identifier(writer)
            )
        )
        admin.execute(
            psycopg_sql.SQL("CREATE ROLE {} LOGIN CREATEROLE PASSWORD {}").format(
                psycopg_sql.Identifier(migrator),
                psycopg_sql.Literal("managed-migrator-test-password"),
            )
        )
        database = admin.execute("SELECT current_database()").fetchone()[0]
        admin.execute(
            psycopg_sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                psycopg_sql.Identifier(database),
                psycopg_sql.Identifier(migrator),
            )
        )
    migrator_dsn = make_conninfo(
        dsn, user=migrator, password="managed-migrator-test-password"
    )
    try:
        applied = PostgresHistoryRepository(migrator_dsn).migrate()
        assert applied[-1] == "032_evidence_vault_sv9_judgment_authority.sql"
        with psycopg.connect(dsn) as admin:
            assert admin.execute(
                """
                SELECT NOT EXISTS (
                    SELECT 1
                    FROM pg_auth_members AS memberships
                    WHERE memberships.roleid = %s::regrole
                      AND memberships.member = %s::regrole
                )
                """,
                (writer, migrator),
            ).fetchone()[0]
            assert admin.execute(
                """
                SELECT NOT EXISTS (
                    SELECT 1
                    FROM pg_auth_members AS memberships
                    WHERE memberships.roleid = %s::regrole
                      AND memberships.member IN (
                          SELECT oid FROM pg_roles
                          WHERE rolname IN (
                              'b3s_history_vault_runtime_read',
                              'b3s_pr71_scanner_ingest'
                          )
                      )
                )
                """,
                (writer,),
            ).fetchone()[0]
            assert admin.execute(
                "SELECT pg_has_role(%s, %s, 'SET')", (migrator, owner)
            ).fetchone()[0]
            assert admin.execute("""
                SELECT DISTINCT owners.rolname
                FROM pg_class AS relations
                JOIN pg_namespace AS schemas ON schemas.oid = relations.relnamespace
                JOIN pg_roles AS owners ON owners.oid = relations.relowner
                WHERE schemas.nspname = 'b3s_history'
                  AND relations.relname =
                    'evidence_vault_raw_acquisition_receipts'
            """).fetchall() == [(owner,)]
            assert not admin.execute(
                "SELECT has_schema_privilege(%s, 'b3s_history', 'CREATE')",
                (owner,),
            ).fetchone()[0]
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            reset_roles(admin)


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL")
    or os.environ.get("B3S_ALLOW_SCHEMA_DROP") != "1",
    reason="destructive PostgreSQL integration requires B3S_TEST_DATABASE_URL and B3S_ALLOW_SCHEMA_DROP=1",
)
def test_postgres_fixed_owner_fail_closed_and_preprovisioned_migrator() -> None:
    import psycopg
    from psycopg import sql as psycopg_sql
    from psycopg.conninfo import make_conninfo
    from src.history.repository import PostgresHistoryRepository

    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    migrator = "b3s_preprovisioned_migrator_test"
    owner = "b3s_history_vault_provenance_owner"
    runtime_read = "b3s_history_vault_runtime_read"
    writer = "b3s_history_vault_sv9_shadow_writer"
    password = "preprovisioned-migrator-password"

    def reset(admin) -> None:
        admin.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
        admin.execute(
            "DROP FUNCTION IF EXISTS public.b3s_runtime_preexisting_definer()"
        )
        for role in (migrator, runtime_read, owner, writer):
            if admin.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                (role,),
            ).fetchone()[0]:
                admin.execute(
                    psycopg_sql.SQL("REASSIGN OWNED BY {} TO CURRENT_USER").format(
                        psycopg_sql.Identifier(role)
                    )
                )
                admin.execute(
                    psycopg_sql.SQL("DROP OWNED BY {}").format(
                        psycopg_sql.Identifier(role)
                    )
                )
                admin.execute(
                    psycopg_sql.SQL("DROP ROLE {}").format(
                        psycopg_sql.Identifier(role)
                    )
                )

    def create_migrator(admin) -> str:
        admin.execute(
            psycopg_sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                psycopg_sql.Identifier(migrator), psycopg_sql.Literal(password)
            )
        )
        database = admin.execute("SELECT current_database()").fetchone()[0]
        admin.execute(
            psycopg_sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                psycopg_sql.Identifier(database), psycopg_sql.Identifier(migrator)
            )
        )
        return make_conninfo(dsn, user=migrator, password=password)

    with psycopg.connect(dsn, autocommit=True) as admin:
        version = int(admin.execute("SHOW server_version_num").fetchone()[0])
        reset(admin)
        missing_owner_dsn = create_migrator(admin)
    try:
        with pytest.raises(psycopg.Error, match="owner is not provisioned"):
            PostgresHistoryRepository(missing_owner_dsn).migrate()
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            reset(admin)

    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(
            psycopg_sql.SQL("CREATE ROLE {} NOLOGIN").format(
                psycopg_sql.Identifier(owner)
            )
        )
        admin.execute(
            psycopg_sql.SQL("CREATE ROLE {} NOLOGIN").format(
                psycopg_sql.Identifier(runtime_read)
            )
        )
        unsafe_inherit_dsn = create_migrator(admin)
        admin.execute(
            psycopg_sql.SQL("GRANT {} TO {}").format(
                psycopg_sql.Identifier(owner), psycopg_sql.Identifier(migrator)
            )
        )
    try:
        with pytest.raises(psycopg.Error, match="runtime_read has unsafe role attributes"):
            PostgresHistoryRepository(unsafe_inherit_dsn).migrate()
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            reset(admin)

    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(
            psycopg_sql.SQL("CREATE ROLE {} NOLOGIN").format(
                psycopg_sql.Identifier(owner)
            )
        )
        admin.execute(
            psycopg_sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(
                psycopg_sql.Identifier(runtime_read)
            )
        )
        admin.execute(
            psycopg_sql.SQL("GRANT {} TO {}").format(
                psycopg_sql.Identifier(owner), psycopg_sql.Identifier(runtime_read)
            )
        )
        unsafe_membership_dsn = create_migrator(admin)
        admin.execute(
            psycopg_sql.SQL("GRANT {} TO {}").format(
                psycopg_sql.Identifier(owner), psycopg_sql.Identifier(migrator)
            )
        )
    try:
        with pytest.raises(psycopg.Error, match="must not inherit another role"):
            PostgresHistoryRepository(unsafe_membership_dsn).migrate()
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            reset(admin)

    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(
            psycopg_sql.SQL("CREATE ROLE {} NOLOGIN").format(
                psycopg_sql.Identifier(owner)
            )
        )
        admin.execute(
            psycopg_sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(
                psycopg_sql.Identifier(runtime_read)
            )
        )
        admin.execute(
            """
            CREATE FUNCTION public.b3s_runtime_preexisting_definer()
            RETURNS integer
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog
            AS 'SELECT 1'
            """
        )
        admin.execute(
            "REVOKE ALL ON FUNCTION public.b3s_runtime_preexisting_definer() "
            "FROM PUBLIC"
        )
        admin.execute(
            psycopg_sql.SQL(
                "GRANT EXECUTE ON FUNCTION "
                "public.b3s_runtime_preexisting_definer() TO {}"
            ).format(psycopg_sql.Identifier(runtime_read))
        )
        unsafe_execute_dsn = create_migrator(admin)
        admin.execute(
            psycopg_sql.SQL("GRANT {} TO {}").format(
                psycopg_sql.Identifier(owner), psycopg_sql.Identifier(migrator)
            )
        )
    try:
        with pytest.raises(
            psycopg.Error,
            match="preexisting SECURITY DEFINER EXECUTE",
        ):
            PostgresHistoryRepository(unsafe_execute_dsn).migrate()
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            reset(admin)

    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(
            psycopg_sql.SQL("CREATE ROLE {} NOLOGIN").format(
                psycopg_sql.Identifier(owner)
            )
        )
        admin.execute(
            psycopg_sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(
                psycopg_sql.Identifier(runtime_read)
            )
        )
        admin.execute(
            "CREATE ROLE b3s_history_vault_sv9_shadow_writer "
            "NOLOGIN NOINHERIT"
        )
        provisioned_dsn = create_migrator(admin)
        admin.execute(
            psycopg_sql.SQL("GRANT {} TO {}").format(
                psycopg_sql.Identifier(owner), psycopg_sql.Identifier(migrator)
            )
        )
    try:
        applied = PostgresHistoryRepository(provisioned_dsn).migrate()
        assert applied[-1] == "032_evidence_vault_sv9_judgment_authority.sql"
        with psycopg.connect(dsn) as admin:
            journal_owners = admin.execute("""
                SELECT array_agg(DISTINCT owners.rolname), count(*)
                FROM pg_class AS relations
                JOIN pg_namespace AS schemas ON schemas.oid = relations.relnamespace
                JOIN pg_roles AS owners ON owners.oid = relations.relowner
                WHERE schemas.nspname = 'b3s_history'
                  AND relations.relname = ANY(%s)
            """, ([
                "evidence_vault_raw_acquisition_receipts",
                "evidence_vault_raw_evidence_bindings",
                "evidence_vault_verified_c7_lineage_bindings",
                "evidence_vault_verified_c7_lineage_members",
                "evidence_vault_raw_provenance_disposition_events",
            ],)).fetchone()
            assert journal_owners == ([owner], 5)
            definer_owners = admin.execute("""
                SELECT array_agg(DISTINCT owners.rolname), count(*)
                FROM pg_proc AS functions
                JOIN pg_namespace AS schemas ON schemas.oid = functions.pronamespace
                JOIN pg_roles AS owners ON owners.oid = functions.proowner
                WHERE schemas.nspname = 'b3s_history'
                  AND functions.prosecdef
                  AND functions.proname = ANY(%s)
            """, ([
                "append_evidence_vault_raw_acquisition",
                "bind_evidence_vault_verified_c7_lineage",
                "append_evidence_vault_raw_provenance_disposition",
                "read_evidence_vault_raw_provenance",
                "read_evidence_vault_raw_acquisition",
                "read_evidence_vault_c7_shadow_context",
                "read_evidence_vault_c7_shadow_provenance",
                "read_evidence_vault_c7_shadow_acquisition",
                "validate_evidence_vault_capture_watermark_row",
                "validate_evidence_vault_raw_receipt_durable",
                "validate_evidence_vault_verified_c7_binding_trigger",
            ],)).fetchone()
            assert definer_owners == ([owner], 11)
            assert not admin.execute(
                "SELECT has_schema_privilege(%s, 'b3s_history', 'CREATE')",
                (owner,),
            ).fetchone()[0]
            runtime_attributes = admin.execute(
                """
                SELECT rolcanlogin, rolinherit, rolsuper, rolcreaterole,
                       rolcreatedb, rolreplication, rolbypassrls
                FROM pg_roles WHERE rolname = %s
                """,
                (runtime_read,),
            ).fetchone()
            assert runtime_attributes == (False, False, False, False, False, False, False)
            executable_definers = admin.execute(
                """
                SELECT array_agg(functions.proname ORDER BY functions.proname)
                FROM pg_proc AS functions
                JOIN pg_namespace AS schemas ON schemas.oid = functions.pronamespace
                WHERE schemas.nspname = 'b3s_history'
                  AND functions.prosecdef
                  AND has_function_privilege(%s, functions.oid, 'EXECUTE')
                """,
                (runtime_read,),
            ).fetchone()[0]
            assert executable_definers == [
                "read_evidence_vault_c7_shadow_acquisition",
                "read_evidence_vault_c7_shadow_context",
                "read_evidence_vault_c7_shadow_provenance",
            ]
            assert not admin.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_auth_members AS memberships
                    JOIN pg_roles AS members ON members.oid = memberships.member
                    WHERE members.rolname = %s
                )
                """,
                (runtime_read,),
            ).fetchone()[0]
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            reset(admin)

    if version >= 160000:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                psycopg_sql.SQL("CREATE ROLE {} NOLOGIN").format(
                    psycopg_sql.Identifier(owner)
                )
            )
            set_false_dsn = create_migrator(admin)
            admin.execute(
                psycopg_sql.SQL(
                    "GRANT {} TO {} WITH INHERIT TRUE, SET FALSE"
                ).format(
                    psycopg_sql.Identifier(owner),
                    psycopg_sql.Identifier(migrator),
                )
            )
        try:
            with pytest.raises(psycopg.Error, match="must be able to SET"):
                PostgresHistoryRepository(set_false_dsn).migrate()
        finally:
            with psycopg.connect(dsn, autocommit=True) as admin:
                reset(admin)


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL")
    or os.environ.get("B3S_ALLOW_SCHEMA_DROP") != "1",
    reason="destructive PostgreSQL integration requires B3S_TEST_DATABASE_URL and B3S_ALLOW_SCHEMA_DROP=1",
)
def test_postgres_verified_raw_journals_reject_truncate_and_expose_no_public_execute() -> None:
    import psycopg
    from psycopg import sql as psycopg_sql
    from src.history.repository import PostgresHistoryRepository

    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    execute_only_role = "b3s_raw_execute_only_test"
    governance_role = "b3s_provenance_governance_test"
    runtime_read = "b3s_history_vault_runtime_read"
    writer = "b3s_history_vault_sv9_shadow_writer"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
        for role in (execute_only_role, governance_role, runtime_read, writer):
            conn.execute(
                psycopg_sql.SQL("DROP ROLE IF EXISTS {}").format(
                    psycopg_sql.Identifier(role)
                )
            )
    try:
        applied = PostgresHistoryRepository(dsn).migrate()
        assert applied[-1] == "032_evidence_vault_sv9_judgment_authority.sql"
        receipt = _signed_owned_receipt()
        dumped = receipt.model_dump(mode="json")
        with psycopg.connect(dsn) as conn:
            from psycopg.types.json import Jsonb

            sql_fingerprint = conn.execute(
                """SELECT b3s_history.evidence_vault_canonical_fingerprint(
                       'evidence-vault-raw-acquisition-receipt-v1', %s::jsonb
                   )""",
                (Jsonb(dumped["claims"]),),
            ).fetchone()[0]
            assert sql_fingerprint == dumped["receipt_fingerprint"]
            from src.services.evidence_vault_raw_provenance import (
                evidence_memory_source_identity_id,
            )
            sql_source_ids = conn.execute("""
                SELECT
                    b3s_history.evidence_vault_source_identity_id(
                        'https://example.com', 'owned_web'
                    ),
                    b3s_history.evidence_vault_source_identity_id(
                        'https://www.linkedin.com/company/example',
                        'external_social_profile'
                    )
            """).fetchone()
            assert sql_source_ids == (
                evidence_memory_source_identity_id(
                    source_url='https://example.com', raw_fact_role='owned_web'
                ),
                evidence_memory_source_identity_id(
                    source_url='https://www.linkedin.com/company/example',
                    raw_fact_role='external_social_profile',
                ),
            )
            linkedin_checks = conn.execute("""
                SELECT
                    b3s_history.evidence_vault_is_strict_linkedin_company_url(
                        'https://www.linkedin.com/company/a'
                    ),
                    b3s_history.evidence_vault_is_strict_linkedin_company_url(
                        'https://www.linkedin.com/company/example-company'
                    ),
                    b3s_history.evidence_vault_is_strict_linkedin_company_url(
                        'https://www.linkedin.com/company/Example'
                    ),
                    b3s_history.evidence_vault_is_strict_linkedin_company_url(
                        'https://www.linkedin.com/company/example company'
                    ),
                    b3s_history.evidence_vault_is_strict_linkedin_company_url(
                        'https://www.linkedin.com/company/éxample'
                    ),
                    b3s_history.evidence_vault_is_strict_linkedin_company_url(
                        'https://www.linkedin.com/company/example_'
                    ),
                    b3s_history.evidence_vault_is_strict_linkedin_company_url(
                        'https://www.linkedin.com/company/example/'
                    )
            """).fetchone()
            assert linkedin_checks == (True, True, False, False, False, False, False)
            nonce_type = conn.execute("""
                SELECT format_type(attributes.atttypid, attributes.atttypmod)
                FROM pg_attribute AS attributes
                WHERE attributes.attrelid =
                    'b3s_history.evidence_vault_raw_acquisition_receipts'::regclass
                  AND attributes.attname = 'receipt_nonce'
            """).fetchone()[0]
            assert nonce_type == "uuid"
            owner = conn.execute("""
                SELECT rolcanlogin FROM pg_roles
                WHERE rolname = 'b3s_history_vault_provenance_owner'
            """).fetchone()
            assert owner == (False,)
            journal_owners = conn.execute("""
                SELECT DISTINCT owner.rolname
                FROM pg_class AS tables
                JOIN pg_namespace AS schemas ON schemas.oid = tables.relnamespace
                JOIN pg_roles AS owner ON owner.oid = tables.relowner
                WHERE schemas.nspname = 'b3s_history'
                  AND tables.relname = ANY(%s)
            """, ([
                "evidence_vault_raw_acquisition_receipts",
                "evidence_vault_raw_evidence_bindings",
                "evidence_vault_verified_c7_lineage_bindings",
                "evidence_vault_verified_c7_lineage_members",
                "evidence_vault_raw_provenance_disposition_events",
            ],)).fetchall()
            assert journal_owners == [("b3s_history_vault_provenance_owner",)]
            assert conn.execute("""
                SELECT
                    has_table_privilege(
                        'b3s_history_vault_provenance_owner',
                        'b3s_history.evidence_vault_canonical_memory_packets', 'SELECT'
                    ),
                    has_table_privilege(
                        'b3s_history_vault_provenance_owner',
                        'b3s_history.evidence_vault_operation_plans', 'SELECT'
                    )
            """).fetchone() == (True, True)
            with conn.transaction():
                conn.execute("SET LOCAL ROLE b3s_history_vault_provenance_owner")
                assert conn.execute(
                    "SELECT b3s_history.read_evidence_vault_raw_provenance(%s)",
                    ("00000000-0000-0000-0000-000000000001",),
                ).fetchone()[0] is None
                with pytest.raises(psycopg.Error, match="ingest envelope is invalid"):
                    with conn.transaction():
                        conn.execute(
                            "SELECT b3s_history.bind_evidence_vault_verified_c7_lineage('{}'::jsonb)"
                        )
            with pytest.raises(
                psycopg.Error,
                match="raw acquisition append envelope cardinality is invalid",
            ):
                with conn.transaction():
                    conn.execute(
                        "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                        (Jsonb({
                            "receipts": [dumped],
                            "evidence_bindings": [],
                            "verified_binding": {},
                            "members": [],
                        }),),
                    )

            # Raw acquisition atomically seeds immutable parents plus the
            # frozen pre-acquisition plan; it needs no operational packet,
            # accepted group, or verified binding.
            import hashlib
            from copy import deepcopy
            from datetime import datetime, timedelta, timezone
            from uuid import uuid4

            from src.services.evidence_vault_canonical_core import (
                canonical_fingerprint,
                canonical_json,
            )
            from src.services.evidence_vault_incremental_refresh import (
                build_vault_scan_plan,
            )

            workspace_id, brand_id, scan_id, capture_id = [uuid4() for _ in range(4)]
            evidence_id, watermark_id = uuid4(), uuid4()
            from src.services.evidence_vault_raw_provenance import (
                pre_receipt_snapshot_sha256,
                receipt_set_fingerprint,
            )

            raw_fragment = {"document": "Owned evidence passage", "linkedin": "https://www.linkedin.com/company/example"}
            raw_without_provenance = {"sources": {"owned": raw_fragment}}
            raw_hash = hashlib.sha256(
                canonical_json(raw_fragment).encode("utf-8")
            ).hexdigest()
            document = "Owned evidence passage"
            document_hash = hashlib.sha256(document.encode()).hexdigest()
            evidence_hash = hashlib.sha256(document.encode()).hexdigest()
            now = datetime.now(timezone.utc).replace(microsecond=0)
            session_id = str(uuid4())
            pre_snapshot_hash = pre_receipt_snapshot_sha256({
                "schema_version": "evidence-vault-pre-receipt-snapshot-v1",
                "workspace_slug": "b3s",
                "source_scan_id": "scan-raw-partial",
                "acquisition_session_id": session_id,
                "canonical_brand_domain": "example.com",
                "canonical_brand_url": "https://example.com",
                "raw_payload": raw_without_provenance,
            })
            signed = _signed_owned_receipt(
                fetched_at=now.isoformat().replace("+00:00", "Z"),
                source_scan_id="scan-raw-partial",
                acquisition_session_id=session_id,
                raw_fragment_sha256=raw_hash,
                extracted_document_sha256=document_hash,
                pre_receipt_snapshot_sha256=pre_snapshot_hash,
            )
            signed_dump = signed.model_dump(mode="json")
            raw_payload = {
                **raw_without_provenance,
                "evidence_vault_raw_provenance": {
                    "schema_version": "evidence-vault-raw-provenance-envelope-v1",
                    "workspace_slug": "b3s",
                    "source_scan_id": "scan-raw-partial",
                    "acquisition_session_id": session_id,
                    "canonical_brand_domain": "example.com",
                    "canonical_brand_url": "https://example.com",
                    "pre_receipt_snapshot_sha256": pre_snapshot_hash,
                    "signed_receipts": [signed_dump],
                    "receipt_set_fingerprint": receipt_set_fingerprint([signed_dump]),
                    "external_identity_provenance": None,
                },
            }
            capture_hash = hashlib.sha256(canonical_json(raw_payload).encode()).hexdigest()
            receipt_id, raw_binding_id = uuid4(), uuid4()
            claims = signed_dump["claims"]
            receipt_row = {
                "id": str(receipt_id),
                "workspace_id": str(workspace_id),
                "brand_id": str(brand_id),
                "scan_run_id": str(scan_id),
                "source_scan_id": "scan-raw-partial",
                "capture_id": str(capture_id),
                "watermark_event_id": None,
                "capture_sequence": None,
                "receipt_schema_version": claims["schema_version"],
                "freshness_policy_version": claims["freshness_policy_version"],
                "signature_schema_version": signed_dump["signature_schema"],
                "key_id": claims["key_id"],
                "receipt_nonce": claims["receipt_nonce"],
                "acquisition_session_id": claims["acquisition_session_id"],
                "workspace_slug": claims["workspace_slug"],
                "canonical_brand": claims["canonical_brand_domain"],
                "channel_role": claims["channel_role"],
                "provider": claims["provider"],
                "acquisition_mode": claims["acquisition"]["acquisition_mode"],
                "pre_receipt_snapshot_sha256": claims["pre_receipt_snapshot_sha256"],
                "source_url": claims["acquisition"]["final_url"],
                "raw_fragment_pointer": claims["raw_fragment_json_pointer"],
                "raw_fragment_sha256": claims["raw_fragment_sha256"],
                "extracted_document_sha256": claims["extracted_document_sha256"],
                "extractor_version": claims["extractor_version"],
                "external_identity_provenance_fingerprint": None,
                "external_identity_provenance": None,
                "fetched_at": claims["fetched_at"],
                "eligible_until": (now + timedelta(hours=24)).isoformat(),
                "claims": claims,
                "receipt_fingerprint": signed_dump["receipt_fingerprint"],
                "signed_payload": {
                    key: signed_dump[key]
                    for key in ("signature_schema", "claims", "receipt_fingerprint")
                },
                "signature": signed_dump["signature"],
            }
            passage_locator = {
                "kind": "utf8_byte_range",
                "extracted_start": 0,
                "extracted_end": len(document.encode()),
                "evidence_start": 0,
                "evidence_end": len(document.encode()),
            }
            raw_binding_fingerprint = canonical_fingerprint(
                "evidence-vault-raw-evidence-binding-v1",
                {
                    "receipt_fingerprint": signed_dump["receipt_fingerprint"],
                    "evidence_record_id": str(evidence_id),
                    "evidence_ref": "owned.ref",
                    "source_url": "https://example.com",
                    "channel_role": "owned_web",
                    "extractor_schema_version": "evidence-vault-deterministic-extractor-v1",
                    "extractor_version": "evidence-vault-deterministic-extractor-v1",
                    "extracted_document_sha256": document_hash,
                    "passage_locator": passage_locator,
                    "passage_sha256": document_hash,
                    "evidence_record_content_hash": evidence_hash,
                },
            )
            binding_row = {
                "id": str(raw_binding_id),
                "workspace_id": str(workspace_id),
                "brand_id": str(brand_id),
                "scan_run_id": str(scan_id),
                "capture_id": str(capture_id),
                "receipt_id": str(receipt_id),
                "channel_role": "owned_web",
                "evidence_record_id": str(evidence_id),
                "evidence_ref": "owned.ref",
                "source_url": "https://example.com",
                "extractor_schema_version": "evidence-vault-deterministic-extractor-v1",
                "extractor_version": "evidence-vault-deterministic-extractor-v1",
                "extracted_document": document,
                "extracted_document_sha256": document_hash,
                "passage_locator": passage_locator,
                "passage_text": document,
                "passage_sha256": document_hash,
                "evidence_record_content_hash": evidence_hash,
                "binding_fingerprint": raw_binding_fingerprint,
            }
            now_text = now.isoformat()
            raw_observation = {
                "schema_version": "b3s-capture-observation-v1",
                "source_scan_id": "scan-raw-partial",
                "source_run_id": "",
                "brand_name": "Example",
                "url": "https://example.com",
                "observed_at": now_text,
                "recorded_at": now_text,
                "pipeline_version": "test-v1",
                "acquisition_state": "completed",
                "acquisition_summary": {},
                "limitations": [],
                "capture_payload": raw_payload,
                "evidence_records": [{
                    "ref": "owned.ref", "source": "web",
                    "evidence_type": "text", "url": "https://example.com",
                    "content": document, "metadata": {"source_class": "owned_copy"},
                }],
                "acquisition_attempts": [],
                "artifacts": [],
                "metadata": {},
            }
            observation_hash = hashlib.sha256(
                canonical_json(raw_observation).encode()
            ).hexdigest()
            operation_plan_id = uuid4()
            operation_plan_payload = build_vault_scan_plan(
                brand_identity="example.com",
                subject_url="https://example.com",
                mode="baseline",
                current_evidence_records=[{
                    "ref": "owned.ref", "source": "web",
                    "evidence_type": "text", "url": "https://example.com",
                    "content": document, "metadata": {"source_class": "owned_copy"},
                }],
            )
            operation_plan_row = {
                "id": str(operation_plan_id),
                "workspace_id": str(workspace_id),
                "brand_id": str(brand_id),
                "scan_run_id": str(scan_id),
                "observation_hash": observation_hash,
                "operation_plan_fingerprint": operation_plan_payload[
                    "operation_plan_fingerprint"
                ],
                "canonical_memory_version": None,
                "mode": "baseline",
                "status": "pending",
                "plan_payload": operation_plan_payload,
            }
            watermark_fingerprint = canonical_fingerprint(
                "evidence-vault-capture-watermark-event-v1",
                {
                    "schema_version": "evidence-vault-capture-watermark-event-v1",
                    "brand_id": str(brand_id),
                    "capture_id": str(capture_id),
                    "capture_sequence": 1,
                    "previous_event_id": None,
                    "previous_event_fingerprint": None,
                    "capture_content_hash": capture_hash,
                    "capture_observation_hash": observation_hash,
                    "append_origin": "capture_observation_commit",
                },
            )
            raw_envelope = {
                "workspace": {
                    "id": str(workspace_id), "slug": "b3s", "name": "B3S"
                },
                "brand": {
                    "id": str(brand_id), "workspace_id": str(workspace_id),
                    "canonical_domain": "example.com", "display_name": "Example",
                    "canonical_url": "https://example.com",
                    "first_observed_at": now_text, "latest_observed_at": now_text,
                },
                "scan_run": {
                    "id": str(scan_id), "workspace_id": str(workspace_id),
                    "brand_id": str(brand_id), "source_scan_id": "scan-raw-partial",
                    "status": "completed", "pipeline_version": "test-v1",
                    "acquisition_state": "completed", "requested_at": now_text,
                    "recorded_at": now_text,
                    "request_payload": raw_observation,
                    "metadata": {"observation_hash": observation_hash},
                },
                "operation_plan": operation_plan_row,
                "capture": {
                    "id": str(capture_id), "scan_run_id": str(scan_id),
                    "brand_id": str(brand_id), "observed_at": now_text,
                    "recorded_at": now_text, "source_url": "https://example.com",
                    "content_hash": capture_hash, "raw_payload": raw_payload,
                },
                "evidence_records": [{
                    "id": str(evidence_id), "capture_id": str(capture_id),
                    "evidence_ref": "owned.ref", "source": "web",
                    "source_class": "owned_copy", "evidence_type": "text",
                    "url": "https://example.com", "content": document,
                    "content_hash": evidence_hash,
                    "metadata": {"source_class": "owned_copy"},
                }],
                "watermark_event": {
                    # The append owns sequence/predecessor/id/fingerprint under
                    # the brand lock; callers supply only bound content hashes.
                    "capture_content_hash": capture_hash,
                    "capture_observation_hash": observation_hash,
                    "append_origin": "capture_observation_commit",
                },
                "receipts": [receipt_row],
                "evidence_bindings": [binding_row],
            }
            conn.commit()
            conn.execute(
                psycopg_sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    psycopg_sql.Identifier(execute_only_role),
                    psycopg_sql.Literal("execute-only-test-password"),
                )
            )
            conn.execute(
                psycopg_sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    psycopg_sql.Identifier(governance_role),
                    psycopg_sql.Literal("governance-test-password"),
                )
            )
            conn.execute(
                psycopg_sql.SQL("GRANT USAGE ON SCHEMA b3s_history TO {}, {}").format(
                    psycopg_sql.Identifier(execute_only_role),
                    psycopg_sql.Identifier(governance_role),
                )
            )
            conn.execute(
                psycopg_sql.SQL(
                    "GRANT EXECUTE ON FUNCTION "
                    "b3s_history.append_evidence_vault_raw_acquisition(jsonb), "
                    "b3s_history.read_evidence_vault_raw_acquisition(text, text) TO {}"
                ).format(psycopg_sql.Identifier(execute_only_role))
            )
            conn.execute(
                psycopg_sql.SQL(
                    "GRANT EXECUTE ON FUNCTION "
                    "b3s_history.bind_evidence_vault_verified_c7_lineage(jsonb), "
                    "b3s_history.read_evidence_vault_raw_provenance(uuid) TO {}"
                ).format(psycopg_sql.Identifier(governance_role))
            )
            conn.commit()
            failed_retryable_envelope = deepcopy(raw_envelope)
            failed_retryable_envelope["operation_plan"]["status"] = "failed_retryable"
            with pytest.raises(psycopg.Error, match="not pre-interpretation"):
                with conn.transaction():
                    conn.execute(
                        psycopg_sql.SQL("SET LOCAL ROLE {}").format(
                            psycopg_sql.Identifier(execute_only_role)
                        )
                    )
                    conn.execute(
                        "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                        (Jsonb(failed_retryable_envelope),),
                    )

            with conn.transaction():
                conn.execute(
                    psycopg_sql.SQL("SET LOCAL ROLE {}").format(
                        psycopg_sql.Identifier(execute_only_role)
                    )
                )
                outcome = conn.execute(
                    "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                    (Jsonb(raw_envelope),),
                ).fetchone()[0]
            assert outcome["receipt_ids"] == [str(receipt_id)]
            assert outcome["capture_sequence"] == 1
            assert outcome["previous_event_id"] is None
            assert outcome["watermark_event_id"] != str(watermark_id)
            from psycopg.conninfo import make_conninfo

            def assert_provenance_role_is_execute_only(role_conn):
                forbidden_statements = (
                    "SET ROLE b3s_history_vault_provenance_owner",
                    "CREATE TABLE b3s_history.forbidden_role_object (id integer)",
                    "INSERT INTO b3s_history.evidence_vault_raw_acquisition_receipts DEFAULT VALUES",
                    "UPDATE b3s_history.evidence_vault_raw_acquisition_receipts SET id = id",
                    "DELETE FROM b3s_history.evidence_vault_raw_acquisition_receipts",
                    "TRUNCATE b3s_history.evidence_vault_raw_acquisition_receipts",
                    "ALTER TABLE b3s_history.evidence_vault_raw_acquisition_receipts DISABLE TRIGGER ALL",
                )
                for forbidden_statement in forbidden_statements:
                    with pytest.raises(psycopg.Error, match="permission denied|must be owner"):
                        with role_conn.transaction():
                            role_conn.execute(forbidden_statement)

            execute_only_dsn = make_conninfo(
                dsn,
                user=execute_only_role,
                password="execute-only-test-password",
            )
            with psycopg.connect(execute_only_dsn) as execute_only_conn:
                replay = execute_only_conn.execute(
                    "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                    (Jsonb(raw_envelope),),
                ).fetchone()[0]
                assert replay["capture_id"] == str(capture_id)
                assert execute_only_conn.execute(
                    "SELECT b3s_history.read_evidence_vault_raw_acquisition(%s, %s)",
                    ("b3s", "scan-raw-partial"),
                ).fetchone()[0]["capture"]["id"] == str(capture_id)
                with pytest.raises(psycopg.Error, match="permission denied"):
                    with execute_only_conn.transaction():
                        execute_only_conn.execute(
                            "SELECT * FROM b3s_history.scan_runs"
                        )
                assert_provenance_role_is_execute_only(execute_only_conn)
            watermark_id = outcome["watermark_event_id"]
            watermark_fingerprint = outcome["watermark_event_fingerprint"]
            receipt_row["watermark_event_id"] = watermark_id
            receipt_row["capture_sequence"] = outcome["capture_sequence"]
            with conn.transaction():
                conn.execute(
                    psycopg_sql.SQL("SET LOCAL ROLE {}").format(
                        psycopg_sql.Identifier(execute_only_role)
                    )
                )
                stored_raw = conn.execute(
                    "SELECT b3s_history.read_evidence_vault_raw_acquisition(%s, %s)",
                    ("b3s", "scan-raw-partial"),
                ).fetchone()[0]
                assert stored_raw["capture"]["id"] == str(capture_id)
                assert stored_raw["operation_plan"]["id"] == str(operation_plan_id)
                assert stored_raw["watermark_event"]["id"] == watermark_id
                assert len(stored_raw["receipts"]) == 1
                assert len(stored_raw["evidence_bindings"]) == 1
                with pytest.raises(psycopg.Error, match="permission denied"):
                    with conn.transaction():
                        conn.execute("SELECT * FROM b3s_history.scan_runs")
            # Exact replay succeeds; divergence under the same content fingerprint does not.
            with conn.transaction():
                conn.execute(
                    psycopg_sql.SQL("SET LOCAL ROLE {}").format(
                        psycopg_sql.Identifier(execute_only_role)
                    )
                )
                conn.execute(
                    "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                    (Jsonb(raw_envelope),),
                )
            forged = {**raw_envelope, "receipts": [{**receipt_row, "id": str(uuid4())}]}
            with pytest.raises(
                psycopg.Error,
                match="cannot be upgraded|replay diverges|binding provenance is invalid",
            ):
                with conn.transaction():
                    conn.execute(
                        "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                        (Jsonb(forged),),
                    )
            counts = conn.execute("""
                SELECT
                    (SELECT count(*) FROM b3s_history.evidence_vault_raw_acquisition_receipts),
                    (SELECT count(*) FROM b3s_history.evidence_vault_raw_evidence_bindings),
                    (SELECT count(*) FROM b3s_history.evidence_vault_verified_c7_lineage_bindings),
                    (SELECT count(*) FROM b3s_history.evidence_vault_verified_c7_lineage_members),
                    (SELECT count(*) FROM b3s_history.evidence_vault_raw_provenance_disposition_events)
            """).fetchone()
            assert tuple(counts) == (1, 1, 0, 0, 0)
            conn.commit()  # fire deferred durable/raw validators before negative checks

            with pytest.raises(psycopg.Error, match="scan replay diverges"):
                with conn.transaction():
                    conn.execute("SET LOCAL session_replication_role = replica")
                    conn.execute("""
                        UPDATE b3s_history.scan_runs
                        SET metadata = metadata || '{"unexpected": true}'::jsonb
                        WHERE id = %s
                    """, (scan_id,))
                    conn.execute("SET LOCAL session_replication_role = origin")
                    conn.execute(
                        "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                        (Jsonb(raw_envelope),),
                    )

            for tampered_column, tampered_value in (
                ("workspace_slug", "b3s-tampered"),
                ("canonical_brand", "tampered.example"),
            ):
                with pytest.raises(psycopg.Error, match="raw receipt replay diverges"):
                    with conn.transaction():
                        conn.execute("SET LOCAL session_replication_role = replica")
                        conn.execute(
                            psycopg_sql.SQL(
                                "UPDATE b3s_history.evidence_vault_raw_acquisition_receipts "
                                "SET {} = %s WHERE id = %s"
                            ).format(psycopg_sql.Identifier(tampered_column)),
                            (tampered_value, receipt_id),
                        )
                        conn.execute("SET LOCAL session_replication_role = origin")
                        conn.execute(
                            "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                            (Jsonb(raw_envelope),),
                        )

            for destructive_sql in (
                "DELETE FROM b3s_history.evidence_vault_capture_watermark_events "
                "WHERE id = %s",
                "DELETE FROM b3s_history.evidence_vault_raw_acquisition_receipts "
                "WHERE id = %s",
            ):
                target_id = watermark_id if "watermark" in destructive_sql else receipt_id
                with pytest.raises(psycopg.Error, match="cannot be upgraded"):
                    with conn.transaction():
                        conn.execute("SET LOCAL session_replication_role = replica")
                        conn.execute(destructive_sql, (target_id,))
                        conn.execute("SET LOCAL session_replication_role = origin")
                        conn.execute(
                            "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                            (Jsonb(raw_envelope),),
                        )

            # A stored two-receipt/two-binding capture cannot be replayed as
            # an owned-only subset. Replica mode builds only the adversarial
            # fixture; the failed call rolls every fixture mutation back.
            subset_receipt_id, subset_binding_id = uuid4(), uuid4()
            subset_receipt_fp, subset_binding_fp = "b" * 64, "c" * 64
            with pytest.raises(psycopg.Error, match="cannot be upgraded"):
                with conn.transaction():
                    conn.execute("SET LOCAL session_replication_role = replica")
                    conn.execute("""
                        INSERT INTO b3s_history.evidence_vault_raw_acquisition_receipts (
                            id, workspace_id, brand_id, scan_run_id, source_scan_id,
                            capture_id, watermark_event_id, capture_sequence,
                            receipt_schema_version, freshness_policy_version,
                            signature_schema_version, key_id, receipt_nonce,
                            acquisition_session_id, workspace_slug, canonical_brand,
                            channel_role, provider, acquisition_mode,
                            pre_receipt_snapshot_sha256, source_url,
                            raw_fragment_pointer, raw_fragment_sha256,
                            extracted_document_sha256, extractor_version,
                            external_identity_provenance_fingerprint,
                            external_identity_provenance, fetched_at, eligible_until,
                            claims, receipt_fingerprint, signed_payload, signature
                        )
                        SELECT
                            %s, workspace_id, brand_id, scan_run_id, source_scan_id,
                            capture_id, watermark_event_id, capture_sequence,
                            receipt_schema_version, freshness_policy_version,
                            signature_schema_version, key_id || '-subset', %s,
                            acquisition_session_id, workspace_slug, canonical_brand,
                            'external_social_profile', 'exa', 'provider_api',
                            pre_receipt_snapshot_sha256,
                            'https://www.linkedin.com/company/example',
                            raw_fragment_pointer, raw_fragment_sha256,
                            extracted_document_sha256, extractor_version,
                            %s, '{}'::jsonb, fetched_at, eligible_until,
                            claims, %s, signed_payload, signature
                        FROM b3s_history.evidence_vault_raw_acquisition_receipts
                        WHERE id = %s
                    """, (
                        subset_receipt_id, uuid4(), "a" * 64,
                        subset_receipt_fp, receipt_id,
                    ))
                    conn.execute("""
                        INSERT INTO b3s_history.evidence_vault_raw_evidence_bindings (
                            id, workspace_id, brand_id, scan_run_id, capture_id,
                            receipt_id, channel_role, evidence_record_id,
                            evidence_ref, source_url, extractor_schema_version,
                            extractor_version, extracted_document,
                            extracted_document_sha256, passage_locator, passage_text,
                            passage_sha256, evidence_record_content_hash,
                            binding_fingerprint
                        )
                        SELECT
                            %s, workspace_id, brand_id, scan_run_id, capture_id,
                            %s, 'external_social_profile', %s,
                            'external.ref',
                            'https://www.linkedin.com/company/example',
                            extractor_schema_version, extractor_version,
                            extracted_document, extracted_document_sha256,
                            passage_locator, passage_text, passage_sha256,
                            evidence_record_content_hash, %s
                        FROM b3s_history.evidence_vault_raw_evidence_bindings
                        WHERE id = %s
                    """, (
                        subset_binding_id, subset_receipt_id, uuid4(),
                        subset_binding_fp, raw_binding_id,
                    ))
                    conn.execute("""
                        UPDATE b3s_history.captures
                        SET raw_payload = jsonb_set(
                            raw_payload,
                            '{evidence_vault_raw_provenance,signed_receipts}',
                            (raw_payload #>
                                '{evidence_vault_raw_provenance,signed_receipts}')
                            || jsonb_build_array(jsonb_build_object(
                                'receipt_fingerprint', %s::text
                            ))
                        )
                        WHERE id = %s
                    """, (subset_receipt_fp, capture_id))
                    conn.execute("SET LOCAL session_replication_role = origin")
                    conn.execute(
                        "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                        (Jsonb(raw_envelope),),
                    )

            # A later capture for the same stable brand advances aggregate
            # brand fields, while replaying the older complete capture remains
            # valid and must not roll those fields back.
            later_now = datetime.now(timezone.utc).replace(microsecond=0)
            if later_now <= now:
                later_now = now + timedelta(seconds=1)
            later_text = later_now.isoformat()
            later_scan_id, later_capture_id = uuid4(), uuid4()
            later_evidence_id, later_watermark_id = uuid4(), uuid4()
            later_receipt_id, later_raw_binding_id = uuid4(), uuid4()
            later_source_scan_id = "scan-raw-later"
            later_session_id = str(uuid4())
            later_pre_snapshot_hash = pre_receipt_snapshot_sha256({
                "schema_version": "evidence-vault-pre-receipt-snapshot-v1",
                "workspace_slug": "b3s",
                "source_scan_id": later_source_scan_id,
                "acquisition_session_id": later_session_id,
                "canonical_brand_domain": "example.com",
                "canonical_brand_url": "https://example.com",
                "raw_payload": raw_without_provenance,
            })
            later_signed = _signed_owned_receipt(
                fetched_at=later_now.isoformat().replace("+00:00", "Z"),
                source_scan_id=later_source_scan_id,
                acquisition_session_id=later_session_id,
                raw_fragment_sha256=raw_hash,
                extracted_document_sha256=document_hash,
                pre_receipt_snapshot_sha256=later_pre_snapshot_hash,
            )
            later_signed_dump = later_signed.model_dump(mode="json")
            later_raw_payload = {
                **raw_without_provenance,
                "evidence_vault_raw_provenance": {
                    "schema_version": "evidence-vault-raw-provenance-envelope-v1",
                    "workspace_slug": "b3s",
                    "source_scan_id": later_source_scan_id,
                    "acquisition_session_id": later_session_id,
                    "canonical_brand_domain": "example.com",
                    "canonical_brand_url": "https://example.com",
                    "pre_receipt_snapshot_sha256": later_pre_snapshot_hash,
                    "signed_receipts": [later_signed_dump],
                    "receipt_set_fingerprint": receipt_set_fingerprint(
                        [later_signed_dump]
                    ),
                    "external_identity_provenance": None,
                },
            }
            later_capture_hash = hashlib.sha256(
                canonical_json(later_raw_payload).encode()
            ).hexdigest()
            later_claims = later_signed_dump["claims"]
            later_receipt_row = {
                **receipt_row,
                "id": str(later_receipt_id),
                "scan_run_id": str(later_scan_id),
                "source_scan_id": later_source_scan_id,
                "capture_id": str(later_capture_id),
                "watermark_event_id": str(later_watermark_id),
                "capture_sequence": 2,
                "key_id": later_claims["key_id"],
                "receipt_nonce": later_claims["receipt_nonce"],
                "acquisition_session_id": later_claims["acquisition_session_id"],
                "pre_receipt_snapshot_sha256": later_claims[
                    "pre_receipt_snapshot_sha256"
                ],
                "fetched_at": later_claims["fetched_at"],
                "eligible_until": (later_now + timedelta(hours=24)).isoformat(),
                "claims": later_claims,
                "receipt_fingerprint": later_signed_dump["receipt_fingerprint"],
                "signed_payload": {
                    key: later_signed_dump[key]
                    for key in ("signature_schema", "claims", "receipt_fingerprint")
                },
                "signature": later_signed_dump["signature"],
            }
            later_raw_binding_fingerprint = canonical_fingerprint(
                "evidence-vault-raw-evidence-binding-v1",
                {
                    "receipt_fingerprint": later_signed_dump["receipt_fingerprint"],
                    "evidence_record_id": str(later_evidence_id),
                    "evidence_ref": "owned.ref",
                    "source_url": "https://example.com",
                    "channel_role": "owned_web",
                    "extractor_schema_version":
                        "evidence-vault-deterministic-extractor-v1",
                    "extractor_version": "evidence-vault-deterministic-extractor-v1",
                    "extracted_document_sha256": document_hash,
                    "passage_locator": passage_locator,
                    "passage_sha256": document_hash,
                    "evidence_record_content_hash": evidence_hash,
                },
            )
            later_binding_row = {
                **binding_row,
                "id": str(later_raw_binding_id),
                "scan_run_id": str(later_scan_id),
                "capture_id": str(later_capture_id),
                "receipt_id": str(later_receipt_id),
                "evidence_record_id": str(later_evidence_id),
                "binding_fingerprint": later_raw_binding_fingerprint,
            }
            later_observation = {
                **raw_observation,
                "source_scan_id": later_source_scan_id,
                "observed_at": later_text,
                "recorded_at": later_text,
                "capture_payload": later_raw_payload,
            }
            later_observation_hash = hashlib.sha256(
                canonical_json(later_observation).encode()
            ).hexdigest()
            later_operation_plan_id = uuid4()
            later_operation_plan_payload = build_vault_scan_plan(
                brand_identity="example.com",
                subject_url="https://example.com",
                mode="baseline",
                current_evidence_records=[{
                    "ref": "owned.ref", "source": "web",
                    "evidence_type": "text", "url": "https://example.com",
                    "content": document, "metadata": {"source_class": "owned_copy"},
                }],
            )
            later_operation_plan_row = {
                "id": str(later_operation_plan_id),
                "workspace_id": str(workspace_id),
                "brand_id": str(brand_id),
                "scan_run_id": str(later_scan_id),
                "observation_hash": later_observation_hash,
                "operation_plan_fingerprint": later_operation_plan_payload[
                    "operation_plan_fingerprint"
                ],
                "canonical_memory_version": None,
                "mode": "baseline",
                "status": "pending",
                "plan_payload": later_operation_plan_payload,
            }
            later_watermark_fingerprint = canonical_fingerprint(
                "evidence-vault-capture-watermark-event-v1",
                {
                    "schema_version": "evidence-vault-capture-watermark-event-v1",
                    "brand_id": str(brand_id),
                    "capture_id": str(later_capture_id),
                    "capture_sequence": 2,
                    "previous_event_id": str(watermark_id),
                    "previous_event_fingerprint": watermark_fingerprint,
                    "capture_content_hash": later_capture_hash,
                    "capture_observation_hash": later_observation_hash,
                    "append_origin": "capture_observation_commit",
                },
            )
            later_envelope = {
                "workspace": raw_envelope["workspace"],
                "brand": {
                    **raw_envelope["brand"],
                    "latest_observed_at": later_text,
                },
                "scan_run": {
                    **raw_envelope["scan_run"],
                    "id": str(later_scan_id),
                    "source_scan_id": later_source_scan_id,
                    "requested_at": later_text,
                    "recorded_at": later_text,
                    "request_payload": later_observation,
                    "metadata": {"observation_hash": later_observation_hash},
                },
                "operation_plan": later_operation_plan_row,
                "capture": {
                    **raw_envelope["capture"],
                    "id": str(later_capture_id),
                    "scan_run_id": str(later_scan_id),
                    "observed_at": later_text,
                    "recorded_at": later_text,
                    "content_hash": later_capture_hash,
                    "raw_payload": later_raw_payload,
                },
                "evidence_records": [{
                    **raw_envelope["evidence_records"][0],
                    "id": str(later_evidence_id),
                    "capture_id": str(later_capture_id),
                }],
                "watermark_event": {
                    **raw_envelope["watermark_event"],
                    "id": str(later_watermark_id),
                    "capture_id": str(later_capture_id),
                    "capture_sequence": 2,
                    "previous_event_id": str(watermark_id),
                    "previous_event_fingerprint": watermark_fingerprint,
                    "capture_content_hash": later_capture_hash,
                    "capture_observation_hash": later_observation_hash,
                    "event_fingerprint": later_watermark_fingerprint,
                },
                "receipts": [later_receipt_row],
                "evidence_bindings": [later_binding_row],
            }
            with conn.transaction():
                conn.execute(
                    psycopg_sql.SQL("SET LOCAL ROLE {}").format(
                        psycopg_sql.Identifier(execute_only_role)
                    )
                )
                later_outcome = conn.execute(
                    "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                    (Jsonb(later_envelope),),
                ).fetchone()[0]
            assert later_outcome["capture_sequence"] == 2
            assert later_outcome["previous_event_id"] == watermark_id
            with conn.transaction():
                conn.execute(
                    psycopg_sql.SQL("SET LOCAL ROLE {}").format(
                        psycopg_sql.Identifier(execute_only_role)
                    )
                )
                conn.execute(
                    "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                    (Jsonb(raw_envelope),),
                )
            assert conn.execute(
                "SELECT latest_observed_at FROM b3s_history.brands WHERE id = %s",
                (brand_id,),
            ).fetchone()[0] == later_now
            conn.commit()

            from copy import deepcopy

            # True initial verified-C7 bind: two independently signed raw
            # channels are appended with their pre-acquisition plan, then an
            # EXECUTE-only LOGIN governance role performs the 2/2/2 bind.
            import base64
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
            from src.services.evidence_vault_raw_provenance import (
                C7_LIVE_FRESHNESS_POLICY_VERSION,
                PUBLIC_KEY_REGISTRY_VERSION,
                RAW_ACQUISITION_RECEIPT_VERSION,
                DirectAcquisition,
                ExternalIdentityProvenance,
                ProviderApiAcquisition,
                RawAcquisitionReceiptClaims,
                public_key_registry_fingerprint,
                sign_raw_acquisition_receipt,
            )

            positive_now = datetime.now(timezone.utc).replace(microsecond=0)
            positive_text = positive_now.isoformat()
            positive_source_scan = "scan-raw-verified-c7"
            positive_session = str(uuid4())
            positive_scan_id, positive_capture_id = uuid4(), uuid4()
            positive_plan_id = uuid4()
            owned_evidence_id, external_evidence_id = uuid4(), uuid4()
            owned_receipt_id, external_receipt_id = uuid4(), uuid4()
            owned_raw_binding_id, external_raw_binding_id = uuid4(), uuid4()
            positive_binding_id = uuid4()
            owned_document = "Owned verified evidence"
            external_document = "Nuestra misión"
            positive_raw_without = {
                "sources": {
                    "owned": {
                        "document": owned_document,
                        "linkedin": "https://www.linkedin.com/company/example",
                    },
                    "external": {
                        "document": external_document,
                        "website": "https://example.com",
                    },
                }
            }
            positive_pre_snapshot = pre_receipt_snapshot_sha256({
                "schema_version": "evidence-vault-pre-receipt-snapshot-v1",
                "workspace_slug": "b3s",
                "source_scan_id": positive_source_scan,
                "acquisition_session_id": positive_session,
                "canonical_brand_domain": "example.com",
                "canonical_brand_url": "https://example.com",
                "raw_payload": positive_raw_without,
            })
            owned_fragment_hash = hashlib.sha256(canonical_json(
                positive_raw_without["sources"]["owned"]
            ).encode()).hexdigest()
            external_fragment_hash = hashlib.sha256(canonical_json(
                positive_raw_without["sources"]["external"]
            ).encode()).hexdigest()
            owned_document_hash = hashlib.sha256(owned_document.encode()).hexdigest()
            external_document_hash = hashlib.sha256(
                external_document.encode()
            ).hexdigest()
            signing_key = Ed25519PrivateKey.generate()
            public_key_bytes = signing_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            positive_registry = {
                "schema_version": PUBLIC_KEY_REGISTRY_VERSION,
                "current_key_id": "verified-c7-test-key",
                "keys": {
                    "verified-c7-test-key": {
                        "version": 1,
                        "status": "current",
                        "signing_not_before": "2026-01-01T00:00:00Z",
                        "signing_ended_at": None,
                        "public_key_base64": base64.b64encode(
                            public_key_bytes
                        ).decode("ascii"),
                    }
                },
            }
            owned_claims = RawAcquisitionReceiptClaims(
                schema_version=RAW_ACQUISITION_RECEIPT_VERSION,
                freshness_policy_version=C7_LIVE_FRESHNESS_POLICY_VERSION,
                key_id="verified-c7-test-key",
                receipt_nonce=str(uuid4()),
                acquisition_session_id=positive_session,
                workspace_slug="b3s",
                source_scan_id=positive_source_scan,
                canonical_brand_domain="example.com",
                channel_role="owned_web",
                pre_receipt_snapshot_sha256=positive_pre_snapshot,
                provider="direct_http",
                acquisition=DirectAcquisition(
                    acquisition_mode="direct_http",
                    requested_url="https://example.com/start",
                    redirect_chain=[{
                        "request_url": "https://example.com/start",
                        "status_code": 301,
                        "location_url": "https://www.example.com/",
                    }],
                    final_url="https://www.example.com/",
                ),
                fetched_at=positive_now.isoformat().replace("+00:00", "Z"),
                status_code=200,
                selected_headers={"content-type": "text/html"},
                media_type="text/html",
                byte_count=128,
                raw_fragment_json_pointer="/sources/owned",
                raw_fragment_sha256=owned_fragment_hash,
                extracted_document_sha256=owned_document_hash,
                extractor_version="evidence-vault-deterministic-extractor-v1",
                external_identity_provenance_fingerprint=None,
            )
            positive_owned_signed = sign_raw_acquisition_receipt(
                owned_claims,
                private_key=signing_key,
                public_key_registry=positive_registry,
            )
            owned_source_identity_id = evidence_memory_source_identity_id(
                source_url="https://example.com", raw_fact_role="owned_web"
            )
            external_source_identity_id = evidence_memory_source_identity_id(
                source_url="https://www.linkedin.com/company/example",
                raw_fact_role="external_social_profile",
            )
            external_provenance = {
                "schema_version": "external-identity-provenance-v1",
                "policy_version":
                    "evidence-vault-external-identity-association-policy-v1",
                "association_method": "owned_raw_links_external_profile",
                "canonical_brand_domain": "example.com",
                "owned_source_url": "https://example.com",
                "external_source_url":
                    "https://www.linkedin.com/company/example",
                "proof_receipt_fingerprint":
                    positive_owned_signed.receipt_fingerprint,
                "raw_fact_role": "owned_web",
                "raw_fact_json_pointer": "/sources/owned/linkedin",
                "raw_fact_sha256": hashlib.sha256(canonical_json(
                    "https://www.linkedin.com/company/example"
                ).encode()).hexdigest(),
                "source_identity_schema_version": "evidence-memory-document-v2",
                "owned_source_identity_id": owned_source_identity_id,
                "external_source_identity_id": external_source_identity_id,
            }
            ExternalIdentityProvenance.model_validate(external_provenance)
            external_provenance_fingerprint = canonical_fingerprint(
                "external-identity-provenance-v1", external_provenance
            )
            external_claims = RawAcquisitionReceiptClaims(
                schema_version=RAW_ACQUISITION_RECEIPT_VERSION,
                freshness_policy_version=C7_LIVE_FRESHNESS_POLICY_VERSION,
                key_id="verified-c7-test-key",
                receipt_nonce=str(uuid4()),
                acquisition_session_id=positive_session,
                workspace_slug="b3s",
                source_scan_id=positive_source_scan,
                canonical_brand_domain="example.com",
                channel_role="external_social_profile",
                pre_receipt_snapshot_sha256=positive_pre_snapshot,
                provider="exa",
                acquisition=ProviderApiAcquisition(
                    acquisition_mode="provider_api",
                    provider_request_fingerprint="7" * 64,
                    result_ordinal=0,
                    reported_source_url=
                        "https://www.linkedin.com/company/example",
                    redirect_chain=[],
                ),
                fetched_at=positive_now.isoformat().replace("+00:00", "Z"),
                status_code=200,
                selected_headers={"content-type": "text/html"},
                media_type="text/html",
                byte_count=128,
                raw_fragment_json_pointer="/sources/external",
                raw_fragment_sha256=external_fragment_hash,
                extracted_document_sha256=external_document_hash,
                extractor_version="evidence-vault-deterministic-extractor-v1",
                external_identity_provenance_fingerprint=
                    external_provenance_fingerprint,
            )
            positive_external_signed = sign_raw_acquisition_receipt(
                external_claims,
                private_key=signing_key,
                public_key_registry=positive_registry,
            )
            owned_signed_dump = positive_owned_signed.model_dump(mode="json")
            external_signed_dump = positive_external_signed.model_dump(mode="json")
            positive_signed_dumps = sorted(
                [owned_signed_dump, external_signed_dump],
                key=lambda row: row["receipt_fingerprint"],
            )
            positive_raw_payload = {
                **positive_raw_without,
                "evidence_vault_raw_provenance": {
                    "schema_version": "evidence-vault-raw-provenance-envelope-v1",
                    "workspace_slug": "b3s",
                    "source_scan_id": positive_source_scan,
                    "acquisition_session_id": positive_session,
                    "canonical_brand_domain": "example.com",
                    "canonical_brand_url": "https://example.com",
                    "pre_receipt_snapshot_sha256": positive_pre_snapshot,
                    "signed_receipts": positive_signed_dumps,
                    "receipt_set_fingerprint": receipt_set_fingerprint(
                        positive_signed_dumps
                    ),
                    "external_identity_provenance": external_provenance,
                },
            }
            positive_capture_hash = hashlib.sha256(canonical_json(
                positive_raw_payload
            ).encode()).hexdigest()

            def positive_receipt_row(
                *, signed_dump, row_id, channel_role, evidence_provenance
            ):
                row_claims = signed_dump["claims"]
                acquisition = row_claims["acquisition"]
                source_url = acquisition.get("final_url") or acquisition.get(
                    "reported_source_url"
                )
                return {
                    "id": str(row_id),
                    "workspace_id": str(workspace_id),
                    "brand_id": str(brand_id),
                    "scan_run_id": str(positive_scan_id),
                    "source_scan_id": positive_source_scan,
                    "capture_id": str(positive_capture_id),
                    "watermark_event_id": None,
                    "capture_sequence": None,
                    "receipt_schema_version": row_claims["schema_version"],
                    "freshness_policy_version":
                        row_claims["freshness_policy_version"],
                    "signature_schema_version": signed_dump["signature_schema"],
                    "key_id": row_claims["key_id"],
                    "receipt_nonce": row_claims["receipt_nonce"],
                    "acquisition_session_id":
                        row_claims["acquisition_session_id"],
                    "workspace_slug": row_claims["workspace_slug"],
                    "canonical_brand": row_claims["canonical_brand_domain"],
                    "channel_role": channel_role,
                    "provider": row_claims["provider"],
                    "acquisition_mode": acquisition["acquisition_mode"],
                    "pre_receipt_snapshot_sha256":
                        row_claims["pre_receipt_snapshot_sha256"],
                    "source_url": source_url,
                    "raw_fragment_pointer":
                        row_claims["raw_fragment_json_pointer"],
                    "raw_fragment_sha256": row_claims["raw_fragment_sha256"],
                    "extracted_document_sha256":
                        row_claims["extracted_document_sha256"],
                    "extractor_version": row_claims["extractor_version"],
                    "external_identity_provenance_fingerprint":
                        row_claims["external_identity_provenance_fingerprint"],
                    "external_identity_provenance": evidence_provenance,
                    "fetched_at": row_claims["fetched_at"],
                    "eligible_until": (
                        positive_now + timedelta(hours=24)
                    ).isoformat(),
                    "claims": row_claims,
                    "receipt_fingerprint": signed_dump["receipt_fingerprint"],
                    "signed_payload": {
                        key: signed_dump[key]
                        for key in (
                            "signature_schema", "claims", "receipt_fingerprint"
                        )
                    },
                    "signature": signed_dump["signature"],
                }

            positive_owned_receipt_row = positive_receipt_row(
                signed_dump=owned_signed_dump,
                row_id=owned_receipt_id,
                channel_role="owned_web",
                evidence_provenance=None,
            )
            positive_external_receipt_row = positive_receipt_row(
                signed_dump=external_signed_dump,
                row_id=external_receipt_id,
                channel_role="external_social_profile",
                evidence_provenance=external_provenance,
            )
            positive_evidence_rows = [
                {
                    "id": str(owned_evidence_id),
                    "capture_id": str(positive_capture_id),
                    "evidence_ref": "owned.ref",
                    "source": "web", "source_class": "owned_copy",
                    "evidence_type": "text", "url": "https://www.example.com/",
                    "content": owned_document,
                    "content_hash": owned_document_hash,
                    "metadata": {"source_class": "owned_copy"},
                },
                {
                    "id": str(external_evidence_id),
                    "capture_id": str(positive_capture_id),
                    "evidence_ref": "external.ref",
                    "source": "linkedin", "source_class": "external_proof",
                    "evidence_type": "text",
                    "url": "https://www.linkedin.com/company/example",
                    "content": external_document,
                    "content_hash": external_document_hash,
                    "metadata": {"source_class": "external_proof"},
                },
            ]

            def positive_raw_binding(
                *, row_id, receipt_row_value, evidence_row_value, document_value
            ):
                locator = {
                    "kind": "utf8_byte_range",
                    "extracted_start": 0,
                    "extracted_end": len(document_value.encode()),
                    "evidence_start": 0,
                    "evidence_end": len(document_value.encode()),
                }
                fingerprint_payload = {
                    "receipt_fingerprint":
                        receipt_row_value["receipt_fingerprint"],
                    "evidence_record_id": evidence_row_value["id"],
                    "evidence_ref": evidence_row_value["evidence_ref"],
                    "source_url": evidence_row_value["url"],
                    "channel_role": receipt_row_value["channel_role"],
                    "extractor_schema_version":
                        "evidence-vault-deterministic-extractor-v1",
                    "extractor_version":
                        "evidence-vault-deterministic-extractor-v1",
                    "extracted_document_sha256":
                        evidence_row_value["content_hash"],
                    "passage_locator": locator,
                    "passage_sha256": evidence_row_value["content_hash"],
                    "evidence_record_content_hash":
                        evidence_row_value["content_hash"],
                }
                return {
                    "id": str(row_id),
                    "workspace_id": str(workspace_id),
                    "brand_id": str(brand_id),
                    "scan_run_id": str(positive_scan_id),
                    "capture_id": str(positive_capture_id),
                    "receipt_id": receipt_row_value["id"],
                    "channel_role": receipt_row_value["channel_role"],
                    "evidence_record_id": evidence_row_value["id"],
                    "evidence_ref": evidence_row_value["evidence_ref"],
                    "source_url": evidence_row_value["url"],
                    "extractor_schema_version":
                        "evidence-vault-deterministic-extractor-v1",
                    "extractor_version":
                        "evidence-vault-deterministic-extractor-v1",
                    "extracted_document": document_value,
                    "extracted_document_sha256":
                        evidence_row_value["content_hash"],
                    "passage_locator": locator,
                    "passage_text": document_value,
                    "passage_sha256": evidence_row_value["content_hash"],
                    "evidence_record_content_hash":
                        evidence_row_value["content_hash"],
                    "binding_fingerprint": canonical_fingerprint(
                        "evidence-vault-raw-evidence-binding-v1",
                        fingerprint_payload,
                    ),
                }

            positive_owned_raw_binding = positive_raw_binding(
                row_id=owned_raw_binding_id,
                receipt_row_value=positive_owned_receipt_row,
                evidence_row_value=positive_evidence_rows[0],
                document_value=owned_document,
            )
            positive_external_raw_binding = positive_raw_binding(
                row_id=external_raw_binding_id,
                receipt_row_value=positive_external_receipt_row,
                evidence_row_value=positive_evidence_rows[1],
                document_value=external_document,
            )
            positive_observation_records = [
                {
                    "ref": row["evidence_ref"], "source": row["source"],
                    "evidence_type": row["evidence_type"], "url": row["url"],
                    "content": row["content"], "metadata": row["metadata"],
                }
                for row in positive_evidence_rows
            ]
            positive_observation = {
                "schema_version": "b3s-capture-observation-v1",
                "source_scan_id": positive_source_scan,
                "source_run_id": "", "brand_name": "Example",
                "url": "https://example.com", "observed_at": positive_text,
                "recorded_at": positive_text, "pipeline_version": "test-v1",
                "acquisition_state": "completed", "acquisition_summary": {},
                "limitations": [], "capture_payload": positive_raw_payload,
                "evidence_records": positive_observation_records,
                "acquisition_attempts": [], "artifacts": [], "metadata": {},
            }
            positive_observation_hash = hashlib.sha256(canonical_json(
                positive_observation
            ).encode()).hexdigest()
            positive_plan_payload = build_vault_scan_plan(
                brand_identity="example.com",
                subject_url="https://example.com",
                mode="baseline",
                current_evidence_records=positive_observation_records,
            )
            positive_plan_row = {
                "id": str(positive_plan_id),
                "workspace_id": str(workspace_id),
                "brand_id": str(brand_id),
                "scan_run_id": str(positive_scan_id),
                "observation_hash": positive_observation_hash,
                "operation_plan_fingerprint":
                    positive_plan_payload["operation_plan_fingerprint"],
                "canonical_memory_version": None,
                "mode": "baseline", "status": "pending",
                "plan_payload": positive_plan_payload,
            }
            positive_raw_envelope = {
                "workspace": raw_envelope["workspace"],
                "brand": {
                    **raw_envelope["brand"],
                    "latest_observed_at": positive_text,
                },
                "scan_run": {
                    "id": str(positive_scan_id),
                    "workspace_id": str(workspace_id),
                    "brand_id": str(brand_id),
                    "source_scan_id": positive_source_scan,
                    "status": "completed", "pipeline_version": "test-v1",
                    "acquisition_state": "completed",
                    "requested_at": positive_text,
                    "recorded_at": positive_text,
                    "request_payload": positive_observation,
                    "metadata": {"observation_hash": positive_observation_hash},
                },
                "operation_plan": positive_plan_row,
                "capture": {
                    "id": str(positive_capture_id),
                    "scan_run_id": str(positive_scan_id),
                    "brand_id": str(brand_id),
                    "observed_at": positive_text, "recorded_at": positive_text,
                    "source_url": "https://example.com",
                    "content_hash": positive_capture_hash,
                    "raw_payload": positive_raw_payload,
                },
                "evidence_records": positive_evidence_rows,
                "watermark_event": {
                    "capture_content_hash": positive_capture_hash,
                    "capture_observation_hash": positive_observation_hash,
                    "append_origin": "capture_observation_commit",
                },
                "receipts": [
                    positive_owned_receipt_row,
                    positive_external_receipt_row,
                ],
                "evidence_bindings": [
                    positive_owned_raw_binding,
                    positive_external_raw_binding,
                ],
            }
            one_byte_envelope = deepcopy(positive_raw_envelope)
            one_byte_binding = one_byte_envelope["evidence_bindings"][0]
            one_byte_binding["passage_locator"] = {
                "kind": "utf8_byte_range",
                "extracted_start": 0, "extracted_end": 1,
                "evidence_start": 0, "evidence_end": 1,
            }
            one_byte_binding["passage_text"] = "O"
            one_byte_binding["passage_sha256"] = hashlib.sha256(b"O").hexdigest()
            one_byte_receipt = one_byte_envelope["receipts"][0]
            one_byte_binding["binding_fingerprint"] = canonical_fingerprint(
                "evidence-vault-raw-evidence-binding-v1",
                {
                    "receipt_fingerprint": one_byte_receipt["receipt_fingerprint"],
                    "evidence_record_id": one_byte_binding["evidence_record_id"],
                    "evidence_ref": one_byte_binding["evidence_ref"],
                    "source_url": one_byte_binding["source_url"],
                    "channel_role": one_byte_binding["channel_role"],
                    "extractor_schema_version":
                        one_byte_binding["extractor_schema_version"],
                    "extractor_version": one_byte_binding["extractor_version"],
                    "extracted_document_sha256":
                        one_byte_binding["extracted_document_sha256"],
                    "passage_locator": one_byte_binding["passage_locator"],
                    "passage_sha256": one_byte_binding["passage_sha256"],
                    "evidence_record_content_hash":
                        one_byte_binding["evidence_record_content_hash"],
                },
            )
            with pytest.raises(
                psycopg.Error,
                match="locator does not reproduce a meaningful passage",
            ):
                with conn.transaction():
                    conn.execute(
                        psycopg_sql.SQL("SET LOCAL ROLE {}").format(
                            psycopg_sql.Identifier(execute_only_role)
                        )
                    )
                    conn.execute(
                        "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                        (Jsonb(one_byte_envelope),),
                    )

            oversized_envelope = deepcopy(positive_raw_envelope)
            oversized_binding = oversized_envelope["evidence_bindings"][0]
            oversized_binding["passage_locator"]["extracted_end"] = 999_999
            oversized_binding["passage_locator"]["evidence_end"] = 999_999
            oversized_receipt = oversized_envelope["receipts"][0]
            oversized_binding["binding_fingerprint"] = canonical_fingerprint(
                "evidence-vault-raw-evidence-binding-v1",
                {
                    "receipt_fingerprint": oversized_receipt["receipt_fingerprint"],
                    "evidence_record_id": oversized_binding["evidence_record_id"],
                    "evidence_ref": oversized_binding["evidence_ref"],
                    "source_url": oversized_binding["source_url"],
                    "channel_role": oversized_binding["channel_role"],
                    "extractor_schema_version":
                        oversized_binding["extractor_schema_version"],
                    "extractor_version": oversized_binding["extractor_version"],
                    "extracted_document_sha256":
                        oversized_binding["extracted_document_sha256"],
                    "passage_locator": oversized_binding["passage_locator"],
                    "passage_sha256": oversized_binding["passage_sha256"],
                    "evidence_record_content_hash":
                        oversized_binding["evidence_record_content_hash"],
                },
            )
            with pytest.raises(
                psycopg.Error,
                match="raw evidence byte locator is invalid",
            ):
                with conn.transaction():
                    conn.execute(
                        psycopg_sql.SQL("SET LOCAL ROLE {}").format(
                            psycopg_sql.Identifier(execute_only_role)
                        )
                    )
                    conn.execute(
                        "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                        (Jsonb(oversized_envelope),),
                    )

            with conn.transaction():
                conn.execute(
                    psycopg_sql.SQL("SET LOCAL ROLE {}").format(
                        psycopg_sql.Identifier(execute_only_role)
                    )
                )
                positive_raw_outcome = conn.execute(
                    "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                    (Jsonb(positive_raw_envelope),),
                ).fetchone()[0]
            assert positive_raw_outcome["capture_sequence"] == 3
            stored_positive_eligible = dict(conn.execute("""
                SELECT id::text, eligible_until
                FROM b3s_history.evidence_vault_raw_acquisition_receipts
                WHERE capture_id = %s
            """, (positive_capture_id,)).fetchall())
            for row in (
                positive_owned_receipt_row, positive_external_receipt_row
            ):
                row["watermark_event_id"] = positive_raw_outcome[
                    "watermark_event_id"
                ]
                row["capture_sequence"] = positive_raw_outcome[
                    "capture_sequence"
                ]
                row["eligible_until"] = stored_positive_eligible[
                    row["id"]
                ].isoformat()

            tampered_locator_envelope = deepcopy(positive_raw_envelope)
            tampered_locator_envelope["evidence_bindings"][0][
                "passage_locator"
            ]["evidence_start"] = 1
            with pytest.raises(
                psycopg.Error,
                match=(
                    "locator does not reproduce a meaningful passage|"
                    "binding replay diverges"
                ),
            ):
                with conn.transaction():
                    conn.execute(
                        psycopg_sql.SQL("SET LOCAL ROLE {}").format(
                            psycopg_sql.Identifier(execute_only_role)
                        )
                    )
                    conn.execute(
                        "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                        (Jsonb(tampered_locator_envelope),),
                    )

            group_id = canonical_fingerprint(
                "evidence-vault-c7-test-group-v1",
                {"capture_id": str(positive_capture_id)},
            )
            relation_specs = []
            for role, evidence_row_value, source_identity_id in (
                (
                    "owned_web", positive_evidence_rows[0],
                    owned_source_identity_id,
                ),
                (
                    "external_social_profile", positive_evidence_rows[1],
                    external_source_identity_id,
                ),
            ):
                evidence_fingerprint = evidence_row_value["content_hash"]
                evidence_identity = canonical_fingerprint(
                    "evidence-vault-c7-test-evidence-v1",
                    {"role": role, "fingerprint": evidence_fingerprint},
                )
                relation_id = canonical_fingerprint(
                    "evidence-vault-c7-test-relation-v1",
                    {"group_id": group_id, "role": role},
                )
                relation_specs.append({
                    "relation_id": relation_id,
                    "evidence_id": evidence_identity,
                    "source_identity_id": source_identity_id,
                    "evidence_fingerprint": evidence_fingerprint,
                    "channel_role": role,
                    "ref": evidence_row_value["evidence_ref"],
                    "literal_quote": evidence_row_value["content"],
                })
            exact_group = {
                "tile_id": "C7", "group_id": group_id,
                "decision_rule": "all_of", "relations": relation_specs,
            }
            artifact_unsigned = {
                "schema_version": "evidence-vault-exact-relation-supplement-v1",
                "authority": False, "runtime_effect": False,
                "cutover_authorized": False,
                "brand_identity": "example.com",
                "subject_url": "https://example.com",
                "selected_tile_ids": ["C7"], "groups": [exact_group],
                "relation_count": 2, "adoption_eligible": False,
            }
            exact_artifact_fingerprint = canonical_fingerprint(
                "evidence-vault-exact-relation-supplement-v1",
                artifact_unsigned,
            )
            exact_artifact = {
                **artifact_unsigned,
                "artifact_fingerprint": exact_artifact_fingerprint,
            }
            reference_resolution = {
                "schema_version":
                    "evidence-vault-exact-relation-source-resolution-v1",
                "source_kind": "exact_relation_supplement",
                "artifact_fingerprint": exact_artifact_fingerprint,
                "artifact": exact_artifact,
            }
            reference_resolution_fingerprint = canonical_fingerprint(
                "evidence-vault-operational-source-resolution-v1",
                reference_resolution,
            )
            packet_manifest = {
                "schema_version": "evidence-vault-candidate-packet-v1",
                "brand_identity": "example.com",
            }
            packet_candidate_tiles = [{} for _ in range(80)]
            packet_fingerprint = canonical_fingerprint(
                "evidence-vault-candidate-packet-fingerprint-v1",
                {
                    "manifest": packet_manifest,
                    "candidate_tiles": packet_candidate_tiles,
                },
            )
            packet_payload = {
                "manifest": packet_manifest,
                "candidate_tiles": packet_candidate_tiles,
                "candidate_packet_fingerprint": packet_fingerprint,
            }
            packet_id = uuid4()
            conn.execute("""
                INSERT INTO b3s_history.evidence_vault_canonical_memory_packets (
                    id, brand_id, packet_fingerprint, schema_version,
                    brand_identity, parent_canonical_memory_version,
                    reference_resolution_fingerprint, reference_resolution,
                    manifest, candidate_tiles, packet_kind, packet_payload
                ) VALUES (
                    %s, %s, %s, 'evidence-vault-candidate-packet-v1',
                    'example.com', NULL, %s, %s, %s, %s,
                    'operational_source_v2', %s
                )
            """, (
                packet_id, brand_id, packet_fingerprint,
                reference_resolution_fingerprint, Jsonb(reference_resolution),
                Jsonb(packet_manifest), Jsonb(packet_candidate_tiles),
                Jsonb(packet_payload),
            ))
            positive_receipt_set = receipt_set_fingerprint(
                positive_signed_dumps
            )
            raw_by_role = {
                "owned_web": positive_owned_raw_binding,
                "external_social_profile": positive_external_raw_binding,
            }
            receipt_by_role = {
                "owned_web": positive_owned_receipt_row,
                "external_social_profile": positive_external_receipt_row,
            }
            evidence_by_role = {
                "owned_web": positive_evidence_rows[0],
                "external_social_profile": positive_evidence_rows[1],
            }
            member_rows = []
            member_set_payload = []
            for relation in relation_specs:
                role = relation["channel_role"]
                raw_row_value = raw_by_role[role]
                receipt_row_value = receipt_by_role[role]
                evidence_row_value = evidence_by_role[role]
                member_payload = {
                    "group_id": group_id,
                    "relation_id": relation["relation_id"],
                    "evidence_id": relation["evidence_id"],
                    "source_identity_id": relation["source_identity_id"],
                    "evidence_fingerprint": relation["evidence_fingerprint"],
                    "channel_role": role,
                    "evidence_ref": relation["ref"],
                    "source_ref": relation["ref"],
                    "evidence_quote": relation["literal_quote"],
                    "receipt_fingerprint":
                        receipt_row_value["receipt_fingerprint"],
                    "raw_evidence_binding_fingerprint":
                        raw_row_value["binding_fingerprint"],
                }
                member_set_payload.append({
                    "channel_role": role,
                    "relation_id": relation["relation_id"],
                    "evidence_id": relation["evidence_id"],
                    "source_identity_id": relation["source_identity_id"],
                    "evidence_fingerprint": relation["evidence_fingerprint"],
                    "evidence_ref": relation["ref"],
                    "source_ref": relation["ref"],
                    "evidence_quote": relation["literal_quote"],
                    "receipt_fingerprint":
                        receipt_row_value["receipt_fingerprint"],
                    "raw_evidence_binding_fingerprint":
                        raw_row_value["binding_fingerprint"],
                })
                member_rows.append({
                    "id": str(uuid4()),
                    "binding_id": str(positive_binding_id),
                    "workspace_id": str(workspace_id),
                    "brand_id": str(brand_id),
                    "scan_run_id": str(positive_scan_id),
                    "capture_id": str(positive_capture_id),
                    "operational_source_packet_id": str(packet_id),
                    "composite_group_id": group_id,
                    "raw_evidence_binding_id": raw_row_value["id"],
                    "receipt_id": receipt_row_value["id"],
                    "evidence_record_id": evidence_row_value["id"],
                    "channel_role": role,
                    "relation_id": relation["relation_id"],
                    "evidence_id": relation["evidence_id"],
                    "source_identity_id": relation["source_identity_id"],
                    "evidence_fingerprint": relation["evidence_fingerprint"],
                    "evidence_ref": relation["ref"],
                    "source_ref": relation["ref"],
                    "evidence_quote": relation["literal_quote"],
                    "member_fingerprint": canonical_fingerprint(
                        "evidence-vault-verified-c7-lineage-member-v1",
                        member_payload,
                    ),
                })
            member_set_payload.sort(key=lambda row: row["relation_id"])
            positive_member_set = canonical_fingerprint(
                "evidence-vault-verified-c7-lineage-member-set-v1",
                member_set_payload,
            )
            stored_positive_eligible_until = conn.execute("""
                SELECT min(eligible_until)
                FROM b3s_history.evidence_vault_raw_acquisition_receipts
                WHERE capture_id = %s
            """, (positive_capture_id,)).fetchone()[0]
            positive_eligible_until = stored_positive_eligible_until.isoformat()
            registry_fingerprint = public_key_registry_fingerprint(
                positive_registry
            )
            verified_binding_payload = {
                "schema_version":
                    "evidence-vault-verified-c7-lineage-binding-v1",
                "canonical_brand": "example.com",
                "source_packet_fingerprint": packet_fingerprint,
                "operation_plan_fingerprint":
                    positive_plan_payload["operation_plan_fingerprint"],
                "exact_artifact_fingerprint": exact_artifact_fingerprint,
                "watermark_event_fingerprint": positive_raw_outcome[
                    "watermark_event_fingerprint"
                ],
                "capture_id": str(positive_capture_id),
                "capture_sequence": positive_raw_outcome["capture_sequence"],
                "composite_group_id": group_id,
                "freshness_policy_version":
                    "evidence-vault-c7-live-freshness-policy-v1",
                "public_key_registry_fingerprint": registry_fingerprint,
                "provenance": "verified_raw_acquisition_receipt",
                "receipt_set_fingerprint": positive_receipt_set,
                "member_set_fingerprint": positive_member_set,
                "eligible_until": positive_eligible_until,
            }
            verified_binding_fingerprint = conn.execute(
                """SELECT b3s_history.evidence_vault_canonical_fingerprint(
                       'evidence-vault-verified-c7-lineage-binding-v1',
                       %s::jsonb || jsonb_build_object(
                           'eligible_until', %s::timestamptz
                       )
                   )""",
                (
                    Jsonb({
                        key: value for key, value in verified_binding_payload.items()
                        if key != "eligible_until"
                    }),
                    stored_positive_eligible_until,
                ),
            ).fetchone()[0]
            verified_binding_row = {
                "id": str(positive_binding_id),
                "workspace_id": str(workspace_id),
                "brand_id": str(brand_id),
                "scan_run_id": str(positive_scan_id),
                "capture_id": str(positive_capture_id),
                "watermark_event_id": positive_raw_outcome[
                    "watermark_event_id"
                ],
                "capture_sequence": positive_raw_outcome["capture_sequence"],
                "operational_source_packet_id": str(packet_id),
                "operation_plan_id": str(positive_plan_id),
                "composite_group_id": group_id,
                "canonical_brand": "example.com",
                "source_packet_fingerprint": packet_fingerprint,
                "operation_plan_fingerprint":
                    positive_plan_payload["operation_plan_fingerprint"],
                "exact_artifact_fingerprint": exact_artifact_fingerprint,
                "freshness_policy_version":
                    "evidence-vault-c7-live-freshness-policy-v1",
                "public_key_registry_fingerprint": registry_fingerprint,
                "provenance": "verified_raw_acquisition_receipt",
                "receipt_set_fingerprint": positive_receipt_set,
                "member_set_fingerprint": positive_member_set,
                "eligible_until": positive_eligible_until,
                "binding_schema_version":
                    "evidence-vault-verified-c7-lineage-binding-v1",
                "binding_fingerprint": verified_binding_fingerprint,
            }
            positive_bind_envelope = {
                "receipts": [
                    positive_owned_receipt_row,
                    positive_external_receipt_row,
                ],
                "evidence_bindings": [
                    positive_owned_raw_binding,
                    positive_external_raw_binding,
                ],
                "verified_binding": verified_binding_row,
                "members": member_rows,
            }
            conn.commit()
            governance_dsn = make_conninfo(
                dsn, user=governance_role,
                password="governance-test-password",
            )
            with psycopg.connect(governance_dsn) as governance_conn:
                bind_outcome = governance_conn.execute(
                    "SELECT b3s_history.bind_evidence_vault_verified_c7_lineage(%s::jsonb)",
                    (Jsonb(positive_bind_envelope),),
                ).fetchone()[0]
                governance_conn.commit()
                assert bind_outcome["verified_binding_id"] == str(positive_binding_id)
                replay_outcome = governance_conn.execute(
                    "SELECT b3s_history.bind_evidence_vault_verified_c7_lineage(%s::jsonb)",
                    (Jsonb(positive_bind_envelope),),
                ).fetchone()[0]
                governance_conn.commit()
                assert replay_outcome == bind_outcome
                invalid_member_sets = (
                    [],
                    [member_rows[0]],
                    [*member_rows, {**member_rows[0], "id": str(uuid4())}],
                    [member_rows[0], member_rows[0]],
                )
                for invalid_members in invalid_member_sets:
                    invalid_cardinality = deepcopy(positive_bind_envelope)
                    invalid_cardinality["members"] = invalid_members
                    with pytest.raises(psycopg.Error, match="ingest envelope is invalid"):
                        with governance_conn.transaction():
                            governance_conn.execute(
                                "SELECT b3s_history.bind_evidence_vault_verified_c7_lineage(%s::jsonb)",
                                (Jsonb(invalid_cardinality),),
                            )
                changed_member = deepcopy(positive_bind_envelope)
                changed_member["members"][0]["source_ref"] = "changed.ref"
                with pytest.raises(psycopg.Error, match="member replay diverges"):
                    with governance_conn.transaction():
                        governance_conn.execute(
                            "SELECT b3s_history.bind_evidence_vault_verified_c7_lineage(%s::jsonb)",
                            (Jsonb(changed_member),),
                        )
                changed_binding = deepcopy(positive_bind_envelope)
                changed_binding["verified_binding"][
                    "public_key_registry_fingerprint"
                ] = "f" * 64
                with pytest.raises(psycopg.Error, match="binding replay diverges"):
                    with governance_conn.transaction():
                        governance_conn.execute(
                            "SELECT b3s_history.bind_evidence_vault_verified_c7_lineage(%s::jsonb)",
                            (Jsonb(changed_binding),),
                        )
                assert not governance_conn.execute(
                    "SELECT has_table_privilege(current_user, "
                    "'b3s_history.evidence_vault_verified_c7_lineage_bindings', "
                    "'SELECT')"
                ).fetchone()[0]
                assert_provenance_role_is_execute_only(governance_conn)
            assert conn.execute(
                "SELECT count(*) FROM b3s_history.evidence_vault_verified_c7_lineage_members WHERE binding_id = %s",
                (positive_binding_id,),
            ).fetchone()[0] == 2
            with pytest.raises(psycopg.Error, match="foreign key constraint"):
                with conn.transaction():
                    conn.execute("""
                        INSERT INTO b3s_history.evidence_vault_verified_c7_lineage_members (
                            id, binding_id, workspace_id, brand_id, scan_run_id,
                            capture_id, operational_source_packet_id,
                            composite_group_id, raw_evidence_binding_id, receipt_id,
                            evidence_record_id, channel_role, relation_id, evidence_id,
                            source_identity_id, evidence_fingerprint, evidence_ref,
                            source_ref, evidence_quote, member_fingerprint
                        )
                        SELECT %s, %s, %s, brand_id, scan_run_id, capture_id,
                            operational_source_packet_id, composite_group_id,
                            raw_evidence_binding_id, receipt_id, evidence_record_id,
                            channel_role, relation_id, evidence_id,
                            source_identity_id, evidence_fingerprint, evidence_ref,
                            source_ref, evidence_quote, %s
                        FROM b3s_history.evidence_vault_verified_c7_lineage_members
                        WHERE binding_id = %s LIMIT 1
                    """, (
                        uuid4(), uuid4(), uuid4(), "e" * 64,
                        positive_binding_id,
                    ))
            with pytest.raises(
                psycopg.Error,
                match="identity/current-watermark contract is invalid",
            ):
                with conn.transaction():
                    conn.execute("SET LOCAL session_replication_role = replica")
                    conn.execute("""
                        UPDATE b3s_history.evidence_vault_canonical_memory_packets
                        SET reference_resolution = jsonb_set(
                            reference_resolution,
                            '{artifact,groups}',
                            (reference_resolution #> '{artifact,groups}') ||
                            (reference_resolution #> '{artifact,groups}')
                        )
                        WHERE id = %s
                    """, (packet_id,))
                    conn.execute("SET LOCAL session_replication_role = origin")
                    conn.execute(
                        "SELECT b3s_history.validate_evidence_vault_verified_c7_binding(%s)",
                        (positive_binding_id,),
                    )

            def make_concurrent_owned_envelope(label: str) -> dict:
                concurrent_scan_id = uuid4()
                concurrent_capture_id = uuid4()
                concurrent_evidence_id = uuid4()
                concurrent_receipt_id = uuid4()
                concurrent_raw_binding_id = uuid4()
                concurrent_plan_id = uuid4()
                concurrent_source_scan = f"scan-concurrent-{label}"
                concurrent_session_id = str(uuid4())
                concurrent_now = datetime.now(timezone.utc).replace(microsecond=0)
                concurrent_now_text = concurrent_now.isoformat().replace("+00:00", "Z")
                concurrent_pre_hash = pre_receipt_snapshot_sha256({
                    "schema_version": "evidence-vault-pre-receipt-snapshot-v1",
                    "workspace_slug": "b3s",
                    "source_scan_id": concurrent_source_scan,
                    "acquisition_session_id": concurrent_session_id,
                    "canonical_brand_domain": "example.com",
                    "canonical_brand_url": "https://example.com",
                    "raw_payload": raw_without_provenance,
                })
                concurrent_signed = _signed_owned_receipt(
                    fetched_at=concurrent_now_text,
                    source_scan_id=concurrent_source_scan,
                    acquisition_session_id=concurrent_session_id,
                    raw_fragment_sha256=raw_hash,
                    extracted_document_sha256=document_hash,
                    pre_receipt_snapshot_sha256=concurrent_pre_hash,
                ).model_dump(mode="json")
                concurrent_claims = concurrent_signed["claims"]
                concurrent_raw_payload = {
                    **raw_without_provenance,
                    "evidence_vault_raw_provenance": {
                        "schema_version": "evidence-vault-raw-provenance-envelope-v1",
                        "workspace_slug": "b3s",
                        "source_scan_id": concurrent_source_scan,
                        "acquisition_session_id": concurrent_session_id,
                        "canonical_brand_domain": "example.com",
                        "canonical_brand_url": "https://example.com",
                        "pre_receipt_snapshot_sha256": concurrent_pre_hash,
                        "signed_receipts": [concurrent_signed],
                        "receipt_set_fingerprint": receipt_set_fingerprint(
                            [concurrent_signed]
                        ),
                        "external_identity_provenance": None,
                    },
                }
                concurrent_capture_hash = hashlib.sha256(
                    canonical_json(concurrent_raw_payload).encode()
                ).hexdigest()
                concurrent_receipt = {
                    **receipt_row,
                    "id": str(concurrent_receipt_id),
                    "scan_run_id": str(concurrent_scan_id),
                    "source_scan_id": concurrent_source_scan,
                    "capture_id": str(concurrent_capture_id),
                    "watermark_event_id": None,
                    "capture_sequence": None,
                    "receipt_schema_version": concurrent_claims["schema_version"],
                    "freshness_policy_version":
                        concurrent_claims["freshness_policy_version"],
                    "signature_schema_version": concurrent_signed["signature_schema"],
                    "key_id": concurrent_claims["key_id"],
                    "receipt_nonce": concurrent_claims["receipt_nonce"],
                    "acquisition_session_id":
                        concurrent_claims["acquisition_session_id"],
                    "pre_receipt_snapshot_sha256": concurrent_pre_hash,
                    "fetched_at": concurrent_claims["fetched_at"],
                    "eligible_until": (
                        concurrent_now + timedelta(hours=24)
                    ).isoformat(),
                    "claims": concurrent_claims,
                    "receipt_fingerprint": concurrent_signed["receipt_fingerprint"],
                    "signed_payload": {
                        key: concurrent_signed[key]
                        for key in (
                            "signature_schema", "claims", "receipt_fingerprint"
                        )
                    },
                    "signature": concurrent_signed["signature"],
                }
                concurrent_binding = {
                    **binding_row,
                    "id": str(concurrent_raw_binding_id),
                    "scan_run_id": str(concurrent_scan_id),
                    "capture_id": str(concurrent_capture_id),
                    "receipt_id": str(concurrent_receipt_id),
                    "receipt_fingerprint": concurrent_signed["receipt_fingerprint"],
                    "evidence_record_id": str(concurrent_evidence_id),
                }
                concurrent_binding["binding_fingerprint"] = canonical_fingerprint(
                    "evidence-vault-raw-evidence-binding-v1",
                    {
                        key: concurrent_binding[key]
                        for key in (
                            "receipt_fingerprint", "evidence_record_id",
                            "evidence_ref", "source_url", "channel_role",
                            "extractor_schema_version", "extractor_version",
                            "extracted_document_sha256", "passage_locator",
                            "passage_sha256", "evidence_record_content_hash",
                        )
                    },
                )
                concurrent_observation = {
                    **raw_observation,
                    "source_scan_id": concurrent_source_scan,
                    "observed_at": concurrent_now_text,
                    "recorded_at": concurrent_now_text,
                    "capture_payload": concurrent_raw_payload,
                }
                concurrent_observation_hash = hashlib.sha256(
                    canonical_json(concurrent_observation).encode()
                ).hexdigest()
                concurrent_plan_payload = build_vault_scan_plan(
                    brand_identity="example.com",
                    subject_url="https://example.com",
                    mode="baseline",
                    current_evidence_records=[{
                        "ref": "owned.ref", "source": "web",
                        "evidence_type": "text", "url": "https://example.com",
                        "content": document,
                        "metadata": {"source_class": "owned_copy"},
                    }],
                )
                return {
                    "workspace": raw_envelope["workspace"],
                    "brand": {
                        **raw_envelope["brand"],
                        "latest_observed_at": concurrent_now_text,
                    },
                    "scan_run": {
                        "id": str(concurrent_scan_id),
                        "workspace_id": str(workspace_id),
                        "brand_id": str(brand_id),
                        "source_scan_id": concurrent_source_scan,
                        "status": "completed", "pipeline_version": "test-v1",
                        "acquisition_state": "completed",
                        "requested_at": concurrent_now_text,
                        "recorded_at": concurrent_now_text,
                        "request_payload": concurrent_observation,
                        "metadata": {
                            "observation_hash": concurrent_observation_hash
                        },
                    },
                    "operation_plan": {
                        "id": str(concurrent_plan_id),
                        "workspace_id": str(workspace_id),
                        "brand_id": str(brand_id),
                        "scan_run_id": str(concurrent_scan_id),
                        "observation_hash": concurrent_observation_hash,
                        "operation_plan_fingerprint": concurrent_plan_payload[
                            "operation_plan_fingerprint"
                        ],
                        "canonical_memory_version": None,
                        "mode": "baseline", "status": "pending",
                        "plan_payload": concurrent_plan_payload,
                    },
                    "capture": {
                        "id": str(concurrent_capture_id),
                        "scan_run_id": str(concurrent_scan_id),
                        "brand_id": str(brand_id),
                        "observed_at": concurrent_now_text,
                        "recorded_at": concurrent_now_text,
                        "source_url": "https://example.com",
                        "content_hash": concurrent_capture_hash,
                        "raw_payload": concurrent_raw_payload,
                    },
                    "evidence_records": [{
                        "id": str(concurrent_evidence_id),
                        "capture_id": str(concurrent_capture_id),
                        "evidence_ref": "owned.ref", "source": "web",
                        "source_class": "owned_copy", "evidence_type": "text",
                        "url": "https://example.com", "content": document,
                        "content_hash": evidence_hash,
                        "metadata": {"source_class": "owned_copy"},
                    }],
                    "watermark_event": {
                        "capture_content_hash": concurrent_capture_hash,
                        "capture_observation_hash": concurrent_observation_hash,
                        "append_origin": "capture_observation_commit",
                    },
                    "receipts": [concurrent_receipt],
                    "evidence_bindings": [concurrent_binding],
                }

            concurrent_envelopes = [
                make_concurrent_owned_envelope("a"),
                make_concurrent_owned_envelope("b"),
            ]

            def append_concurrently(concurrent_envelope: dict) -> dict:
                with psycopg.connect(execute_only_dsn) as concurrent_conn:
                    return concurrent_conn.execute(
                        "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                        (Jsonb(concurrent_envelope),),
                    ).fetchone()[0]

            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as executor:
                concurrent_outcomes = list(
                    executor.map(append_concurrently, concurrent_envelopes)
                )
            concurrent_sequences = sorted(
                outcome["capture_sequence"] for outcome in concurrent_outcomes
            )
            assert concurrent_sequences == [
                positive_raw_outcome["capture_sequence"] + 1,
                positive_raw_outcome["capture_sequence"] + 2,
            ]
            assert len({
                outcome["watermark_event_id"] for outcome in concurrent_outcomes
            }) == 2
            with pytest.raises(
                psycopg.Error,
                match="identity/current-watermark contract is invalid",
            ):
                with conn.transaction():
                    conn.execute(
                        "SELECT b3s_history.validate_evidence_vault_verified_c7_binding(%s)",
                        (positive_binding_id,),
                    )

            assert conn.execute("""
                SELECT action, prior_state, resulting_state, event_sequence
                FROM b3s_history.evidence_vault_raw_provenance_disposition_events
                WHERE binding_id = %s
            """, (positive_binding_id,)).fetchone() == (
                "retain", "none", "retained", 1,
            )

            incomplete_group = {
                "receipts": [receipt_row],
                "evidence_bindings": [binding_row],
                "verified_binding": {},
                "members": [],
            }
            with pytest.raises(psycopg.Error, match="ingest envelope is invalid"):
                with conn.transaction():
                    conn.execute(
                        "SELECT b3s_history.bind_evidence_vault_verified_c7_lineage(%s::jsonb)",
                        (Jsonb(incomplete_group),),
                    )
            replay_only_group = deepcopy(positive_bind_envelope)
            for replay_table, replay_column, replay_value, replay_error in (
                (
                    "evidence_vault_raw_acquisition_receipts", "provider",
                    "firecrawl", "raw receipt replay diverges",
                ),
                (
                    "evidence_vault_raw_evidence_bindings", "source_url",
                    "https://example.com/other", "raw evidence binding replay diverges",
                ),
            ):
                with pytest.raises(psycopg.Error, match=replay_error):
                    with conn.transaction():
                        conn.execute("SET LOCAL session_replication_role = replica")
                        conn.execute(
                            psycopg_sql.SQL(
                                "UPDATE b3s_history.{} SET {} = %s WHERE id = %s"
                            ).format(
                                psycopg_sql.Identifier(replay_table),
                                psycopg_sql.Identifier(replay_column),
                            ),
                            (
                                replay_value,
                                owned_receipt_id if "receipts" in replay_table
                                else owned_raw_binding_id,
                            ),
                        )
                        conn.execute("SET LOCAL session_replication_role = origin")
                        conn.execute(
                            "SELECT b3s_history.bind_evidence_vault_verified_c7_lineage(%s::jsonb)",
                            (Jsonb(replay_only_group),),
                        )
            crossed = {
                **raw_envelope,
                "capture": {**raw_envelope["capture"], "brand_id": str(uuid4())},
            }
            with pytest.raises(psycopg.Error, match="identity is inconsistent"):
                with conn.transaction():
                    conn.execute(
                        "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                        (Jsonb(crossed),),
                    )
            from copy import deepcopy
            divergent_observation = deepcopy(raw_envelope)
            divergent_observation["scan_run"]["request_payload"][
                "capture_payload"
            ] = {"different": True}
            divergent_hash = hashlib.sha256(canonical_json(
                divergent_observation["scan_run"]["request_payload"]
            ).encode()).hexdigest()
            divergent_observation["scan_run"]["metadata"][
                "observation_hash"
            ] = divergent_hash
            divergent_observation["watermark_event"][
                "capture_observation_hash"
            ] = divergent_hash
            with pytest.raises(psycopg.Error, match="capture/observation/operation-plan content"):
                with conn.transaction():
                    conn.execute(
                        "SELECT b3s_history.append_evidence_vault_raw_acquisition(%s::jsonb)",
                        (Jsonb(divergent_observation),),
                    )
            assert conn.execute(
                "SELECT count(*) FROM b3s_history.captures WHERE id = %s",
                (capture_id,),
            ).fetchone()[0] == 1
            with pytest.raises(psycopg.Error, match="append-only"):
                with conn.transaction():
                    conn.execute("""
                        UPDATE b3s_history.evidence_vault_raw_acquisition_receipts
                        SET signature = signature WHERE id = %s
                    """, (receipt_id,))
            with pytest.raises(psycopg.Error, match="append-only"):
                with conn.transaction():
                    conn.execute(
                        "DELETE FROM b3s_history.evidence_vault_raw_evidence_bindings WHERE id = %s",
                        (raw_binding_id,),
                    )

            # Isolate the disposition state machine from packet/group setup.
            # Replica mode is fixture-only and unavailable to runtime roles.
            disposition_binding_id = uuid4()
            disposition_binding_fp = "d" * 64
            receipt_set_fp = raw_payload["evidence_vault_raw_provenance"][
                "receipt_set_fingerprint"
            ]
            composite_group_id = "e" * 64
            conn.execute("SET session_replication_role = replica")
            conn.execute("""
                INSERT INTO b3s_history.evidence_vault_verified_c7_lineage_bindings (
                    id, workspace_id, brand_id, scan_run_id, capture_id,
                    watermark_event_id, capture_sequence,
                    operational_source_packet_id, operation_plan_id,
                    composite_group_id, canonical_brand, source_packet_fingerprint,
                    operation_plan_fingerprint, exact_artifact_fingerprint,
                    freshness_policy_version, public_key_registry_fingerprint,
                    provenance, receipt_set_fingerprint, member_set_fingerprint,
                    eligible_until, binding_fingerprint
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, 1, %s, %s, %s,
                    'example.com', %s, %s, %s,
                    'evidence-vault-c7-live-freshness-policy-v1', %s,
                    'verified_raw_acquisition_receipt', %s, %s, %s, %s
                )
            """, (
                disposition_binding_id, workspace_id, brand_id, scan_id, capture_id,
                watermark_id, uuid4(), uuid4(), composite_group_id,
                "8" * 64, "9" * 64, "0" * 64, "a" * 64,
                receipt_set_fp, "1" * 64, now + timedelta(hours=24),
                disposition_binding_fp,
            ))
            conn.execute("SET session_replication_role = origin")

            previous_id = None
            previous_fingerprint = None
            resulting_state = "none"
            for sequence, action, expected_state in (
                (1, "retain", "retained"),
                (2, "place_legal_hold", "legal_hold"),
                (3, "release_legal_hold", "retained"),
                (4, "revoke_runtime", "runtime_revoked"),
            ):
                event_id = uuid4()
                idem = hashlib.sha256(f"disposition-{sequence}".encode()).hexdigest()
                reason = f"test transition {sequence}"
                actor = "provenance-operator-test"
                event_fp = canonical_fingerprint(
                    "evidence-vault-raw-provenance-disposition-event-v1",
                    {
                        "schema_version": "evidence-vault-raw-provenance-disposition-event-v1",
                        "binding_fingerprint": disposition_binding_fp,
                        "receipt_set_fingerprint": receipt_set_fp,
                        "event_sequence": sequence,
                        "previous_event_fingerprint": previous_fingerprint,
                        "action": action,
                        "prior_state": resulting_state,
                        "resulting_state": expected_state,
                        "reason": reason,
                        "actor_id": actor,
                        "idempotency_key_hash": idem,
                    },
                )
                event_payload = {
                    "id": str(event_id), "binding_id": str(disposition_binding_id),
                    "workspace_id": str(workspace_id), "brand_id": str(brand_id),
                    "scan_run_id": str(scan_id), "capture_id": str(capture_id),
                    "operational_source_packet_id": str(uuid4()),
                    "composite_group_id": composite_group_id,
                    "receipt_set_fingerprint": receipt_set_fp,
                    "event_sequence": sequence,
                    "previous_event_id": str(previous_id) if previous_id else None,
                    "previous_event_fingerprint": previous_fingerprint,
                    "action": action, "prior_state": resulting_state,
                    "resulting_state": expected_state, "reason": reason,
                    "actor_id": actor, "idempotency_key_hash": idem,
                    "event_schema_version": "evidence-vault-raw-provenance-disposition-event-v1",
                    "event_fingerprint": event_fp,
                }
                # The packet identity must equal the target composite FK.
                event_payload["operational_source_packet_id"] = str(
                    conn.execute("""
                        SELECT operational_source_packet_id
                        FROM b3s_history.evidence_vault_verified_c7_lineage_bindings
                        WHERE id = %s
                    """, (disposition_binding_id,)).fetchone()[0]
                )
                stored_fp = conn.execute(
                    """SELECT (b3s_history.append_evidence_vault_raw_provenance_disposition(
                           %s::jsonb)).event_fingerprint""",
                    (Jsonb(event_payload),),
                ).fetchone()[0]
                assert stored_fp == event_fp
                conn.execute(
                    "SELECT b3s_history.append_evidence_vault_raw_provenance_disposition(%s::jsonb)",
                    (Jsonb(event_payload),),
                )
                if sequence == 1:
                    divergent_event = {**event_payload, "reason": "hidden divergence"}
                    with pytest.raises(psycopg.Error, match="divergent content"):
                        with conn.transaction():
                            conn.execute(
                                "SELECT b3s_history.append_evidence_vault_raw_provenance_disposition(%s::jsonb)",
                                (Jsonb(divergent_event),),
                            )
                previous_id, previous_fingerprint = event_id, event_fp
                resulting_state = expected_state

            terminal_payload = {
                **event_payload,
                "id": str(uuid4()), "event_sequence": 5,
                "previous_event_id": str(previous_id),
                "previous_event_fingerprint": previous_fingerprint,
                "action": "place_legal_hold", "prior_state": "runtime_revoked",
                "resulting_state": "legal_hold",
                "idempotency_key_hash": hashlib.sha256(b"terminal").hexdigest(),
                "event_fingerprint": "f" * 64,
            }
            with pytest.raises(psycopg.Error, match="invalid or terminal"):
                with conn.transaction():
                    conn.execute(
                        "SELECT b3s_history.append_evidence_vault_raw_provenance_disposition(%s::jsonb)",
                        (Jsonb(terminal_payload),),
                    )

            tables = (
                "evidence_vault_raw_acquisition_receipts",
                "evidence_vault_raw_evidence_bindings",
                "evidence_vault_verified_c7_lineage_bindings",
                "evidence_vault_verified_c7_lineage_members",
                "evidence_vault_raw_provenance_disposition_events",
            )
            truncate_triggers = conn.execute("""
                SELECT count(*)
                FROM pg_trigger AS triggers
                JOIN pg_class AS tables ON tables.oid = triggers.tgrelid
                JOIN pg_namespace AS schemas ON schemas.oid = tables.relnamespace
                WHERE schemas.nspname = 'b3s_history'
                  AND tables.relname = ANY(%s)
                  AND triggers.tgname LIKE '%%no_truncate'
            """, (list(tables),)).fetchone()[0]
            assert truncate_triggers == 5
            with pytest.raises(psycopg.Error, match="cannot be truncated"):
                with conn.transaction():
                    conn.execute(
                        "TRUNCATE " + ", ".join(
                            f"b3s_history.{table}" for table in tables
                        )
                    )
            rows = conn.execute("""
                SELECT p.proname, has_function_privilege('public', p.oid, 'EXECUTE') AS public_execute
                FROM pg_proc AS p
                JOIN pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = 'b3s_history'
                  AND p.proname IN (
                    'append_evidence_vault_raw_acquisition',
                    'bind_evidence_vault_verified_c7_lineage',
                    'append_evidence_vault_raw_provenance_disposition',
                    'read_evidence_vault_raw_provenance',
                    'read_evidence_vault_raw_acquisition'
                  )
            """).fetchall()
            assert len(rows) == 5
            assert all(not row[1] for row in rows)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")
            for role in (execute_only_role, governance_role, runtime_read, writer):
                conn.execute(
                    psycopg_sql.SQL("DROP ROLE IF EXISTS {}").format(
                        psycopg_sql.Identifier(role)
                    )
                )
