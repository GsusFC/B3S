from __future__ import annotations

from copy import deepcopy
import hashlib
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from src.history.capture_observation import parse_capture_observation
from src.services.evidence_memory_identity_v2 import project_evidence_memory_row_identity
from src.services.evidence_vault_candidate_resolver import canonical_aggregation_policy_fingerprint
from src.services.evidence_vault_canonical_core import (
    build_tile_contract_registry,
    canonical_fingerprint,
    canonical_json,
    reducer_policy_fingerprint,
    tile_contract_registry_fingerprint,
)
from src.services.evidence_vault_exact_relation_supplement import (
    EVIDENCE_VAULT_EXACT_RELATION_SUPPLEMENT_VERSION,
    EvidenceVaultExactRelationSupplementError,
    validate_exact_relation_supplement_structure,
)
from src.services.evidence_vault_lineage_replay import (
    EVIDENCE_VAULT_LINEAGE_SEED_EXPORT_VERSION,
    EvidenceVaultLineageReplayError,
    build_historical_report_capture_observation,
    build_lineage_seed_export_v2,
    derive_normalized_evidence_pack,
    validate_lineage_seed_export_v2,
)
from src.services.scanner_evidence_comparison import canonical_evidence_representatives


RAW_SHA = "f" * 64
SUBJECT = "https://causaprima.ai"


def _report() -> dict:
    return {
        "id": "historical-c7-1",
        "url": SUBJECT,
        "brand_name": "Causa Prima",
        "created_at": "2026-07-08T09:17:20.190398Z",
        "recorded_at": "2026-07-08T09:18:20.190398Z",
        "components": [{"key": "coherencia", "score": 1}],
        "limitations": ["historical_report_only"],
        "attempts": [
            {
                "provider": "historical",
                "status": "recorded",
                "details": {"max_tokens": 100},
            }
        ],
        "acquisition_artifacts": [
            {"name": "historical-screenshot.png", "sha256": "1" * 64}
        ],
        "acquisition_gate": {"state": "sufficient"},
        "raw": {
            "source_run_id": "historical-run-1",
            "flow": {
                "candidate": {
                    "brand_name": "Causa Prima",
                    "url": SUBJECT,
                    "evidence_pack": {
                        "schema_version": "test-pack-v1",
                        "evidence": [
                            {
                                "ref": "owned.1",
                                "source": "website",
                                "evidence_type": "copy",
                                "url": SUBJECT,
                                "content": "The agent-to-agent network for finance teams.",
                                "metadata": {
                                    "source_class": "owned_copy",
                                    "identity_match_llm": "exact",
                                    "relevant_blocks": ["coherencia"],
                                    "specificity": "high",
                                },
                            },
                            {
                                "ref": "linkedin.1",
                                "source": "linkedin",
                                "evidence_type": "company_profile",
                                "url": "https://www.linkedin.com/company/causa-prima",
                                "content": (
                                    "Causa Prima puts buyers and suppliers on the same "
                                    "network for finance teams."
                                ),
                                "metadata": {
                                    "source_class": "external_proof",
                                    "identity_match_llm": "brand_name",
                                    "semantic_labeling_version": "old",
                                    "stance": "support",
                                },
                            },
                        ],
                    },
                }
            },
        },
    }


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _channel_role(row: dict) -> str:
    return (
        "owned_web"
        if row["metadata"]["source_class"] == "owned_copy"
        else "external_social_profile"
    )


