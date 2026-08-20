from pathlib import Path


_MIGRATION = Path(
    "src/history/migrations/030_evidence_vault_receipt_evidence_cardinality.sql"
)


def test_receipt_evidence_cardinality_migration_is_forward_only_and_fail_closed() -> None:
    sql = _MIGRATION.read_text(encoding="utf-8")

    assert "DROP CONSTRAINT evidence_vault_raw_evidence_bindings_receipt_id_key" in sql
    assert "UNIQUE (receipt_id, evidence_record_id)" in sql
    assert (
        "RENAME TO append_evidence_vault_raw_acquisition_one_to_one_v1" in sql
    )
    assert "SECURITY INVOKER" in sql
    assert "CREATE FUNCTION b3s_history.append_evidence_vault_raw_acquisition(payload jsonb)" in sql
    assert (
        "jsonb_array_length(payload -> 'evidence_bindings')\n"
        "            IS DISTINCT FROM jsonb_array_length(payload -> 'evidence_records')"
        in sql
    )
    assert "coverage.binding_count IS DISTINCT FROM 1" in sql
    assert "WHERE binding.receipt_id = receipt.id" in sql
    assert "raw acquisition binding provenance is invalid" in sql
    assert "raw acquisition binding coverage is invalid" in sql
    assert "ON CONFLICT (binding_fingerprint) DO NOTHING" in sql
    assert (
        "truncated 1-per-receipt subset"
        in sql
    )
    assert "raw evidence binding replay diverges from immutable stored content" in sql
    assert "DROP TABLE" not in sql
    assert "DELETE FROM" not in sql
    assert "TRUNCATE" not in sql


def test_receipt_evidence_cardinality_migration_hides_the_legacy_definer() -> None:
    sql = _MIGRATION.read_text(encoding="utf-8")

    assert (
        "REVOKE ALL ON FUNCTION\n"
        "    b3s_history.append_evidence_vault_raw_acquisition_one_to_one_v1(jsonb)\n"
        "FROM PUBLIC"
        in sql
    )
    assert "REVOKE ALL PRIVILEGES ON FUNCTION" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql
    assert "RESET ROLE" in sql
    assert (
        "REVOKE CREATE ON SCHEMA b3s_history\n"
        "FROM b3s_history_vault_provenance_owner"
        in sql
    )
