from pathlib import Path


MIGRATION = Path(
    "src/history/migrations/028_evidence_vault_operational_sv9_shadow_diagnostics.sql"
)


def test_migration_exposes_only_a_sanitized_owner_view() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    lowered = sql.lower()

    assert "create view b3s_history.evidence_vault_operational_sv9_shadow_diagnostics_v1" in lowered
    assert "with (security_barrier = true, security_invoker = false)" in lowered
    assert "set local role b3s_history_vault_provenance_owner" in lowered
    assert "grant usage, create on schema b3s_history" in lowered
    assert "revoke create on schema b3s_history" in lowered
    assert "sv9 shadow diagnostic owner retains schema create" in lowered
    assert "create function" not in lowered
    assert "security definer" not in lowered
    assert "grant select on b3s_history.evidence_vault_operational_sv9_shadow_assessments" not in lowered
    assert "candidate_semantic_tiles" not in lowered
    assert "source_packet_id" not in lowered
    assert "operational_packet_id" not in lowered
    assert "reviewer" not in lowered


def test_migration_grants_no_consumer_and_revokes_all_known_broad_principals() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    lowered = sql.lower()

    assert "grant select" not in lowered
    assert "from public" in lowered
    assert "b3s_history_vault_sv9_shadow_writer" in lowered
    assert "b3s_history_vault_runtime_read" in lowered
    assert "b3s_pr71_scanner_ingest" in lowered
    assert "b3s_pr71_app_runtime" in lowered
    assert "pr71 app runtime inherits access to the raw sv9 shadow ledger" in lowered
    assert "has_table_privilege" in lowered
    assert "public can access the sv9 shadow diagnostic view" in lowered


def test_view_has_an_exact_scalar_column_contract() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for column in (
        "evaluation_identity",
        "operational_packet_fingerprint",
        "assessment_status",
        "sv9_score",
        "base_average",
        "magnetism_capped",
        "assessment_fingerprint",
        "score_fingerprint",
        "semantic_provenance_fingerprint",
        "verification_pending_count",
        "verification_verified_count",
        "verification_disputed_count",
        "verification_stale_count",
        "verification_unverifiable_count",
        "authority",
        "production_runtime_effect",
        "scanner_runtime_effect",
        "created_at",
    ):
        assert f"'{column}'" in sql