def _exact_c7_source(pack: dict) -> dict:
    brand = "causaprima.ai"
    candidate_id = "2" * 64
    parent = "3" * 64
    audit = "4" * 64
    worksheet = "5" * 64
    reviewer = "reviewer@example.test"
    reviewed_at = "2026-08-02T10:00:00+00:00"
    pack_sha = _sha(pack)
    request_payload = {
        "brand_identity": brand,
        "subject_url": SUBJECT,
        "parent_canonical_memory_version": parent,
        "source_evidence_pack_canonical_sha256": pack_sha,
        "source_assessment_audit_fingerprint": audit,
        "source_assessment_worksheet_sha256": worksheet,
        "source_assessment_reviewer_id": reviewer,
        "source_assessment_reviewed_at": reviewed_at,
        "tile_contract_registry_fingerprint": tile_contract_registry_fingerprint(),
        "reducer_policy_fingerprint": reducer_policy_fingerprint(),
        "aggregation_policy_fingerprint": canonical_aggregation_policy_fingerprint(),
        "selected_assessment_candidate_ids": [candidate_id],
        "selected_tile_ids": ["C7"],
    }
    request_fingerprint = canonical_fingerprint(
        "evidence-vault-exact-relation-supplement-request-v1",
        request_payload,
    )
    representatives = canonical_evidence_representatives(pack["evidence"], subject_url=SUBJECT)
    quote_by_ref = {
        "owned.1": "agent-to-agent network for finance teams",
        "linkedin.1": "puts buyers and suppliers on the same network",
    }
    members = []
    for fingerprint, row in representatives.items():
        identity = project_evidence_memory_row_identity(
            {**row, "evidence_fingerprint": fingerprint},
            brand_domain=brand,
        )
        assert identity is not None
        members.append(
            {
                "evidence_fingerprint": fingerprint,
                "evidence_id": identity["evidence_id"],
                "source_identity_id": identity["document_id"],
                "ref": row["ref"],
                "url": row["url"],
                "source_class": row["metadata"]["source_class"],
                "channel_role": _channel_role(row),
                "literal_quote": quote_by_ref.get(row["ref"], row["content"]),
            }
        )
    members.sort(key=lambda row: (row["source_identity_id"], row["evidence_fingerprint"]))
    group_id = canonical_fingerprint(
        "evidence-vault-exact-relation-composite-claim-v1",
        {
            "request_fingerprint": request_fingerprint,
            "tile_contract_registry_fingerprint": tile_contract_registry_fingerprint(),
            "assessment_candidate_id": candidate_id,
            "tile_id": "C7",
            "decision_rule": "all_of",
            "members": members,
        },
    )
    relations = []
    for member in members:
        relation_id = canonical_fingerprint(
            "evidence-vault-exact-relation-v1",
            {
                "request_fingerprint": request_fingerprint,
                "assessment_candidate_id": candidate_id,
                "group_id": group_id,
                "tile_id": "C7",
                "evidence_id": member["evidence_id"],
                "source_identity_id": member["source_identity_id"],
                "claim_id": group_id,
                "literal_quote": member["literal_quote"],
                "polarity": "supports",
            },
        )
        relations.append(
            {
                **member,
                "relation_id": relation_id,
                "tile_id": "C7",
                "polarity": "supports",
                "claim_id": group_id,
                "review_status": "unreviewed",
                "decision_event_id": None,
            }
        )
    contract = next(
        row for row in build_tile_contract_registry()["tiles"] if row["tile_id"] == "C7"
    )
    group = {
        "group_id": group_id,
        "assessment_candidate_id": candidate_id,
        "tile_id": "C7",
        "tile_key": contract["tile_key"],
        "tile_condition": contract["condition"],
        "evidence_contract_ok": contract["evidence_contract"]["ok"],
        "evidence_contract_reject": contract["evidence_contract"]["reject"],
        "decision_rule": "all_of",
        "group_contract": {
            "decision_rule": "all_of",
            "minimum_member_count": len(members),
            "required_distinct_source_identity_count": len(members),
            "required_channel_roles": ["external_social_profile", "owned_web"],
            "member_decisions_must_match": True,
            "member_change_requires_review": True,
        },
        "relations": relations,
        "assessment_rationale": "The phrase survives across exact channels.",
        "assessment_limitation": "Pending exact relation review.",
        "review_status": "unreviewed",
        "decision_event_id": None,
    }
    unsigned = {
        "schema_version": EVIDENCE_VAULT_EXACT_RELATION_SUPPLEMENT_VERSION,
        "brand_identity": brand,
        "subject_url": SUBJECT,
        "parent_canonical_memory_version": parent,
        "source_evidence_pack_canonical_sha256": pack_sha,
        "source_assessment_audit_fingerprint": audit,
        "source_assessment_worksheet_sha256": worksheet,
        "source_assessment_reviewer_id": reviewer,
        "source_assessment_reviewed_at": reviewed_at,
        "request_fingerprint": request_fingerprint,
        "tile_contract_registry_fingerprint": tile_contract_registry_fingerprint(),
        "reducer_policy_fingerprint": reducer_policy_fingerprint(),
        "aggregation_policy_fingerprint": canonical_aggregation_policy_fingerprint(),
        "selected_tile_ids": ["C7"],
        "groups": [group],
        "relation_count": len(relations),
        "adoption_eligible": False,
        "authority": False,
        "runtime_effect": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "cutover_authorized": False,
    }
    artifact = {
        **unsigned,
        "artifact_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_EXACT_RELATION_SUPPLEMENT_VERSION,
            unsigned,
        ),
    }
    validate_exact_relation_supplement_structure(artifact)
    return artifact


