"""Static contract for migration 025's storage-only SV9 shadow ledger."""

from __future__ import annotations

from pathlib import Path


_MIGRATION = Path(
    "src/history/migrations/025_evidence_vault_operational_sv9_shadow_assessments.sql"
)


def _sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def test_sv9_shadow_ledger_is_the_forward_only_migration_head() -> None:
    from src.history.repository import _migration_files

    filenames = [filename for filename, _sql_text in _migration_files()]

    assert filenames[-1] == _MIGRATION.name
    assert filenames.count(_MIGRATION.name) == 1


def test_sv9_shadow_ledger_binds_exact_operational_source_packets() -> None:
    sql = _sql()

    assert (
        "FOREIGN KEY (\n        brand_id, operational_packet_id, "
        "operational_packet_fingerprint," in sql
    )
    assert (
        "FOREIGN KEY (\n        brand_id, source_packet_id, "
        "source_candidate_packet_fingerprint," in sql
    )
    assert (
        "REFERENCES b3s_history.evidence_vault_canonical_memory_packets (\n"
        "        brand_id, id, packet_fingerprint, packet_kind" in sql
    )
    assert "operational_source_v2', 'operational_reviewed_v2" in sql
    assert "canonical_v1` is never a valid source" in sql
    assert "source_payload ->> 'candidate_packet_fingerprint'" in sql
    assert "operational_payload ->> 'source_candidate_packet_fingerprint'" in sql
    assert "operational_payload ->> 'candidate_overlay_version'" in sql
    assert "expected_parent_canonical_memory_version" in sql


def test_sv9_shadow_ledger_is_strict_conservative_and_append_only() -> None:
    sql = _sql()

    assert "jsonb_array_length(tiles) = 80" in sql
    assert "jsonb_array_length(requirements -> 'tiles') = 80" in sql
    assert "'ok', 'no', 'sin_evidencia', 'contradiction'" in sql
    assert "'pending', 'disputed', 'stale', 'unverifiable'" in sql
    assert "'verified'" not in sql
    assert "'available'," in sql
    assert "'stale_candidate_parent'," in sql
    assert "'contradiction_requires_semantic_reassessment'" in sql
    assert "BEFORE INSERT ON b3s_history.evidence_vault_operational_sv9_shadow_assessments" in sql
    assert "BEFORE UPDATE OR DELETE ON b3s_history.evidence_vault_operational_sv9_shadow_assessments" in sql
    assert "BEFORE TRUNCATE ON b3s_history.evidence_vault_operational_sv9_shadow_assessments" in sql
    assert "SECURITY DEFINER" not in sql
    assert (
        "REVOKE ALL ON b3s_history.evidence_vault_operational_sv9_shadow_assessments\n"
        "FROM PUBLIC" in sql
    )
    assert "b3s_pr71_scanner_ingest" in sql
    assert "has_table_privilege" in sql
    assert "has_any_column_privilege" in sql


def test_adr_limits_persistence_to_the_non_authoritative_ledger() -> None:
    adr = Path("docs/evidence_vault_sv9_assessment_adr_v3.md").read_text(
        encoding="utf-8"
    )

    assert "migración 025 solo define un ledger append-only no autoritativo" in adr
    assert "no añade writer, read path ni exposición" in adr
