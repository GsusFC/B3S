from copy import deepcopy
import hashlib
import json

import pytest

from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.services.evidence_vault_incremental_refresh import (
    EVIDENCE_VAULT_LEGACY_INCREMENTAL_DELTA_VERSION,
    EvidenceVaultOperationPlanError,
    build_vault_scan_plan,
    validate_vault_scan_plan,
)
from src.services.scanner_analysis_contract import analysis_contract_from_report
from src.services.evidence_vault_semantic_analysis_contract import (
    EvidenceVaultSemanticAnalysisContractError,
    current_semantic_analysis_contract,
    validate_semantic_analysis_contract,
)


def test_current_semantic_contract_is_canonical_and_runtime_bound() -> None:
    contract = current_semantic_analysis_contract()

    assert validate_semantic_analysis_contract(contract) == contract
    assert validate_semantic_analysis_contract(
        contract, require_current=True
    ) == contract
    assert contract["evidence_labeling_version"] == "sv9-flow-evidence-labeling-v4"
    assert contract["relation_proposal_version"] == (
        "evidence-tile-relation-proposal-v3"
    )


def test_future_well_formed_contract_is_not_available_in_current_runtime() -> None:
    future = current_semantic_analysis_contract()
    future["relation_policy_version"] = "evidence-tile-relation-policy-v4"
    unsigned = {
        key: value
        for key, value in future.items()
        if key != "semantic_analysis_contract_fingerprint"
    }
    future["semantic_analysis_contract_fingerprint"] = canonical_fingerprint(
        future["schema_version"], unsigned
    )

    assert validate_semantic_analysis_contract(future) == future
    with pytest.raises(
        EvidenceVaultSemanticAnalysisContractError,
        match="not available",
    ):
        validate_semantic_analysis_contract(future, require_current=True)



def test_report_analysis_identity_includes_vault_semantic_contract() -> None:
    legacy_report = {"raw": {"flow": {"interpretation_debug": {}}}}
    current_report = {
        "raw": {
            "flow": {
                "interpretation_debug": {
                    "semantic_analysis_contract": (
                        current_semantic_analysis_contract()
                    )
                }
            }
        }
    }

    legacy = analysis_contract_from_report(legacy_report)
    current = analysis_contract_from_report(current_report)
    legacy_values = {
        key: value
        for key, value in legacy.items()
        if key not in {"schema_version", "fingerprint"}
    }
    expected_legacy_fingerprint = hashlib.sha256(
        json.dumps(
            legacy_values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert "vault_semantic_analysis_contract_fingerprint" not in legacy
    assert legacy["fingerprint"] == expected_legacy_fingerprint
    assert current["vault_semantic_analysis_contract_fingerprint"] == (
        current_semantic_analysis_contract()[
            "semantic_analysis_contract_fingerprint"
        ]
    )
    assert current["fingerprint"] != legacy["fingerprint"]

def test_legacy_plan_context_remains_valid_without_becoming_current() -> None:
    plan = build_vault_scan_plan(
        brand_identity="example.com",
        subject_url="https://example.com",
        mode="baseline",
        current_evidence_records=[_row()],
    )
    legacy = deepcopy(plan)
    context_unsigned = {
        "scope": legacy["semantic_context"]["scope"],
        "evidence_fingerprints": legacy["semantic_context"][
            "evidence_fingerprints"
        ],
    }
    legacy["semantic_context"] = {
        **context_unsigned,
        "context_fingerprint": canonical_fingerprint(
            "evidence-vault-semantic-context-v1",
            context_unsigned,
        ),
    }
    legacy["delta"]["schema_version"] = (
        EVIDENCE_VAULT_LEGACY_INCREMENTAL_DELTA_VERSION
    )
    unsigned = {
        key: value
        for key, value in legacy.items()
        if key != "operation_plan_fingerprint"
    }
    legacy["operation_plan_fingerprint"] = canonical_fingerprint(
        legacy["schema_version"], unsigned
    )

    validate_vault_scan_plan(legacy)
    tampered = deepcopy(legacy)
    tampered["semantic_context"]["evidence_fingerprints"] = []
    with pytest.raises(EvidenceVaultOperationPlanError):
        validate_vault_scan_plan(tampered)


def _row() -> dict[str, object]:
    return {
        "ref": "home",
        "source": "web",
        "evidence_type": "copy",
        "url": "https://example.com",
        "content": "Stable evidence",
        "confidence": "high",
        "metadata": {"source_class": "owned_copy"},
    }
