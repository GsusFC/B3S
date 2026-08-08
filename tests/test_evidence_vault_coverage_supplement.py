from __future__ import annotations

from copy import deepcopy
import json

import pytest

from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.services.evidence_vault_coverage_supplement import (
    EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_RESULT_VERSION,
    EvidenceVaultCoverageSupplementError,
    build_coverage_supplement_request,
    execute_coverage_supplement,
    validate_coverage_supplement_request,
    validate_coverage_supplement_result,
)


class SupplementLLM:
    api_key = "test"

    def _call_json(self, system, user, **kwargs):
        payload = json.loads(user.split("\n", 1)[1])
        row = payload[0]
        return {
            "relations": [{
                "evidence_fingerprint": row["evidence_fingerprint"],
                "tile_id": row["allowed_tiles"][0]["tile_id"],
                "polarity": "supports",
                "literal_quote": "End the chase between companies.",
                "rationale": "The line presents a clear audience tension.",
            }]
        }


def _row() -> dict:
    return {
        "evidence_fingerprint": "a" * 64,
        "ref": "raw_inputs.1.chunk.3",
        "source": "web",
        "evidence_type": "raw_input",
        "url": "https://causaprima.ai",
        "content": "## End the chase between companies.",
        "confidence": "high",
        "metadata": {
            "source_class": "owned_copy",
            "identity_match": "domain",
        },
    }


def _request() -> dict:
    return build_coverage_supplement_request(
        brand_identity="causaprima.ai",
        subject_url="https://causaprima.ai",
        parent_canonical_memory_version="b" * 64,
        source_evidence_pack_sha256="c" * 64,
        evidence_rows=[_row()],
        tile_shortlists={"a" * 64: ["MG1", "MG3"]},
        target_tile_ids=["MG1", "MG3", "VA4"],
        rationale="Review explicit missing coverage without acquisition drift.",
    )


def test_coverage_supplement_is_bounded_pending_and_reproducible() -> None:
    request = _request()
    validate_coverage_supplement_request(request)

    result = execute_coverage_supplement(
        request,
        evidence_rows=[_row()],
        llm=SupplementLLM(),
    )

    assert result["authority"] is False
    assert result["runtime_effect"] is False
    assert result["uncovered_target_tile_ids"] == ["MG3", "VA4"]
    assert result["basis_relations"][0]["review_status"] == "unreviewed"
    assert result["basis_relations"][0]["decision_event_id"] is None
    assert len(result["basis_relations"][0]["relation_id"]) == 64
    validate_coverage_supplement_result(result, request=request)


def test_coverage_supplement_rejects_cross_brand_owned_evidence() -> None:
    with pytest.raises(
        EvidenceVaultCoverageSupplementError,
        match="brand and subject identities mismatch",
    ):
        build_coverage_supplement_request(
            brand_identity="victim.example",
            subject_url="https://causaprima.ai",
            parent_canonical_memory_version="b" * 64,
            source_evidence_pack_sha256="c" * 64,
            evidence_rows=[_row()],
            tile_shortlists={"a" * 64: ["MG1"]},
            target_tile_ids=["MG1"],
            rationale="Cross-brand request must fail closed.",
        )

    foreign_row = _row()
    foreign_row["url"] = "https://other-brand.example"
    with pytest.raises(
        EvidenceVaultCoverageSupplementError,
        match="owned evidence identity mismatch",
    ):
        build_coverage_supplement_request(
            brand_identity="causaprima.ai",
            subject_url="https://causaprima.ai",
            parent_canonical_memory_version="b" * 64,
            source_evidence_pack_sha256="c" * 64,
            evidence_rows=[foreign_row],
            tile_shortlists={"a" * 64: ["MG1"]},
            target_tile_ids=["MG1"],
            rationale="Foreign owned evidence must fail closed.",
        )


def test_coverage_supplement_rejects_cross_tile_basis_tampering() -> None:
    request = _request()
    result = execute_coverage_supplement(
        request,
        evidence_rows=[_row()],
        llm=SupplementLLM(),
    )
    tampered = deepcopy(result)
    tampered["basis_relations"][0]["tile_id"] = "VA4"
    unsigned = {
        key: value
        for key, value in tampered.items()
        if key != "result_fingerprint"
    }
    tampered["result_fingerprint"] = canonical_fingerprint(
        EVIDENCE_VAULT_COVERAGE_SUPPLEMENT_RESULT_VERSION,
        unsigned,
    )

    with pytest.raises(
        EvidenceVaultCoverageSupplementError,
        match="basis is not derived",
    ):
        validate_coverage_supplement_result(tampered, request=request)



def test_coverage_supplement_rejects_content_relabelled_under_frozen_id() -> None:
    request = _request()
    tampered = _row()
    tampered["content"] = "Fabricated content under a borrowed evidence id."

    with pytest.raises(
        EvidenceVaultCoverageSupplementError,
        match="snapshot binding changed",
    ):
        execute_coverage_supplement(
            request,
            evidence_rows=[tampered],
            llm=SupplementLLM(),
        )

def test_coverage_supplement_rejects_unscoped_or_authoritative_requests() -> None:
    with pytest.raises(
        EvidenceVaultCoverageSupplementError,
        match="shortlist is invalid",
    ):
        build_coverage_supplement_request(
            brand_identity="causaprima.ai",
            subject_url="https://causaprima.ai",
            parent_canonical_memory_version="b" * 64,
            source_evidence_pack_sha256="c" * 64,
            evidence_rows=[_row()],
            tile_shortlists={"a" * 64: ["M1"]},
            target_tile_ids=["MG1"],
            rationale="Invalid cross-scope request.",
        )

    request = _request()
    request["authority"] = True
    with pytest.raises(
        EvidenceVaultCoverageSupplementError,
        match="authority is invalid",
    ):
        validate_coverage_supplement_request(request)
