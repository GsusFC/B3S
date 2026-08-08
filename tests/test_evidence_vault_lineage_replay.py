from __future__ import annotations

from copy import deepcopy
import hashlib
import subprocess
import sys

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


def test_seed_export_rejects_self_consistent_c7_group_with_extra_member() -> None:
    inputs = _inputs()
    report = deepcopy(inputs["source_historical_report"])
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
    inputs.update(
        {
            "source_historical_report": report,
            "capture_observation": build_historical_report_capture_observation(
                historical_report=report,
                source_raw_bytes_sha256=RAW_SHA,
                source_artifact_name="historical-c7-1.json",
            ),
            "normalized_evidence_pack": normalized,
            "exact_relation_supplement": _exact_c7_source(normalized),
        }
    )
    assert len(inputs["exact_relation_supplement"]["groups"][0]["relations"]) == 3

    with pytest.raises(
        EvidenceVaultLineageReplayError,
        match="C7 requires exact all_of owned-web/external-social semantics",
    ):
        build_lineage_seed_export_v2(**inputs)


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
