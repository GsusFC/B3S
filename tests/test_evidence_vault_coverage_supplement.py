from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.history.repository import PostgresHistoryRepository
from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.services.evidence_vault_operational_authority import (
    EvidenceVaultOperationalAuthorityError,
)
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
            }],
            "analysis": [{
                "evidence_fingerprint": row["evidence_fingerprint"],
                "decision": "supported",
            }],
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
    assert result["relation_proposal"]["analysis_states"] == {
        "a" * 64: "supported"
    }
    validate_coverage_supplement_result(result, request=request)

    for mutate in (
        lambda proposal: proposal.pop("analysis_states"),
        lambda proposal: proposal.__setitem__("unexpected", True),
        lambda proposal: proposal["analysis_states"].__setitem__(
            "a" * 64, "unknown"
        ),
        lambda proposal: proposal["analysis_states"].__setitem__(
            "a" * 64, "analyzed_without_sufficient_support"
        ),
    ):
        broken = deepcopy(result)
        mutate(broken["relation_proposal"])
        broken["result_fingerprint"] = canonical_fingerprint(
            broken["schema_version"],
            {
                key: value
                for key, value in broken.items()
                if key != "result_fingerprint"
            },
        )
        with pytest.raises(
            EvidenceVaultCoverageSupplementError,
            match="relation proposal|relation analysis",
        ):
            validate_coverage_supplement_result(broken, request=request)


def test_persisted_relation_replay_is_audit_only_at_registration() -> None:
    root = Path(__file__).parents[1]
    artifact = json.loads(
        (
            root
            / "audits/evidence_vault_field_validation_v1"
            / "causa-prima-coverage-supplement-v1.json"
        ).read_text(encoding="utf-8")
    )
    pack = json.loads(
        (
            root
            / "fixtures/evidence_vault_field_validation_v1"
            / "causa_prima-a8ba05137817-normalized-evidence-pack.json"
        ).read_text(encoding="utf-8")
    )
    frozen = json.loads((root / "audits/evidence_vault_field_validation_v1" / "causa-prima-coverage-supplement-v2.json").read_text())
    assert set(frozen["result"]["relation_proposal"]) == {
        "schema_version",
        "relations",
        "discarded_relations",
    }
    validate_coverage_supplement_result(frozen["result"], request=frozen["request"])
    for versions in (("evidence-vault-coverage-supplement-result-v1", "evidence-tile-relation-proposal-v3"), ("evidence-vault-coverage-supplement-result-v2", "evidence-tile-relation-proposal-v2")):
        cross = deepcopy(frozen["result"]); cross["schema_version"], cross["relation_proposal"]["schema_version"] = versions; cross.update({"relation_proposal_call_count": 1} if versions[0].endswith("v2") else {}); cross["result_fingerprint"] = canonical_fingerprint(versions[0], {key: value for key, value in cross.items() if key != "result_fingerprint"})
        with pytest.raises(EvidenceVaultCoverageSupplementError, match="relation proposal"):
            validate_coverage_supplement_result(cross, request=frozen["request"])
    repository = PostgresHistoryRepository("postgresql://unused")

    with pytest.raises(
        EvidenceVaultOperationalAuthorityError,
        match="audit-only",
    ):
        repository.register_evidence_vault_coverage_supplement_source_packet(
            "causaprima.ai",
            artifact,
            evidence_pack=pack,
        )


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
