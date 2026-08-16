"""Source-only semantic scoring for exact Evidence Vault candidate packets."""

from typing import Any, Mapping

from src.services.evidence_vault_canonical_core import (
    EVIDENCE_VAULT_CANDIDATE_PACKET_FINGERPRINT_VERSION,
    EvidenceVaultCanonicalCoreError,
    canonical_fingerprint,
    canonical_json,
    validate_candidate_packet,
)
from src.sv9.assessment_kernel import (
    Sv9AssessmentError,
    build_sv9_assessment,
    validate_sv9_assessment_output,
)


EVIDENCE_VAULT_SEMANTIC_ASSESSMENT_VERSION = "evidence-vault-semantic-assessment-v3"
EVIDENCE_VAULT_SEMANTIC_SCORE_INPUT_VERSION = "evidence-vault-semantic-score-input-v3"
EVIDENCE_VAULT_SEMANTIC_PROVENANCE_VERSION = "evidence-vault-semantic-provenance-v3"
_CONTRADICTION_REASON = "contradiction_requires_semantic_reassessment"
_OUTPUT_FIELDS = frozenset(
    (
        "schema_version observation_identity source_candidate_packet_fingerprint availability reason_codes "
        "score_input_fingerprint semantic_provenance_fingerprint assessment_output"
    ).split()
)


class EvidenceVaultSemanticScoringV3Error(ValueError):
    """A candidate packet cannot produce an exact semantic v3 assessment."""


def _detached_json_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceVaultSemanticScoringV3Error(f"{label} must be an object")
    try:
        snapshot = _detached_json_value(dict(value))
        canonical_json(snapshot).encode("utf-8")
        return snapshot
    except EvidenceVaultSemanticScoringV3Error:
        raise
    except Exception as exc:
        raise EvidenceVaultSemanticScoringV3Error(f"{label} is not a stable strict-JSON object") from exc


def _detached_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _detached_json_value(item) for key, item in dict(value).items()}
    if isinstance(value, list):
        return [_detached_json_value(item) for item in list(value)]
    if type(value) in (type(None), bool, int, float, str):
        return value
    raise EvidenceVaultSemanticScoringV3Error(f"unsupported JSON type: {type(value).__name__}")


def _validate_raw_packet_identity(packet: dict[str, Any]) -> None:
    manifest = packet["manifest"]
    tiles = packet["candidate_tiles"]
    unsigned = {"manifest": manifest, "candidate_tiles": tiles}
    expected = canonical_fingerprint(EVIDENCE_VAULT_CANDIDATE_PACKET_FINGERPRINT_VERSION, unsigned)
    if packet["candidate_packet_fingerprint"] != expected:
        raise EvidenceVaultSemanticScoringV3Error("candidate packet raw fingerprint mismatch")
    delta = manifest["delta_summary"]
    counts = (
        manifest["candidate_count"],
        delta["tile_state_change_count"],
        delta["score_affecting_count"],
        *delta["by_kind"].values(),
    )
    if any(type(value) is not int for value in counts):
        raise EvidenceVaultSemanticScoringV3Error("candidate packet count types are invalid")


def build_evidence_vault_semantic_assessment(*, source_candidate_packet: Mapping[str, Any]) -> dict[str, Any]:
    """Score only one detached exact candidate vector, without authority inputs."""

    packet = _detached_json_object(
        source_candidate_packet,
        label="source candidate packet",
    )
    try:
        _validate_raw_packet_identity(packet)
        validate_candidate_packet(packet)
        source_fingerprint = packet["candidate_packet_fingerprint"]
        observation_identity = canonical_fingerprint(
            EVIDENCE_VAULT_SEMANTIC_ASSESSMENT_VERSION,
            {"source_candidate_packet_fingerprint": source_fingerprint},
        )
        semantic_tiles = [
            {
                "component_key": tile["component_key"],
                "tile_id": tile["tile_id"],
                "tile_key": tile["tile_key"],
                "candidate_state": tile["candidate_state"],
            }
            for tile in packet["candidate_tiles"]
        ]
        common = {
            "schema_version": EVIDENCE_VAULT_SEMANTIC_ASSESSMENT_VERSION,
            "observation_identity": observation_identity,
            "source_candidate_packet_fingerprint": source_fingerprint,
        }
        if any(tile["candidate_state"] == "contradiction" for tile in semantic_tiles):
            return {
                **common,
                "availability": "unavailable",
                "reason_codes": [_CONTRADICTION_REASON],
                "score_input_fingerprint": None,
                "semantic_provenance_fingerprint": None,
                "assessment_output": None,
            }
        score_input_fingerprint = canonical_fingerprint(
            EVIDENCE_VAULT_SEMANTIC_SCORE_INPUT_VERSION,
            {"tiles": semantic_tiles},
        )
        assessment_output = build_sv9_assessment(
            [
                {
                    "component_key": tile["component_key"],
                    "tile_id": tile["tile_id"],
                    "tile_key": tile["tile_key"],
                    "assessment_state": tile["candidate_state"],
                }
                for tile in semantic_tiles
            ]
        )
        semantic_provenance_fingerprint = canonical_fingerprint(
            EVIDENCE_VAULT_SEMANTIC_PROVENANCE_VERSION,
            {
                "score_input_fingerprint": score_input_fingerprint,
                "kernel_assessment_fingerprint": assessment_output["assessment_fingerprint"],
                "kernel_score_fingerprint": assessment_output["score_fingerprint"],
            },
        )
    except (AttributeError, KeyError, TypeError, EvidenceVaultCanonicalCoreError, Sv9AssessmentError) as exc:
        raise EvidenceVaultSemanticScoringV3Error("source candidate semantic assessment is invalid") from exc
    return {
        **common,
        "availability": "available",
        "reason_codes": [],
        "score_input_fingerprint": score_input_fingerprint,
        "semantic_provenance_fingerprint": semantic_provenance_fingerprint,
        "assessment_output": assessment_output,
    }


def validate_evidence_vault_semantic_assessment(
    assessment: Mapping[str, Any],
    *,
    source_candidate_packet: Mapping[str, Any],
) -> None:
    """Rebuild and strictly compare an assessment to its exact source packet."""

    actual = _detached_json_object(assessment, label="assessment")
    if set(actual) != _OUTPUT_FIELDS:
        raise EvidenceVaultSemanticScoringV3Error("assessment fields mismatch")
    expected = build_evidence_vault_semantic_assessment(source_candidate_packet=source_candidate_packet)
    try:
        if canonical_json(actual) != canonical_json(expected):
            raise EvidenceVaultSemanticScoringV3Error("assessment does not match its exact source packet")
        if actual["availability"] == "available":
            validate_sv9_assessment_output(actual["assessment_output"])
    except (EvidenceVaultCanonicalCoreError, Sv9AssessmentError) as exc:
        raise EvidenceVaultSemanticScoringV3Error("assessment is not a valid strict semantic snapshot") from exc
