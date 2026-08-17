from pathlib import Path


MIGRATION = Path(
    "src/history/migrations/029_evidence_vault_semantic_analysis_claims.sql"
)


def test_semantic_analysis_claims_migration_is_forward_only_and_bounded() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "read_evidence_vault_raw_planning_context(" in sql
    assert "p_semantic_analysis_contract_fingerprint text" in sql
    assert "evidence-vault-raw-planning-context-v2" in sql
    assert "semantic_analysis_claimed_fingerprints" in sql
    assert "classify_evidence_fingerprints" in sql
    assert "plans.status <> 'superseded'" in sql
    assert "LIMIT 500" in sql
    assert "LIMIT 5001" in sql
    assert "semantic_claim_count > 5000" in sql
    assert "existing_operation_plan" in sql
    assert "semantic_analysis_contract_fingerprint" in sql
    assert "REVOKE EXECUTE" in sql
    assert "TO b3s_pr71_scanner_ingest" in sql


def test_semantic_analysis_claims_migration_has_no_historical_dml() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").upper()

    assert "INSERT INTO" not in sql
    assert "UPDATE " not in sql
    assert "DELETE FROM" not in sql
    assert "TRUNCATE " not in sql