def _inputs() -> dict:
    report = _report()
    capture = build_historical_report_capture_observation(
        historical_report=report,
        source_raw_bytes_sha256=RAW_SHA,
        source_artifact_name="historical-c7-1.json",
    )
    normalized = derive_normalized_evidence_pack(
        report["raw"]["flow"]["candidate"]["evidence_pack"]
    )
    return {
        "workspace_slug": "b3s",
        "seed_id": "causa-prima-c7",
        "lineage_export_identity": "causa-prima-c7:1",
        "lineage_export_ordinal": 1,
        "expected_predecessor_event_fingerprint": None,
        "source_historical_report": report,
        "source_raw_bytes_sha256": RAW_SHA,
        "source_artifact_name": "historical-c7-1.json",
        "capture_observation": capture,
        "normalized_evidence_pack": normalized,
        "exact_relation_supplement": _exact_c7_source(normalized),
    }


def test_lineage_replay_import_does_not_eagerly_import_history_repository() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from src.services.evidence_vault_lineage_replay "
                "import build_lineage_seed_export_v2; "
                "assert build_lineage_seed_export_v2 is not None; "
                "assert 'src.history.repository' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_report_replay_is_strict_truthful_and_deterministic() -> None:
    report = _report()
    first = build_historical_report_capture_observation(
        historical_report=report,
        source_raw_bytes_sha256=RAW_SHA,
        source_artifact_name="historical-c7-1.json",
    )
    second = build_historical_report_capture_observation(
        historical_report=deepcopy(report),
        source_raw_bytes_sha256=RAW_SHA,
        source_artifact_name="historical-c7-1.json",
    )

    assert first == second
    assert first["source_scan_id"].startswith("lineage-replay:")
    assert first["capture_payload"] == report["raw"]["flow"]["candidate"]
    assert first["evidence_records"] == first["capture_payload"]["evidence_pack"]["evidence"]
    assert first["acquisition_attempts"] == report["attempts"]
    assert first["artifacts"] == report["acquisition_artifacts"]
    assert first["metadata"]["provenance"] == "report_derived_candidate_capture"
    assert first["metadata"]["source_raw_hash_verification"] == (
        "declared_unverified_no_source_bytes"
    )
    for field in (
        "original_capture_envelope",
        "authority",
        "runtime_effect",
        "production_runtime_effect",
        "scanner_runtime_effect",
        "cutover_authorized",
    ):
        assert first["metadata"][field] is False
    assert parse_capture_observation(first).raw_observation == first


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda report: report["raw"]["flow"].pop("candidate"), "no embedded acquisition candidate"),
        (
            lambda report: report["raw"]["flow"]["candidate"]["evidence_pack"].update(evidence=[]),
            "no embedded evidence",
        ),
        (
            lambda report: report["raw"]["flow"]["candidate"].update(api_key="secret"),
            "credential-shaped key",
        ),
        (
            lambda report: report["raw"]["flow"]["candidate"].update(
                url="https://other-brand.example"
            ),
            "candidate URL contradicts the report identity",
        ),
    ],
)
def test_report_replay_rejects_missing_or_unsafe_embedded_source(mutation, message: str) -> None:
    report = _report()
    mutation(report)
    with pytest.raises(EvidenceVaultLineageReplayError, match=message):
        build_historical_report_capture_observation(
            historical_report=report,
            source_raw_bytes_sha256=RAW_SHA,
            source_artifact_name="historical-c7-1.json",
        )


def test_report_replay_rejects_malformed_sha() -> None:
    with pytest.raises(EvidenceVaultLineageReplayError, match="lowercase SHA-256"):
        build_historical_report_capture_observation(
            historical_report=_report(),
            source_raw_bytes_sha256="F" * 64,
            source_artifact_name="historical-c7-1.json",
        )


def test_seed_export_binds_exact_c7_source_to_exact_report_capture() -> None:
    inputs = _inputs()
    artifact = build_lineage_seed_export_v2(**inputs)

    assert artifact["schema_version"] == EVIDENCE_VAULT_LINEAGE_SEED_EXPORT_VERSION
    assert artifact["lineage_kind"] == "report_derived_candidate_capture"
    assert artifact["source_historical_report"] == inputs["source_historical_report"]
    assert artifact["capture_sequence"] == artifact["lineage_export_ordinal"] == 1
    assert artifact["expected_predecessor_event_fingerprint"] is None
    assert artifact["group_count"] == 1
    assert artifact["relation_count"] == 2
    assert {row["channel_role"] for row in artifact["evidence_bindings"]} == {
        "owned_web",
        "external_social_profile",
    }
    assert all(row["decision_rule"] == "all_of" for row in artifact["evidence_bindings"])
    for field in (
        "adoption_eligible",
        "authority",
        "runtime_effect",
        "production_runtime_effect",
        "scanner_runtime_effect",
        "cutover_authorized",
    ):
        assert artifact[field] is False
    validate_lineage_seed_export_v2(artifact)
    assert build_lineage_seed_export_v2(**deepcopy(inputs)) == artifact


