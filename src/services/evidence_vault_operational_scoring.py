"""Deterministic scoring for adopted operational-memory v2 projections."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from src.services.evidence_vault_candidate_resolver import (
    build_canonical_aggregation_policy,
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    EvidenceVaultCanonicalCoreError,
    TileState,
    build_tile_contract_registry,
    canonical_fingerprint,
    reduce_tile_basis,
    reducer_policy_fingerprint,
    tile_contract_registry_fingerprint,
)
from src.services.evidence_vault_canonical_scoring import (
    EvidenceVaultCanonicalScoringError,
    calculate_score_from_tile_states,
)
from src.services.evidence_vault_operational_authority import (
    EVIDENCE_VAULT_OPERATIONAL_MEMORY_PROJECTION_VERSION,
)
from src.services.evidence_vault_operational_memory import (
    EVIDENCE_VAULT_ACCEPTED_MEMORY_VERSION,
    EVIDENCE_VAULT_OPERATIONAL_DERIVED_TILE_STATE_VERSION,
)
from src.sv9.rubric import BASE_COMPONENTS, COMPONENTS, PRESENTATION_ORDER, RUBRIC_VERSION


EVIDENCE_VAULT_OPERATIONAL_SCORE_EVALUATION_VERSION = (
    "evidence-vault-operational-score-evaluation-v2"
)
EVIDENCE_VAULT_OPERATIONAL_SCORE_INPUT_VERSION = (
    "evidence-vault-operational-score-input-v2"
)
EVIDENCE_VAULT_OPERATIONAL_EVALUATION_IDENTITY_VERSION = (
    "evidence-vault-operational-evaluation-identity-v2"
)
_EVALUATION_FIELDS = frozenset(
    {
        "schema_version",
        "evaluation_identity",
        "canonical_memory_version",
        "adoption_event_id",
        "score_input_fingerprint",
        "derived_tile_state_fingerprint",
        "rubric_version",
        "tile_contract_registry_fingerprint",
        "reducer_policy_fingerprint",
        "aggregation_policy_fingerprint",
        "score",
        "component_breakdown",
        "base_average",
        "magnetism_capped",
        "authority_coverage",
        "reused_from_evaluation_identity",
        "created_at",
        "authority",
        "authority_scope",
        "production_runtime_effect",
        "scanner_runtime_effect",
    }
)
class EvidenceVaultOperationalScoringError(ValueError):
    """The adopted memory cannot be scored deterministically."""


def build_operational_score_evaluation(
    canonical_memory: Mapping[str, Any],
    *,
    created_at: str,
    reusable_evaluation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    tile_states, coverage, policies = _scoring_inputs(canonical_memory)
    derived_tile_state_fingerprint = canonical_fingerprint(
        EVIDENCE_VAULT_OPERATIONAL_DERIVED_TILE_STATE_VERSION,
        [
            {"tile_id": row["tile_id"], "state": row["state"]}
            for row in tile_states
        ],
    )
    score_input_fingerprint = canonical_fingerprint(
        EVIDENCE_VAULT_OPERATIONAL_SCORE_INPUT_VERSION,
        {
            "derived_tile_state_fingerprint": derived_tile_state_fingerprint,
            **policies,
        },
    )
    canonical_memory_version = _sha256(
        canonical_memory.get("canonical_memory_version"),
        field="canonical_memory_version",
    )
    evaluation_identity = canonical_fingerprint(
        EVIDENCE_VAULT_OPERATIONAL_EVALUATION_IDENTITY_VERSION,
        {
            "canonical_memory_version": canonical_memory_version,
            "score_input_fingerprint": score_input_fingerprint,
        },
    )

    expected_calculation = calculate_score_from_tile_states(tile_states)
    reused_from: str | None = None
    if reusable_evaluation is None:
        calculation = expected_calculation
    else:
        validate_operational_score_evaluation(reusable_evaluation)
        if reusable_evaluation["score_input_fingerprint"] != score_input_fingerprint:
            raise EvidenceVaultOperationalScoringError(
                "reusable evaluation has different effective scoring inputs"
            )
        if reusable_evaluation["evaluation_identity"] == evaluation_identity:
            raise EvidenceVaultOperationalScoringError(
                "an evaluation cannot reuse its own identity"
            )
        calculation = {
            "score": reusable_evaluation["score"],
            "component_breakdown": reusable_evaluation["component_breakdown"],
            "base_average": reusable_evaluation["base_average"],
            "magnetism_capped": reusable_evaluation["magnetism_capped"],
        }
        if calculation != expected_calculation:
            raise EvidenceVaultOperationalScoringError(
                "reusable evaluation calculation does not match current tile states"
            )
        reused_from = str(reusable_evaluation["evaluation_identity"])

    evaluation = {
        "schema_version": EVIDENCE_VAULT_OPERATIONAL_SCORE_EVALUATION_VERSION,
        "evaluation_identity": evaluation_identity,
        "canonical_memory_version": canonical_memory_version,
        "adoption_event_id": str(canonical_memory.get("adoption_event_id") or ""),
        "score_input_fingerprint": score_input_fingerprint,
        "derived_tile_state_fingerprint": derived_tile_state_fingerprint,
        "rubric_version": RUBRIC_VERSION,
        **policies,
        **calculation,
        "authority_coverage": dict(coverage),
        "reused_from_evaluation_identity": reused_from,
        "created_at": _timestamp(created_at),
        "authority": True,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }
    validate_operational_score_evaluation(evaluation)
    return evaluation


def validate_operational_score_evaluation(evaluation: Mapping[str, Any]) -> None:
    if not isinstance(evaluation, Mapping):
        raise EvidenceVaultOperationalScoringError("evaluation must be an object")
    if set(evaluation) != _EVALUATION_FIELDS:
        raise EvidenceVaultOperationalScoringError("evaluation schema fields mismatch")
    if evaluation.get("schema_version") != EVIDENCE_VAULT_OPERATIONAL_SCORE_EVALUATION_VERSION:
        raise EvidenceVaultOperationalScoringError("unsupported evaluation schema")
    if (
        evaluation.get("authority") is not True
        or evaluation.get("authority_scope") != "b3s-vault"
        or evaluation.get("production_runtime_effect") is not False
        or evaluation.get("scanner_runtime_effect") is not False
    ):
        raise EvidenceVaultOperationalScoringError("invalid scoring authority")
    canonical_memory_version = _sha256(
        evaluation.get("canonical_memory_version"),
        field="canonical_memory_version",
    )
    try:
        UUID(str(evaluation.get("adoption_event_id") or ""))
    except (TypeError, ValueError, AttributeError) as exc:
        raise EvidenceVaultOperationalScoringError(
            "adoption_event_id must be a UUID"
        ) from exc
    score_input_fingerprint = _sha256(
        evaluation.get("score_input_fingerprint"),
        field="score_input_fingerprint",
    )
    expected_identity = canonical_fingerprint(
        EVIDENCE_VAULT_OPERATIONAL_EVALUATION_IDENTITY_VERSION,
        {
            "canonical_memory_version": canonical_memory_version,
            "score_input_fingerprint": score_input_fingerprint,
        },
    )
    if evaluation.get("evaluation_identity") != expected_identity:
        raise EvidenceVaultOperationalScoringError("evaluation identity mismatch")
    policies = {
        "tile_contract_registry_fingerprint": _sha256(
            evaluation.get("tile_contract_registry_fingerprint"),
            field="tile_contract_registry_fingerprint",
        ),
        "reducer_policy_fingerprint": _sha256(
            evaluation.get("reducer_policy_fingerprint"),
            field="reducer_policy_fingerprint",
        ),
        "aggregation_policy_fingerprint": _sha256(
            evaluation.get("aggregation_policy_fingerprint"),
            field="aggregation_policy_fingerprint",
        ),
    }
    expected_input = canonical_fingerprint(
        EVIDENCE_VAULT_OPERATIONAL_SCORE_INPUT_VERSION,
        {
            "derived_tile_state_fingerprint": _sha256(
                evaluation.get("derived_tile_state_fingerprint"),
                field="derived_tile_state_fingerprint",
            ),
            **policies,
        },
    )
    if expected_input != score_input_fingerprint:
        raise EvidenceVaultOperationalScoringError("score input fingerprint mismatch")
    if policies["tile_contract_registry_fingerprint"] != tile_contract_registry_fingerprint():
        raise EvidenceVaultOperationalScoringError("tile registry mismatch")
    if policies["reducer_policy_fingerprint"] != reducer_policy_fingerprint():
        raise EvidenceVaultOperationalScoringError("reducer policy mismatch")
    if policies["aggregation_policy_fingerprint"] != canonical_aggregation_policy_fingerprint():
        raise EvidenceVaultOperationalScoringError("aggregation policy mismatch")
    if evaluation.get("rubric_version") != RUBRIC_VERSION:
        raise EvidenceVaultOperationalScoringError("rubric version mismatch")
    _validate_authority_coverage(evaluation.get("authority_coverage"))
    _validate_score_breakdown(evaluation)
    _validate_coverage_matches_breakdown(evaluation)
    reused_from = evaluation.get("reused_from_evaluation_identity")
    if reused_from is not None:
        if _sha256(reused_from, field="reused_from_evaluation_identity") == evaluation[
            "evaluation_identity"
        ]:
            raise EvidenceVaultOperationalScoringError("evaluation cannot reuse itself")
    _timestamp(evaluation.get("created_at"))


def _validate_authority_coverage(value: Any) -> None:
    fields = frozenset(
        {
            "tile_count",
            "accepted_tile_count",
            "accepted_ok_count",
            "accepted_no_count",
            "accepted_sin_evidencia_count",
            "unresolved_tile_count",
            "pending_initial_tile_count",
            "pending_change_tile_count",
            "contradiction_on_accepted_count",
            "contradiction_on_unresolved_count",
            "contradiction_count",
            "tile_authority_coverage_ratio",
            "score_weight_authority_coverage_ratio",
            "score_completeness",
            "canonical_score_status",
        }
    )
    if not isinstance(value, Mapping) or set(value) != fields:
        raise EvidenceVaultOperationalScoringError(
            "authority coverage schema mismatch"
        )
    integer_fields = fields - {
        "tile_authority_coverage_ratio",
        "score_weight_authority_coverage_ratio",
        "score_completeness",
        "canonical_score_status",
    }
    if any(
        not isinstance(value[field], int)
        or isinstance(value[field], bool)
        or value[field] < 0
        for field in integer_fields
    ):
        raise EvidenceVaultOperationalScoringError(
            "authority coverage counts are invalid"
        )
    if value["tile_count"] != 80:
        raise EvidenceVaultOperationalScoringError("authority tile count mismatch")
    accepted = value["accepted_tile_count"]
    unresolved = value["unresolved_tile_count"]
    if accepted + unresolved != 80:
        raise EvidenceVaultOperationalScoringError(
            "authority coverage cardinality mismatch"
        )
    if (
        value["accepted_ok_count"]
        + value["accepted_no_count"]
        + value["accepted_sin_evidencia_count"]
        != accepted
    ):
        raise EvidenceVaultOperationalScoringError(
            "accepted authority counts are inconsistent"
        )
    if value["pending_change_tile_count"] > unresolved:
        raise EvidenceVaultOperationalScoringError(
            "pending changes exceed unresolved authority"
        )
    if value["pending_initial_tile_count"] > unresolved:
        raise EvidenceVaultOperationalScoringError(
            "pending initial tiles exceed unresolved authority"
        )
    if value["contradiction_count"] != (
        value["contradiction_on_accepted_count"]
        + value["contradiction_on_unresolved_count"]
    ):
        raise EvidenceVaultOperationalScoringError(
            "contradiction cardinality mismatch"
        )
    for field in (
        "tile_authority_coverage_ratio",
        "score_weight_authority_coverage_ratio",
    ):
        ratio = value[field]
        if (
            not isinstance(ratio, (int, float))
            or isinstance(ratio, bool)
            or not 0 <= float(ratio) <= 1
        ):
            raise EvidenceVaultOperationalScoringError(
                f"invalid authority coverage ratio: {field}"
            )
    expected_ratio = round(accepted / 80, 6)
    if float(value["tile_authority_coverage_ratio"]) != expected_ratio:
        raise EvidenceVaultOperationalScoringError(
            "tile authority coverage ratio mismatch"
        )
    expected_completeness = "complete" if accepted == 80 else "partial"
    if value["score_completeness"] != expected_completeness:
        raise EvidenceVaultOperationalScoringError("score completeness mismatch")
    expected_status = (
        "pending_reassessment"
        if value["pending_change_tile_count"]
        else "current"
    )
    if value["canonical_score_status"] != expected_status:
        raise EvidenceVaultOperationalScoringError(
            "canonical score status is invalid"
        )


def _validate_score_breakdown(evaluation: Mapping[str, Any]) -> None:
    breakdown = evaluation.get("component_breakdown")
    expected_fields = {
        "component_key",
        "tile_count",
        "ok_count",
        "no_count",
        "sin_evidencia_count",
        "raw_score",
        "effective_score",
        "multiplier",
        "points",
        "max_points",
    }
    if not isinstance(breakdown, list) or len(breakdown) != len(PRESENTATION_ORDER):
        raise EvidenceVaultOperationalScoringError("component breakdown is invalid")
    for position, component_key in enumerate(PRESENTATION_ORDER):
        row = breakdown[position]
        if not isinstance(row, Mapping) or set(row) != expected_fields:
            raise EvidenceVaultOperationalScoringError(
                "component breakdown fields mismatch"
            )
        tile_count = len(COMPONENTS[component_key]["tiles"])
        multiplier = int(COMPONENTS[component_key]["multiplier"])
        integer_values = [
            row[field]
            for field in (
                "tile_count",
                "raw_score",
                "effective_score",
                "multiplier",
                "points",
                "max_points",
            )
        ]
        if any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in integer_values
        ):
            raise EvidenceVaultOperationalScoringError(
                "component numeric fields must be exact integers"
            )
        if (
            row["component_key"] != component_key
            or row["tile_count"] != tile_count
            or row["multiplier"] != multiplier
            or row["max_points"] != tile_count * multiplier
        ):
            raise EvidenceVaultOperationalScoringError(
                "component breakdown metadata mismatch"
            )
        counts = [row["ok_count"], row["no_count"], row["sin_evidencia_count"]]
        if any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0
            for count in counts
        ):
            raise EvidenceVaultOperationalScoringError(
                "component state counts are invalid"
            )
        if sum(counts) != tile_count or row["raw_score"] != row["ok_count"]:
            raise EvidenceVaultOperationalScoringError(
                "component state counts are inconsistent"
            )
    base_average = evaluation.get("base_average")
    if not isinstance(base_average, (int, float)) or isinstance(base_average, bool):
        raise EvidenceVaultOperationalScoringError("base average is invalid")
    expected_average = round(
        sum(
            breakdown[PRESENTATION_ORDER.index(component)]["raw_score"]
            * (10 / int(COMPONENTS[component]["scale"]))
            for component in BASE_COMPONENTS
        )
        / len(BASE_COMPONENTS),
        2,
    )
    if float(base_average) != expected_average:
        raise EvidenceVaultOperationalScoringError("base average is inconsistent")
    policy = build_canonical_aggregation_policy()["magnetism_cap"]
    magnetism = breakdown[PRESENTATION_ORDER.index("magnetism")]
    expected_capped = (
        expected_average < float(policy["base_average_below"])
        and magnetism["raw_score"] > int(policy["maximum_lit_tiles"])
    )
    if evaluation.get("magnetism_capped") is not expected_capped:
        raise EvidenceVaultOperationalScoringError("magnetism cap is inconsistent")
    for row in breakdown:
        expected_effective = (
            int(policy["maximum_lit_tiles"])
            if row["component_key"] == "magnetism" and expected_capped
            else row["raw_score"]
        )
        if (
            row["effective_score"] != expected_effective
            or row["points"] != expected_effective * row["multiplier"]
        ):
            raise EvidenceVaultOperationalScoringError(
                "component effective points are inconsistent"
            )
    score = evaluation.get("score")
    if (
        not isinstance(score, int)
        or isinstance(score, bool)
        or not 0 <= score <= 100
        or score != sum(row["points"] for row in breakdown)
    ):
        raise EvidenceVaultOperationalScoringError("component total is inconsistent")


def _validate_coverage_matches_breakdown(evaluation: Mapping[str, Any]) -> None:
    coverage = evaluation["authority_coverage"]
    breakdown = evaluation["component_breakdown"]
    ok_count = sum(row["ok_count"] for row in breakdown)
    no_count = sum(row["no_count"] for row in breakdown)
    sin_count = sum(row["sin_evidencia_count"] for row in breakdown)
    if (
        ok_count != coverage["accepted_ok_count"]
        or no_count != coverage["accepted_no_count"]
        or sin_count
        != coverage["accepted_sin_evidencia_count"]
        + coverage["unresolved_tile_count"]
    ):
        raise EvidenceVaultOperationalScoringError(
            "authority coverage does not match scored tile states"
        )


def _scoring_inputs(
    memory: Mapping[str, Any],
) -> tuple[list[dict[str, str]], Mapping[str, Any], dict[str, str]]:
    """Derive every scoring input from accepted memory, never from a stored projection."""

    if not isinstance(memory, Mapping):
        raise EvidenceVaultOperationalScoringError("canonical memory must be an object")
    if memory.get("schema_version") != EVIDENCE_VAULT_OPERATIONAL_MEMORY_PROJECTION_VERSION:
        raise EvidenceVaultOperationalScoringError("unsupported memory projection")
    if (
        memory.get("authority") is not True
        or memory.get("authority_scope") != "b3s-vault"
        or memory.get("production_runtime_effect") is not False
        or memory.get("scanner_runtime_effect") is not False
        or memory.get("lifecycle_state") != "active"
    ):
        raise EvidenceVaultOperationalScoringError(
            "scoring requires active adopted Vault memory"
        )
    try:
        UUID(str(memory.get("adoption_event_id") or ""))
    except (TypeError, ValueError, AttributeError) as exc:
        raise EvidenceVaultOperationalScoringError(
            "adoption_event_id must be a UUID"
        ) from exc
    sequence = memory.get("adoption_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise EvidenceVaultOperationalScoringError("adoption sequence is invalid")

    content = memory.get("content")
    if (
        not isinstance(content, Mapping)
        or content.get("schema_version") != EVIDENCE_VAULT_ACCEPTED_MEMORY_VERSION
    ):
        raise EvidenceVaultOperationalScoringError("accepted memory content is invalid")
    expected_version = canonical_fingerprint(
        EVIDENCE_VAULT_ACCEPTED_MEMORY_VERSION,
        content,
    )
    if memory.get("canonical_memory_version") != expected_version:
        raise EvidenceVaultOperationalScoringError("canonical memory fingerprint mismatch")
    if memory.get("brand_identity") != content.get("brand_identity"):
        raise EvidenceVaultOperationalScoringError("canonical memory brand mismatch")

    policies = {
        "tile_contract_registry_fingerprint": _sha256(
            content.get("tile_contract_registry_fingerprint"),
            field="tile_contract_registry_fingerprint",
        ),
        "reducer_policy_fingerprint": _sha256(
            content.get("reducer_policy_fingerprint"),
            field="reducer_policy_fingerprint",
        ),
        "aggregation_policy_fingerprint": _sha256(
            content.get("aggregation_policy_fingerprint"),
            field="aggregation_policy_fingerprint",
        ),
    }
    if policies["tile_contract_registry_fingerprint"] != tile_contract_registry_fingerprint():
        raise EvidenceVaultOperationalScoringError("tile registry mismatch")
    if policies["reducer_policy_fingerprint"] != reducer_policy_fingerprint():
        raise EvidenceVaultOperationalScoringError("reducer policy mismatch")
    if policies["aggregation_policy_fingerprint"] != canonical_aggregation_policy_fingerprint():
        raise EvidenceVaultOperationalScoringError("aggregation policy mismatch")

    accepted_rows = content.get("accepted_tiles")
    if not isinstance(accepted_rows, list):
        raise EvidenceVaultOperationalScoringError("accepted tiles must be an array")
    registry_rows = list(build_tile_contract_registry()["tiles"])
    registry_by_id = {str(row["tile_id"]): row for row in registry_rows}
    accepted_by_id: dict[str, Mapping[str, Any]] = {}
    state_counts = {
        TileState.OK.value: 0,
        TileState.NO.value: 0,
        TileState.SIN_EVIDENCIA.value: 0,
    }
    for row in accepted_rows:
        if not isinstance(row, Mapping):
            raise EvidenceVaultOperationalScoringError("accepted tile must be an object")
        tile_id = str(row.get("tile_id") or "")
        contract = registry_by_id.get(tile_id)
        if contract is None or tile_id in accepted_by_id:
            raise EvidenceVaultOperationalScoringError(
                "accepted memory has an unknown or duplicate tile"
            )
        if (
            str(row.get("component_key") or "") != str(contract["component_key"])
            or str(row.get("tile_key") or "") != str(contract["tile_key"])
        ):
            raise EvidenceVaultOperationalScoringError("accepted tile contract mismatch")
        state = str(row.get("semantic_state") or "")
        if state not in state_counts:
            raise EvidenceVaultOperationalScoringError("accepted tile state is invalid")
        basis = row.get("basis")
        if not isinstance(basis, list):
            raise EvidenceVaultOperationalScoringError("accepted tile basis must be an array")
        try:
            reduction = reduce_tile_basis(
                basis,
                allowed_absence_contract_ids=contract["absence_test_contract_ids"],
            )
        except EvidenceVaultCanonicalCoreError as exc:
            raise EvidenceVaultOperationalScoringError(
                f"accepted tile {tile_id} failed canonical reduction"
            ) from exc
        if reduction["state"] != state:
            raise EvidenceVaultOperationalScoringError(
                f"accepted tile {tile_id} does not match its basis"
            )
        _sha256(
            row.get("source_candidate_packet_fingerprint"),
            field=f"{tile_id}.source_candidate_packet_fingerprint",
        )
        accepted_by_id[tile_id] = row
        state_counts[state] += 1

    pending_rows = content.get("pending_reassessments") or []
    if not isinstance(pending_rows, list):
        raise EvidenceVaultOperationalScoringError(
            "pending reassessments must be an array"
        )
    pending_reassessment_ids: set[str] = set()
    for row in pending_rows:
        if not isinstance(row, Mapping) or set(row) != {
            "tile_id",
            "lifecycle_state",
            "reopen_policy_fingerprint",
            "prior_group_id",
            "trigger_fingerprint",
            "superseded_member_evidence_fingerprints",
        }:
            raise EvidenceVaultOperationalScoringError(
                "pending reassessment fields mismatch"
            )
        tile_id = str(row.get("tile_id") or "")
        if (
            tile_id not in registry_by_id
            or tile_id in accepted_by_id
            or tile_id in pending_reassessment_ids
            or row.get("lifecycle_state") != "pending_reassessment"
        ):
            raise EvidenceVaultOperationalScoringError(
                "pending reassessment tile is invalid"
            )
        for field in (
            "reopen_policy_fingerprint",
            "prior_group_id",
            "trigger_fingerprint",
        ):
            _sha256(
                row.get(field),
                field=f"{tile_id}.{field}",
            )
        superseded = row.get("superseded_member_evidence_fingerprints")
        if (
            not isinstance(superseded, list)
            or not superseded
            or superseded != sorted(set(superseded))
        ):
            raise EvidenceVaultOperationalScoringError(
                "pending reassessment superseded evidence is invalid"
            )
        for fingerprint in superseded:
            _sha256(
                fingerprint,
                field=f"{tile_id}.superseded_member_evidence_fingerprint",
            )
        pending_reassessment_ids.add(tile_id)

    tile_states: list[dict[str, str]] = []
    for contract in registry_rows:
        tile_id = str(contract["tile_id"])
        accepted = accepted_by_id.get(tile_id)
        state = (
            str(accepted["semantic_state"])
            if accepted is not None
            else TileState.SIN_EVIDENCIA.value
        )
        tile_states.append(
            {
                "component_key": str(contract["component_key"]),
                "tile_id": tile_id,
                "tile_key": str(contract["tile_key"]),
                "state": state,
            }
        )

    accepted_count = len(accepted_by_id)
    accepted_weight = sum(
        int(COMPONENTS[str(registry_by_id[tile_id]["component_key"])]["multiplier"])
        for tile_id in accepted_by_id
    )
    total_weight = sum(
        int(COMPONENTS[str(row["component_key"])]["multiplier"])
        for row in registry_rows
    )
    coverage = {
        "tile_count": len(registry_rows),
        "accepted_tile_count": accepted_count,
        "accepted_ok_count": state_counts[TileState.OK.value],
        "accepted_no_count": state_counts[TileState.NO.value],
        "accepted_sin_evidencia_count": state_counts[TileState.SIN_EVIDENCIA.value],
        "unresolved_tile_count": len(registry_rows) - accepted_count,
        "pending_initial_tile_count": 0,
        "pending_change_tile_count": len(pending_reassessment_ids),
        "contradiction_on_accepted_count": 0,
        "contradiction_on_unresolved_count": 0,
        "contradiction_count": 0,
        "tile_authority_coverage_ratio": round(accepted_count / len(registry_rows), 6),
        "score_weight_authority_coverage_ratio": round(
            accepted_weight / total_weight,
            6,
        ),
        "score_completeness": (
            "complete" if accepted_count == len(registry_rows) else "partial"
        ),
        "canonical_score_status": (
            "pending_reassessment"
            if pending_reassessment_ids
            else "current"
        ),
    }
    return tile_states, coverage, policies


def _timestamp(value: Any) -> str:
    normalized = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceVaultOperationalScoringError("invalid created_at") from exc
    if parsed.tzinfo is None:
        raise EvidenceVaultOperationalScoringError("created_at requires timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _sha256(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise EvidenceVaultOperationalScoringError(f"{field} must be a sha256")
    return normalized


__all__ = [
    "EVIDENCE_VAULT_OPERATIONAL_DERIVED_TILE_STATE_VERSION",
    "EVIDENCE_VAULT_OPERATIONAL_EVALUATION_IDENTITY_VERSION",
    "EVIDENCE_VAULT_OPERATIONAL_SCORE_EVALUATION_VERSION",
    "EVIDENCE_VAULT_OPERATIONAL_SCORE_INPUT_VERSION",
    "EvidenceVaultOperationalScoringError",
    "build_operational_score_evaluation",
    "validate_operational_score_evaluation",
]
