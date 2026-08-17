"""Frozen analyzer identity for forward-only Evidence Vault semantic work."""

from __future__ import annotations

from typing import Any, Mapping

from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.sv9_flow.evidence_labeling_worker import EVIDENCE_LABELING_VERSION
from src.sv9_flow.evidence_tile_relation_worker import (
    EVIDENCE_TILE_RELATION_POLICY_VERSION,
    EVIDENCE_TILE_RELATION_PROPOSAL_VERSION,
)
from src.sv9_flow.semantic_passages import SEMANTIC_PASSAGE_POLICY_VERSION


EVIDENCE_VAULT_SEMANTIC_ANALYSIS_CONTRACT_VERSION = (
    "evidence-vault-semantic-analysis-contract-v1"
)


class EvidenceVaultSemanticAnalysisContractError(ValueError):
    """The semantic analyzer contract is malformed or unsupported."""


def current_semantic_analysis_contract() -> dict[str, str]:
    unsigned = {
        "schema_version": EVIDENCE_VAULT_SEMANTIC_ANALYSIS_CONTRACT_VERSION,
        "passage_policy_version": SEMANTIC_PASSAGE_POLICY_VERSION,
        "evidence_labeling_version": EVIDENCE_LABELING_VERSION,
        "relation_proposal_version": EVIDENCE_TILE_RELATION_PROPOSAL_VERSION,
        "relation_policy_version": EVIDENCE_TILE_RELATION_POLICY_VERSION,
    }
    return {
        **unsigned,
        "semantic_analysis_contract_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_SEMANTIC_ANALYSIS_CONTRACT_VERSION,
            unsigned,
        ),
    }


def validate_semantic_analysis_contract(
    contract: Mapping[str, Any],
    *,
    require_current: bool = False,
) -> dict[str, str]:
    fields = {
        "schema_version",
        "passage_policy_version",
        "evidence_labeling_version",
        "relation_proposal_version",
        "relation_policy_version",
        "semantic_analysis_contract_fingerprint",
    }
    if not isinstance(contract, Mapping) or set(contract) != fields:
        raise EvidenceVaultSemanticAnalysisContractError(
            "semantic analysis contract fields mismatch"
        )
    normalized = {field: contract.get(field) for field in fields}
    if any(not isinstance(value, str) or not value for value in normalized.values()):
        raise EvidenceVaultSemanticAnalysisContractError(
            "semantic analysis contract values are invalid"
        )
    if normalized["schema_version"] != EVIDENCE_VAULT_SEMANTIC_ANALYSIS_CONTRACT_VERSION:
        raise EvidenceVaultSemanticAnalysisContractError(
            "semantic analysis contract schema is unsupported"
        )
    unsigned = {
        field: normalized[field]
        for field in fields
        if field != "semantic_analysis_contract_fingerprint"
    }
    if normalized["semantic_analysis_contract_fingerprint"] != canonical_fingerprint(
        EVIDENCE_VAULT_SEMANTIC_ANALYSIS_CONTRACT_VERSION,
        unsigned,
    ):
        raise EvidenceVaultSemanticAnalysisContractError(
            "semantic analysis contract fingerprint mismatch"
        )
    result = {field: str(normalized[field]) for field in fields}
    if require_current and result != current_semantic_analysis_contract():
        raise EvidenceVaultSemanticAnalysisContractError(
            "semantic analysis contract is not available in this runtime"
        )
    return result


__all__ = [
    "EVIDENCE_VAULT_SEMANTIC_ANALYSIS_CONTRACT_VERSION",
    "EvidenceVaultSemanticAnalysisContractError",
    "current_semantic_analysis_contract",
    "validate_semantic_analysis_contract",
]