def test_exact_source_rejects_c7_group_with_extra_member() -> None:
    report = deepcopy(_inputs()["source_historical_report"])
    report["raw"]["flow"]["candidate"]["evidence_pack"]["evidence"].append(
        {
            "ref": "linkedin.2",
            "source": "linkedin",
            "evidence_type": "company_profile",
            "url": "https://www.linkedin.com/company/causa-prima-community",
            "content": "Causa Prima community profile for finance operations teams.",
            "metadata": {
                "source_class": "external_proof",
                "identity_match_llm": "brand_name",
                "stance": "support",
            },
        }
    )
    normalized = derive_normalized_evidence_pack(
        report["raw"]["flow"]["candidate"]["evidence_pack"]
    )
    with pytest.raises(
        EvidenceVaultExactRelationSupplementError,
        match="group state is invalid",
    ):
        _exact_c7_source(normalized)


@pytest.mark.parametrize("target", ["quote", "identity", "capture", "normalized_pack"])
def test_seed_export_rejects_tampered_relation_or_capture(target: str) -> None:
    inputs = _inputs()
    if target == "quote":
        source = deepcopy(inputs["exact_relation_supplement"])
        source["groups"][0]["relations"][0]["literal_quote"] = "not in evidence"
        # A caller can rehash a tampered source; semantic rederivation must still reject it.
        unsigned = {key: value for key, value in source.items() if key != "artifact_fingerprint"}
        source["artifact_fingerprint"] = canonical_fingerprint(
            EVIDENCE_VAULT_EXACT_RELATION_SUPPLEMENT_VERSION,
            unsigned,
        )
        inputs["exact_relation_supplement"] = source
    elif target == "identity":
        source = deepcopy(inputs["exact_relation_supplement"])
        source["groups"][0]["relations"][0]["evidence_id"] = "9" * 64
        unsigned = {key: value for key, value in source.items() if key != "artifact_fingerprint"}
        source["artifact_fingerprint"] = canonical_fingerprint(
            EVIDENCE_VAULT_EXACT_RELATION_SUPPLEMENT_VERSION,
            unsigned,
        )
        inputs["exact_relation_supplement"] = source
    elif target == "capture":
        inputs["capture_observation"]["evidence_records"][0]["content"] += " tampered"
    else:
        inputs["normalized_evidence_pack"]["evidence"][0]["content"] += " tampered"

    with pytest.raises(EvidenceVaultLineageReplayError):
        build_lineage_seed_export_v2(**inputs)


def test_seed_export_validator_rejects_unknown_fields_and_recomputed_top_hashes() -> None:
    artifact = build_lineage_seed_export_v2(**_inputs())
    artifact["forged"] = True
    with pytest.raises(EvidenceVaultLineageReplayError, match="fields are invalid"):
        validate_lineage_seed_export_v2(artifact)

    forged = build_lineage_seed_export_v2(**_inputs())
    forged["source_historical_report"]["limitations"].append(
        "nested mutation with recomputed top-level hashes"
    )
    forged.pop("lineage_export_fingerprint")
    forged.pop("artifact_fingerprint")
    forged["lineage_export_fingerprint"] = canonical_fingerprint(
        f"{EVIDENCE_VAULT_LINEAGE_SEED_EXPORT_VERSION}-manifest",
        forged,
    )
    forged["artifact_fingerprint"] = canonical_fingerprint(
        EVIDENCE_VAULT_LINEAGE_SEED_EXPORT_VERSION,
        forged,
    )
    with pytest.raises(EvidenceVaultLineageReplayError):
        validate_lineage_seed_export_v2(forged)


def test_later_seed_requires_exact_predecessor_event_fingerprint() -> None:
    inputs = _inputs()
    inputs["lineage_export_ordinal"] = 2
    inputs["lineage_export_identity"] = "causa-prima-c7:2"
    with pytest.raises(EvidenceVaultLineageReplayError, match="require a predecessor"):
        build_lineage_seed_export_v2(**inputs)

    inputs["expected_predecessor_event_fingerprint"] = "6" * 64
    artifact = build_lineage_seed_export_v2(**inputs)
    assert artifact["capture_sequence"] == 2
    assert artifact["replay_origin_sequence"] == 2
    assert artifact["expected_predecessor_event_fingerprint"] == "6" * 64
    validate_lineage_seed_export_v2(artifact)

    inputs["replay_origin_sequence"] = 1
    chained = build_lineage_seed_export_v2(**inputs)
    assert chained["capture_sequence"] == 2
    assert chained["replay_origin_sequence"] == 1
    validate_lineage_seed_export_v2(chained)

    inputs["replay_origin_sequence"] = 3
    with pytest.raises(EvidenceVaultLineageReplayError, match="cannot exceed"):
        build_lineage_seed_export_v2(**inputs)


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)
def test_postgres_lineage_binding_validates_exact_members_and_hashes() -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    from src.history.repository import PostgresHistoryRepository

    if os.environ.get("B3S_TEST_ALLOW_SCHEMA_DROP") != "1":
        pytest.fail("B3S_TEST_ALLOW_SCHEMA_DROP=1 is required")
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    repository = PostgresHistoryRepository(dsn)
    repository.migrate()
    inputs = _inputs()
    repository.persist_capture_observation(inputs["capture_observation"])
    exact_artifact = inputs["exact_relation_supplement"]
    source_packet_id = uuid4()
    source_manifest: dict[str, object] = {
        "schema_version": "evidence-vault-candidate-packet-v1"
    }
    source_tiles = [{} for _ in range(80)]
    source_packet_fingerprint = canonical_fingerprint(
        "evidence-vault-candidate-packet-fingerprint-v1",
        {"manifest": source_manifest, "candidate_tiles": source_tiles},
    )
    source_resolution = {
        "schema_version": "evidence-vault-operational-source-resolution-v1",
        "source_kind": "exact_relation_supplement",
        "artifact_fingerprint": exact_artifact["artifact_fingerprint"],
        "artifact": exact_artifact,
    }
    source_resolution_fingerprint = canonical_fingerprint(
        "evidence-vault-operational-source-resolution-v1",
        source_resolution,
    )
    source_payload = {
        "manifest": source_manifest,
        "candidate_tiles": source_tiles,
        "candidate_packet_fingerprint": source_packet_fingerprint,
    }
    with psycopg.connect(dsn) as conn:
        brand_id = conn.execute(
            """
            SELECT id FROM b3s_history.brands
            WHERE canonical_domain = 'causaprima.ai'
            """
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO b3s_history.evidence_vault_canonical_memory_packets (
                id, brand_id, packet_fingerprint, schema_version,
                brand_identity, parent_canonical_memory_version,
                reference_resolution_fingerprint, reference_resolution,
                manifest, candidate_tiles, authority_state, authority,
                production_runtime_effect, scanner_runtime_effect,
                packet_kind, packet_payload
            ) VALUES (
                %s, %s, %s, 'evidence-vault-candidate-packet-v1', 'causaprima.ai', %s,
                %s, %s, %s, %s, 'pending_review', false,
                false, false, 'operational_source_v2', %s
            )
            """,
            (
                source_packet_id,
                brand_id,
                source_packet_fingerprint,
                exact_artifact["parent_canonical_memory_version"],
                source_resolution_fingerprint,
                Jsonb(source_resolution),
                Jsonb(source_manifest),
                Jsonb(source_tiles),
                Jsonb(source_payload),
            ),
        )

    bad_operational_payload = {
        "candidate_packet_fingerprint": "a" * 64,
    }
    bad_storage_resolution = {
        "schema_version": "evidence-vault-operational-storage-resolution-v1",
        "reference_resolution_fingerprint": "b" * 64,
    }
    with psycopg.connect(dsn) as conn:
        with pytest.raises(
            psycopg.Error,
            match="operational packet schema is invalid",
        ):
            conn.execute(
                """
                INSERT INTO b3s_history.evidence_vault_canonical_memory_packets (
                    id, brand_id, packet_fingerprint, schema_version,
                    brand_identity, parent_canonical_memory_version,
                    reference_resolution_fingerprint, reference_resolution,
                    manifest, candidate_tiles, authority_state, authority,
                    production_runtime_effect, scanner_runtime_effect,
                    packet_kind, packet_payload,
                    accepted_memory_candidate_version, candidate_overlay_version
                ) VALUES (
                    %s, %s, %s,
                    'evidence-vault-operational-memory-packet-v2',
                    'causaprima.ai', %s, %s, %s, '{}'::jsonb, '[]'::jsonb,
                    'pending_review', false, false, false,
                    'operational_v2', %s, %s, %s
                )
                """,
                (
                    uuid4(),
                    brand_id,
                    "a" * 64,
                    exact_artifact["parent_canonical_memory_version"],
                    "b" * 64,
                    Jsonb(bad_storage_resolution),
                    Jsonb(bad_operational_payload),
                    "c" * 64,
                    "d" * 64,
                ),
            )

    missing_resolution_schema = dict(source_resolution)
    missing_resolution_schema.pop("schema_version")
    missing_resolution_manifest = {
        "schema_version": "evidence-vault-candidate-packet-v1",
        "variant": "missing-resolution-schema",
    }
    missing_resolution_fingerprint = canonical_fingerprint(
        "evidence-vault-candidate-packet-fingerprint-v1",
        {
            "manifest": missing_resolution_manifest,
            "candidate_tiles": source_tiles,
        },
    )
    with psycopg.connect(dsn) as conn:
        with pytest.raises(
            psycopg.Error,
            match="source resolution schema is invalid",
        ):
            conn.execute(
                """
                INSERT INTO b3s_history.evidence_vault_canonical_memory_packets (
                    id, brand_id, packet_fingerprint, schema_version,
                    brand_identity, parent_canonical_memory_version,
                    reference_resolution_fingerprint, reference_resolution,
                    manifest, candidate_tiles, authority_state, authority,
                    production_runtime_effect, scanner_runtime_effect,
                    packet_kind, packet_payload
                ) VALUES (
                    %s, %s, %s, 'evidence-vault-candidate-packet-v1',
                    'causaprima.ai', %s, %s, %s, %s, %s,
                    'pending_review', false, false, false,
                    'operational_source_v2', %s
                )
                """,
                (
                    uuid4(),
                    brand_id,
                    missing_resolution_fingerprint,
                    exact_artifact["parent_canonical_memory_version"],
                    "e" * 64,
                    Jsonb(missing_resolution_schema),
                    Jsonb(missing_resolution_manifest),
                    Jsonb(source_tiles),
                    Jsonb(
                        {
                            "manifest": missing_resolution_manifest,
                            "candidate_tiles": source_tiles,
                            "candidate_packet_fingerprint": (
                                missing_resolution_fingerprint
                            ),
                        }
                    ),
                ),
            )

    missing_artifact_schema = deepcopy(source_resolution)
    missing_artifact_schema["artifact"].pop("schema_version")
    missing_artifact_resolution_fingerprint = canonical_fingerprint(
        "evidence-vault-operational-source-resolution-v1",
        missing_artifact_schema,
    )
    missing_artifact_manifest = {
        "schema_version": "evidence-vault-candidate-packet-v1",
        "variant": "missing-artifact-schema",
    }
    missing_artifact_packet_fingerprint = canonical_fingerprint(
        "evidence-vault-candidate-packet-fingerprint-v1",
        {
            "manifest": missing_artifact_manifest,
            "candidate_tiles": source_tiles,
        },
    )
    with psycopg.connect(dsn) as conn:
        with pytest.raises(
            psycopg.Error,
            match="exact source artifact fingerprint is invalid",
        ):
            conn.execute(
                """
                INSERT INTO b3s_history.evidence_vault_canonical_memory_packets (
                    id, brand_id, packet_fingerprint, schema_version,
                    brand_identity, parent_canonical_memory_version,
                    reference_resolution_fingerprint, reference_resolution,
                    manifest, candidate_tiles, authority_state, authority,
                    production_runtime_effect, scanner_runtime_effect,
                    packet_kind, packet_payload
                ) VALUES (
                    %s, %s, %s, 'evidence-vault-candidate-packet-v1',
                    'causaprima.ai', %s, %s, %s, %s, %s,
                    'pending_review', false, false, false,
                    'operational_source_v2', %s
                )
                """,
                (
                    uuid4(),
                    brand_id,
                    missing_artifact_packet_fingerprint,
                    exact_artifact["parent_canonical_memory_version"],
                    missing_artifact_resolution_fingerprint,
                    Jsonb(missing_artifact_schema),
                    Jsonb(missing_artifact_manifest),
                    Jsonb(source_tiles),
                    Jsonb(
                        {
                            "manifest": missing_artifact_manifest,
                            "candidate_tiles": source_tiles,
                            "candidate_packet_fingerprint": (
                                missing_artifact_packet_fingerprint
                            ),
                        }
                    ),
                ),
            )

    lineage_export = build_lineage_seed_export_v2(**inputs)
    stored, replayed = repository.bind_evidence_vault_exact_source_capture_lineage(
        "causaprima.ai",
        lineage_export,
    )

    assert replayed is False
    assert stored["member_count"] == 2
    assert len(stored["binding_fingerprint"]) == 64
    with psycopg.connect(dsn) as conn:
        conn.execute(
            """
            UPDATE b3s_history.scan_runs
            SET status = 'completed', completed_at = now(),
                metadata = jsonb_set(metadata, '{diagnostic}', 'true'::jsonb)
            WHERE source_scan_id = %s
            """,
            (inputs["capture_observation"]["source_scan_id"],),
        )

    with psycopg.connect(dsn) as lock_conn:
        lock_conn.execute(
            """
            SELECT pg_advisory_xact_lock(
                b3s_history.evidence_vault_brand_lock_key(
                    workspace_id,
                    id
                )
            )
            FROM b3s_history.brands
            WHERE id = %s
            """,
            (brand_id,),
        )
        with psycopg.connect(dsn, autocommit=True) as concurrent_conn:
            concurrent_conn.execute("SET statement_timeout = '1s'")
            concurrent_conn.execute(
                """
                UPDATE b3s_history.scan_runs
                SET status = status
                WHERE source_scan_id = %s
                """,
                (inputs["capture_observation"]["source_scan_id"],),
            )
        lock_conn.execute("SET LOCAL statement_timeout = '1s'")
        lock_conn.execute(
            """
            UPDATE b3s_history.scan_runs
            SET status = status
            WHERE source_scan_id = %s
            """,
            (inputs["capture_observation"]["source_scan_id"],),
        )

    for statement, parameters, message in (
        (
            "UPDATE b3s_history.captures SET content_hash = %s",
            ("0" * 64,),
            "immutable lineage input",
        ),
        (
            "UPDATE b3s_history.scan_runs SET request_payload = '{}'::jsonb",
            (),
            "identity, request, and observation hash are immutable",
        ),
        (
            """
            UPDATE b3s_history.scan_runs
            SET metadata = jsonb_set(metadata, '{observation_hash}', to_jsonb(%s::text))
            """,
            ("0" * 64,),
            "identity, request, and observation hash are immutable",
        ),
        (
            "UPDATE b3s_history.evidence_records SET content = 'forged'",
            (),
            "capture evidence is immutable",
        ),
    ):
        with psycopg.connect(dsn) as conn:
            with pytest.raises(psycopg.Error, match=message):
                conn.execute(statement, parameters)

    with psycopg.connect(dsn) as conn:
        watermarked_capture_id = conn.execute(
            """
            SELECT capture_id
            FROM b3s_history.evidence_vault_capture_watermark_events
            ORDER BY capture_sequence DESC
            LIMIT 1
            """
        ).fetchone()[0]
        unwatermarked_scan_id = uuid4()
        unwatermarked_capture_id = uuid4()
        movable_evidence_id = uuid4()
        conn.execute(
            """
            INSERT INTO b3s_history.scan_runs (
                id, workspace_id, brand_id, source_scan_id, status,
                pipeline_version, requested_at, request_payload, metadata
            )
            SELECT %s, workspace_id, id, %s, 'completed', 'test-v1',
                   now(), '{}'::jsonb, '{}'::jsonb
            FROM b3s_history.brands
            WHERE id = %s
            """,
            (
                unwatermarked_scan_id,
                f"unwatermarked-{unwatermarked_scan_id}",
                brand_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO b3s_history.captures (
                id, scan_run_id, brand_id, observed_at, recorded_at,
                source_url, content_hash
            ) VALUES (%s, %s, %s, now(), now(), %s, %s)
            """,
            (
                unwatermarked_capture_id,
                unwatermarked_scan_id,
                brand_id,
                SUBJECT,
                "5" * 64,
            ),
        )
        conn.execute(
            """
            INSERT INTO b3s_history.evidence_records (
                id, capture_id, evidence_ref, source, evidence_type,
                content, content_hash
            ) VALUES (%s, %s, 'movable.1', 'test', 'test', 'movable', %s)
            """,
            (
                movable_evidence_id,
                unwatermarked_capture_id,
                hashlib.sha256(b"movable").hexdigest(),
            ),
        )
        with pytest.raises(psycopg.Error, match="cannot change its capture"):
            conn.execute(
                """
                UPDATE b3s_history.evidence_records
                SET capture_id = %s
                WHERE id = %s
                """,
                (watermarked_capture_id, movable_evidence_id),
            )
        conn.rollback()

    duplicate_manifest = {
        "schema_version": "evidence-vault-candidate-packet-v1",
        "variant": "duplicate",
    }
    duplicate_packet_fingerprint = canonical_fingerprint(
        "evidence-vault-candidate-packet-fingerprint-v1",
        {"manifest": duplicate_manifest, "candidate_tiles": source_tiles},
    )
    duplicate_payload = {
        "manifest": duplicate_manifest,
        "candidate_tiles": source_tiles,
        "candidate_packet_fingerprint": duplicate_packet_fingerprint,
    }
    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.Error, match="exact_source_artifact"):
            conn.execute(
                """
                INSERT INTO b3s_history.evidence_vault_canonical_memory_packets (
                    id, brand_id, packet_fingerprint, schema_version,
                    brand_identity, parent_canonical_memory_version,
                    reference_resolution_fingerprint, reference_resolution,
                    manifest, candidate_tiles, authority_state, authority,
                    production_runtime_effect, scanner_runtime_effect,
                    packet_kind, packet_payload
                )
                SELECT %s, brand_id, %s, schema_version, brand_identity,
                       parent_canonical_memory_version,
                       reference_resolution_fingerprint,
                       reference_resolution, %s, candidate_tiles,
                       authority_state, authority, production_runtime_effect,
                       scanner_runtime_effect, packet_kind, %s
                FROM b3s_history.evidence_vault_canonical_memory_packets
                WHERE id = %s
                """,
                (
                    uuid4(),
                    duplicate_packet_fingerprint,
                    Jsonb(duplicate_manifest),
                    Jsonb(duplicate_payload),
                    source_packet_id,
                ),
            )

    for keep_count in (0, 1):
        with psycopg.connect(dsn) as conn:
            conn.execute(
                """
                ALTER TABLE b3s_history.evidence_vault_operational_source_capture_lineage_members
                DISABLE TRIGGER evidence_vault_source_capture_lineage_members_append_only
                """
            )
            conn.execute(
                """
                DELETE FROM b3s_history.evidence_vault_operational_source_capture_lineage_members
                WHERE id NOT IN (
                    SELECT id
                    FROM b3s_history.evidence_vault_operational_source_capture_lineage_members
                    ORDER BY relation_id
                    LIMIT %s
                )
                """,
                (keep_count,),
            )
            with pytest.raises(psycopg.Error, match="exactly two independent"):
                conn.execute(
                    """
                    SELECT b3s_history.validate_evidence_vault_lineage_binding_content(%s)
                    """,
                    (stored["binding_id"],),
                )
            conn.rollback()

    with psycopg.connect(dsn) as conn:
        binding = conn.execute(
            """
            SELECT brand_id, operational_source_packet_id, capture_id
            FROM b3s_history.evidence_vault_operational_source_capture_lineage_bindings
            WHERE id = %s
            """,
            (stored["binding_id"],),
        ).fetchone()
        member = conn.execute(
            """
            SELECT composite_group_id
            FROM b3s_history.evidence_vault_operational_source_capture_lineage_members
            WHERE binding_id = %s
            LIMIT 1
            """,
            (stored["binding_id"],),
        ).fetchone()
        extra_evidence_id = uuid4()
        conn.execute(
            """
            ALTER TABLE b3s_history.evidence_records
            DISABLE TRIGGER evidence_vault_evidence_parent_immutable
            """
        )
        conn.execute(
            """
            INSERT INTO b3s_history.evidence_records (
                id, capture_id, evidence_ref, source, source_class,
                evidence_type, url, content, content_hash, confidence, metadata
            ) VALUES (
                %s, %s, 'extra.1', 'test', 'other', 'test', '',
                'extra evidence', %s, 'medium', '{}'::jsonb
            )
            """,
            (extra_evidence_id, binding[2], hashlib.sha256(b"extra evidence").hexdigest()),
        )
        conn.execute(
            """
            INSERT INTO b3s_history.evidence_vault_operational_source_capture_lineage_members (
                id, brand_id, binding_id, operational_source_packet_id,
                capture_id, evidence_record_id, composite_group_id, relation_id,
                evidence_id, source_identity_id, evidence_fingerprint,
                channel_role, evidence_ref, source_ref, evidence_quote,
                member_fingerprint, authority, production_runtime_effect,
                scanner_runtime_effect
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'owned_web', 'extra.1', 'extra.1', 'extra', %s,
                false, false, false
            )
            """,
            (
                uuid4(), binding[0], stored["binding_id"], binding[1],
                binding[2], extra_evidence_id, member[0], "0" * 64,
                "1" * 64, "2" * 64, "3" * 64, "4" * 64,
            ),
        )
        with pytest.raises(psycopg.Error, match="exactly two independent"):
            conn.execute(
                """
                SELECT b3s_history.validate_evidence_vault_lineage_binding_content(%s)
                """,
                (stored["binding_id"],),
            )
        conn.rollback()

    with psycopg.connect(dsn) as conn:
        member_count = conn.execute(
            """
            SELECT count(*)
            FROM b3s_history.evidence_vault_operational_source_capture_lineage_members
            """
        ).fetchone()[0]
        assert member_count == 2
        with pytest.raises(psycopg.Error, match="cannot be truncated"):
            conn.execute(
                """
                TRUNCATE b3s_history.evidence_vault_operational_source_capture_lineage_members
                """
            )
