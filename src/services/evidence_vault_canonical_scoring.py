"""Deterministic scoring for promoted, Vault-only canonical memory.

The scorer never reads scanner output or pending candidate packets.  Its only
input is the authoritative canonical-memory projection rebuilt from the
append-only promotion journal.  It therefore calculates a score without
changing evidence, tile state, scanner output, or production behavior.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from src.services.evidence_vault_candidate_resolver import (
    build_canonical_aggregation_policy,
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_authority import (
    AUTHORITY_SCOPE,
    EVIDENCE_VAULT_CANONICAL_MEMORY_CONTENT_VERSION,
    EVIDENCE_VAULT_CANONICAL_MEMORY_PROJECTION_VERSION,
)
from src.services.evidence_vault_canonical_core import (
    EVIDENCE_VAULT_DERIVED_TILE_STATE_VERSION,
    TileState,
    build_tile_contract_registry,
    canonical_fingerprint,
    reducer_policy_fingerprint,
    tile_contract_registry_fingerprint,
)
from src.sv9.rubric import BASE_COMPONENTS, COMPONENTS, PRESENTATION_ORDER, RUBRIC_VERSION


EVIDENCE_VAULT_CANONICAL_SCORE_EVALUATION_VERSION = (
    "evidence-vault-canonical-score-evaluation-v1"
)
EVIDENCE_VAULT_SCORE_INPUT_VERSION = "evidence-vault-canonical-score-input-v1"
EVIDENCE_VAULT_EVALUATION_IDENTITY_VERSION = (
    "evidence-vault-canonical-evaluation-identity-v1"
)

_CANONICAL_MEMORY_FIELDS = frozenset(
    {
        "schema_version",
        "canonical_memory_version",
        "promotion_event_id",
        "promotion_sequence",
        "authority",
        "authority_scope",
        "production_runtime_effect",
        "scanner_runtime_effect",
        "content",
    }
)
_CANONICAL_CONTENT_FIELDS = frozenset(
    {
        "schema_version",
        "brand_identity",
        "parent_canonical_memory_version",
        "candidate_memory_version",
        "accepted_memory_candidate_version",
        "reviewed_memory_candidate_version",
        "review_packet_set_fingerprint",
        "rubric_version",
        "tile_contract_registry_fingerprint",
        "reducer_policy_fingerprint",
        "aggregation_policy_fingerprint",
        "tile_count",
        "tiles",
    }
)
_CANONICAL_TILE_FIELDS = frozenset(
    {
        "component_key",
        "tile_id",
        "tile_key",
        "state",
        "basis",
        "provenance_status",
        "single_source_dependency",
        "coverage_refs",
        "unresolved_refs",
        "source_candidate_packet_fingerprint",
        "source_delta_kind",
    }
)
_EVALUATION_FIELDS = frozenset(
    {
        "schema_version",
        "evaluation_identity",
        "canonical_memory_version",
        "promotion_event_id",
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
        "reused_from_evaluation_identity",
        "created_at",
        "authority",
        "authority_scope",
        "production_runtime_effect",
        "scanner_runtime_effect",
    }
)
_COMPONENT_BREAKDOWN_FIELDS = frozenset(
    {
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
)


class EvidenceVaultCanonicalScoringError(ValueError):
    """Canonical memory or a persisted evaluation cannot be scored safely."""


def build_canonical_score_evaluation(
    canonical_memory: dict[str, Any],
    *,
    created_at: str,
    reusable_evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate one immutable evaluation for an authoritative memory version.

    ``reusable_evaluation`` is only a calculation cache.  A strengthened
    canonical memory still receives a new ``evaluation_identity`` even when
    its effective tile states, policy inputs, and numeric result are unchanged.
    """

    tile_states = _canonical_tile_states(canonical_memory)
    content = canonical_memory["content"]
    derived_fingerprint = _derived_tile_state_fingerprint(tile_states)
    score_input_fingerprint = canonical_fingerprint(
        EVIDENCE_VAULT_SCORE_INPUT_VERSION,
        {
            "derived_tile_state_fingerprint": derived_fingerprint,
            "tile_contract_registry_fingerprint": content[
                "tile_contract_registry_fingerprint"
            ],
            "reducer_policy_fingerprint": content["reducer_policy_fingerprint"],
            "aggregation_policy_fingerprint": content[
                "aggregation_policy_fingerprint"
            ],
        },
    )
    evaluation_identity = canonical_fingerprint(
        EVIDENCE_VAULT_EVALUATION_IDENTITY_VERSION,
        {
            "canonical_memory_version": canonical_memory[
                "canonical_memory_version"
            ],
            "score_input_fingerprint": score_input_fingerprint,
        },
    )

    reused_from: str | None = None
    if reusable_evaluation is None:
        calculation = _calculate(tile_states)
    else:
        validate_canonical_score_evaluation(reusable_evaluation)
        if reusable_evaluation["score_input_fingerprint"] != score_input_fingerprint:
            raise EvidenceVaultCanonicalScoringError(
                "reusable evaluation has different effective scoring inputs"
            )
        if reusable_evaluation["evaluation_identity"] == evaluation_identity:
            raise EvidenceVaultCanonicalScoringError(
                "an evaluation cannot reuse its own identity"
            )
        calculation = {
            "score": reusable_evaluation["score"],
            "component_breakdown": reusable_evaluation["component_breakdown"],
            "base_average": reusable_evaluation["base_average"],
            "magnetism_capped": reusable_evaluation["magnetism_capped"],
        }
        reused_from = reusable_evaluation["evaluation_identity"]

    evaluation = {
        "schema_version": EVIDENCE_VAULT_CANONICAL_SCORE_EVALUATION_VERSION,
        "evaluation_identity": evaluation_identity,
        "canonical_memory_version": canonical_memory["canonical_memory_version"],
        "promotion_event_id": canonical_memory["promotion_event_id"],
        "score_input_fingerprint": score_input_fingerprint,
        "derived_tile_state_fingerprint": derived_fingerprint,
        "rubric_version": content["rubric_version"],
        "tile_contract_registry_fingerprint": content[
            "tile_contract_registry_fingerprint"
        ],
        "reducer_policy_fingerprint": content["reducer_policy_fingerprint"],
        "aggregation_policy_fingerprint": content[
            "aggregation_policy_fingerprint"
        ],
        **calculation,
        "reused_from_evaluation_identity": reused_from,
        "created_at": _timestamp(created_at),
        "authority": True,
        "authority_scope": AUTHORITY_SCOPE,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }
    validate_canonical_score_evaluation(evaluation)
    return evaluation


def calculate_score_from_tile_states(
    tile_states: list[dict[str, str]],
) -> dict[str, Any]:
    """Calculate SV9 from one complete, authority-filtered tile projection.

    This is the shared deterministic calculation boundary.  Callers remain
    responsible for proving authority; this function only validates the exact
    80-tile registry and performs the versioned arithmetic.
    """

    registry = build_tile_contract_registry()["tiles"]
    expected = {
        str(row["tile_id"]): {
            "component_key": str(row["component_key"]),
            "tile_key": str(row["tile_key"]),
        }
        for row in registry
    }
    if not isinstance(tile_states, list) or len(tile_states) != len(registry):
        raise EvidenceVaultCanonicalScoringError(
            "deterministic scoring requires exactly 80 tile states"
        )
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in tile_states:
        if not isinstance(row, dict):
            raise EvidenceVaultCanonicalScoringError("tile state must be an object")
        tile_id = str(row.get("tile_id") or "")
        contract = expected.get(tile_id)
        if contract is None or tile_id in seen:
            raise EvidenceVaultCanonicalScoringError(
                f"unknown or duplicate scoring tile: {tile_id}"
            )
        seen.add(tile_id)
        state = str(row.get("state") or "")
        if state not in {
            TileState.OK.value,
            TileState.NO.value,
            TileState.SIN_EVIDENCIA.value,
        }:
            raise EvidenceVaultCanonicalScoringError(
                f"invalid effective scoring state for {tile_id}"
            )
        component_key = str(row.get("component_key") or "")
        tile_key = str(row.get("tile_key") or "")
        if component_key != contract["component_key"] or tile_key != contract["tile_key"]:
            raise EvidenceVaultCanonicalScoringError(
                f"scoring tile contract mismatch for {tile_id}"
            )
        normalized.append(
            {
                "component_key": component_key,
                "tile_id": tile_id,
                "tile_key": tile_key,
                "state": state,
            }
        )
    order = {str(row["tile_id"]): index for index, row in enumerate(registry)}
    normalized.sort(key=lambda row: order[row["tile_id"]])
    return _calculate(normalized)


def validate_canonical_score_evaluation(evaluation: dict[str, Any]) -> None:
    """Reject altered, incompatible, or arithmetically inconsistent results."""

    if not isinstance(evaluation, dict) or set(evaluation) != _EVALUATION_FIELDS:
        raise EvidenceVaultCanonicalScoringError(
            "canonical score evaluation schema fields mismatch"
        )
    if (
        evaluation.get("schema_version")
        != EVIDENCE_VAULT_CANONICAL_SCORE_EVALUATION_VERSION
    ):
        raise EvidenceVaultCanonicalScoringError(
            "unsupported canonical score evaluation schema"
        )
    if (
        evaluation.get("authority") is not True
        or evaluation.get("authority_scope") != AUTHORITY_SCOPE
        or evaluation.get("production_runtime_effect") is not False
        or evaluation.get("scanner_runtime_effect") is not False
    ):
        raise EvidenceVaultCanonicalScoringError(
            "canonical score evaluation has invalid authority scope"
        )
    canonical_memory_version = _sha256(
        evaluation.get("canonical_memory_version"),
        field="canonical_memory_version",
    )
    score_input_fingerprint = _sha256(
        evaluation.get("score_input_fingerprint"),
        field="score_input_fingerprint",
    )
    expected_evaluation_identity = canonical_fingerprint(
        EVIDENCE_VAULT_EVALUATION_IDENTITY_VERSION,
        {
            "canonical_memory_version": canonical_memory_version,
            "score_input_fingerprint": score_input_fingerprint,
        },
    )
    if evaluation.get("evaluation_identity") != expected_evaluation_identity:
        raise EvidenceVaultCanonicalScoringError("evaluation identity mismatch")
    str(UUID(_text(evaluation.get("promotion_event_id"), field="promotion_event_id")))
    derived_fingerprint = _sha256(
        evaluation.get("derived_tile_state_fingerprint"),
        field="derived_tile_state_fingerprint",
    )
    registry_fingerprint = _sha256(
        evaluation.get("tile_contract_registry_fingerprint"),
        field="tile_contract_registry_fingerprint",
    )
    reducer_fingerprint = _sha256(
        evaluation.get("reducer_policy_fingerprint"),
        field="reducer_policy_fingerprint",
    )
    aggregation_fingerprint = _sha256(
        evaluation.get("aggregation_policy_fingerprint"),
        field="aggregation_policy_fingerprint",
    )
    expected_score_input = canonical_fingerprint(
        EVIDENCE_VAULT_SCORE_INPUT_VERSION,
        {
            "derived_tile_state_fingerprint": derived_fingerprint,
            "tile_contract_registry_fingerprint": registry_fingerprint,
            "reducer_policy_fingerprint": reducer_fingerprint,
            "aggregation_policy_fingerprint": aggregation_fingerprint,
        },
    )
    if score_input_fingerprint != expected_score_input:
        raise EvidenceVaultCanonicalScoringError("score input fingerprint mismatch")
    if evaluation.get("rubric_version") != RUBRIC_VERSION:
        raise EvidenceVaultCanonicalScoringError("canonical score rubric mismatch")
    if registry_fingerprint != tile_contract_registry_fingerprint():
        raise EvidenceVaultCanonicalScoringError(
            "canonical score tile contract registry mismatch"
        )
    if reducer_fingerprint != reducer_policy_fingerprint():
        raise EvidenceVaultCanonicalScoringError(
            "canonical score reducer policy mismatch"
        )
    if aggregation_fingerprint != canonical_aggregation_policy_fingerprint():
        raise EvidenceVaultCanonicalScoringError(
            "canonical score aggregation policy mismatch"
        )
    _timestamp(evaluation.get("created_at"))
    reused_from = evaluation.get("reused_from_evaluation_identity")
    if reused_from is not None:
        _sha256(reused_from, field="reused_from_evaluation_identity")
        if reused_from == evaluation["evaluation_identity"]:
            raise EvidenceVaultCanonicalScoringError(
                "evaluation cannot reference itself as reused"
            )
    _validate_breakdown(evaluation)


def _canonical_tile_states(canonical_memory: dict[str, Any]) -> list[dict[str, str]]:
    if not isinstance(canonical_memory, dict) or set(canonical_memory) != _CANONICAL_MEMORY_FIELDS:
        raise EvidenceVaultCanonicalScoringError(
            "canonical memory projection schema fields mismatch"
        )
    if (
        canonical_memory.get("schema_version")
        != EVIDENCE_VAULT_CANONICAL_MEMORY_PROJECTION_VERSION
        or canonical_memory.get("authority") is not True
        or canonical_memory.get("authority_scope") != AUTHORITY_SCOPE
        or canonical_memory.get("production_runtime_effect") is not False
        or canonical_memory.get("scanner_runtime_effect") is not False
    ):
        raise EvidenceVaultCanonicalScoringError(
            "canonical scoring requires promoted b3s-vault memory"
        )
    str(UUID(_text(canonical_memory.get("promotion_event_id"), field="promotion_event_id")))
    sequence = canonical_memory.get("promotion_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise EvidenceVaultCanonicalScoringError(
            "canonical memory promotion sequence is invalid"
        )
    content = canonical_memory.get("content")
    if not isinstance(content, dict) or set(content) != _CANONICAL_CONTENT_FIELDS:
        raise EvidenceVaultCanonicalScoringError(
            "canonical memory content schema fields mismatch"
        )
    if content.get("schema_version") != EVIDENCE_VAULT_CANONICAL_MEMORY_CONTENT_VERSION:
        raise EvidenceVaultCanonicalScoringError(
            "unsupported canonical memory content schema"
        )
    expected_version = canonical_fingerprint(
        EVIDENCE_VAULT_CANONICAL_MEMORY_CONTENT_VERSION,
        content,
    )
    if canonical_memory.get("canonical_memory_version") != expected_version:
        raise EvidenceVaultCanonicalScoringError(
            "canonical memory content fingerprint mismatch"
        )
    if content.get("rubric_version") != RUBRIC_VERSION:
        raise EvidenceVaultCanonicalScoringError("canonical memory rubric mismatch")
    if (
        content.get("tile_contract_registry_fingerprint")
        != tile_contract_registry_fingerprint()
    ):
        raise EvidenceVaultCanonicalScoringError(
            "canonical memory tile contract registry mismatch"
        )
    if content.get("reducer_policy_fingerprint") != reducer_policy_fingerprint():
        raise EvidenceVaultCanonicalScoringError(
            "canonical memory reducer policy mismatch"
        )
    if (
        content.get("aggregation_policy_fingerprint")
        != canonical_aggregation_policy_fingerprint()
    ):
        raise EvidenceVaultCanonicalScoringError(
            "canonical memory aggregation policy mismatch"
        )
    tiles = content.get("tiles")
    registry = build_tile_contract_registry()["tiles"]
    if (
        not isinstance(tiles, list)
        or content.get("tile_count") != 80
        or len(tiles) != 80
    ):
        raise EvidenceVaultCanonicalScoringError(
            "canonical scoring requires exactly 80 tiles"
        )
    expected_by_id = {str(row["tile_id"]): row for row in registry}
    states: list[dict[str, str]] = []
    seen: set[str] = set()
    for tile in tiles:
        if not isinstance(tile, dict) or set(tile) != _CANONICAL_TILE_FIELDS:
            raise EvidenceVaultCanonicalScoringError(
                "canonical tile schema fields mismatch"
            )
        tile_id = str(tile.get("tile_id") or "")
        expected = expected_by_id.get(tile_id)
        if expected is None or tile_id in seen:
            raise EvidenceVaultCanonicalScoringError(
                "canonical memory has duplicate or unknown tiles"
            )
        seen.add(tile_id)
        component_key = str(tile.get("component_key") or "")
        tile_key = str(tile.get("tile_key") or "")
        if (
            component_key != expected["component_key"]
            or tile_key != expected["tile_key"]
        ):
            raise EvidenceVaultCanonicalScoringError(
                "canonical tile identity does not match the registry"
            )
        state = str(tile.get("state") or "")
        if state == TileState.CONTRADICTION.value:
            raise EvidenceVaultCanonicalScoringError(
                "contradiction cannot enter canonical scoring"
            )
        if state not in {
            TileState.OK.value,
            TileState.NO.value,
            TileState.SIN_EVIDENCIA.value,
        }:
            raise EvidenceVaultCanonicalScoringError(
                "canonical tile state is invalid"
            )
        _sha256(
            tile.get("source_candidate_packet_fingerprint"),
            field="source_candidate_packet_fingerprint",
        )
        for field in ("basis", "coverage_refs", "unresolved_refs"):
            if not isinstance(tile.get(field), list):
                raise EvidenceVaultCanonicalScoringError(
                    f"canonical tile {field} must be an array"
                )
        states.append(
            {
                "component_key": component_key,
                "tile_id": tile_id,
                "tile_key": tile_key,
                "state": state,
            }
        )
    if set(expected_by_id) != seen:
        raise EvidenceVaultCanonicalScoringError(
            "canonical memory tile coverage is incomplete"
        )
    order = {str(row["tile_id"]): index for index, row in enumerate(registry)}
    return sorted(states, key=lambda row: order[row["tile_id"]])


def _derived_tile_state_fingerprint(tile_states: list[dict[str, str]]) -> str:
    return canonical_fingerprint(
        EVIDENCE_VAULT_DERIVED_TILE_STATE_VERSION,
        {
            "rubric_version": RUBRIC_VERSION,
            "tiles": tile_states,
        },
    )


def _calculate(tile_states: list[dict[str, str]]) -> dict[str, Any]:
    policy = build_canonical_aggregation_policy()
    states_by_component: dict[str, list[str]] = {
        component_key: [] for component_key in PRESENTATION_ORDER
    }
    for tile in tile_states:
        states_by_component[tile["component_key"]].append(tile["state"])

    raw_scores = {
        component_key: states_by_component[component_key].count(TileState.OK.value)
        for component_key in PRESENTATION_ORDER
    }
    base_average = round(
        sum(
            raw_scores[component_key]
            * (10 / int(COMPONENTS[component_key]["scale"]))
            for component_key in BASE_COMPONENTS
        )
        / len(BASE_COMPONENTS),
        2,
    )
    magnetism_policy = policy["magnetism_cap"]
    magnetism_capped = (
        base_average < float(magnetism_policy["base_average_below"])
        and raw_scores["magnetism"]
        > int(magnetism_policy["maximum_lit_tiles"])
    )

    breakdown: list[dict[str, Any]] = []
    for component_key in PRESENTATION_ORDER:
        states = states_by_component[component_key]
        raw_score = raw_scores[component_key]
        effective_score = (
            int(magnetism_policy["maximum_lit_tiles"])
            if component_key == "magnetism" and magnetism_capped
            else raw_score
        )
        multiplier = int(COMPONENTS[component_key]["multiplier"])
        breakdown.append(
            {
                "component_key": component_key,
                "tile_count": len(states),
                "ok_count": states.count(TileState.OK.value),
                "no_count": states.count(TileState.NO.value),
                "sin_evidencia_count": states.count(
                    TileState.SIN_EVIDENCIA.value
                ),
                "raw_score": raw_score,
                "effective_score": effective_score,
                "multiplier": multiplier,
                "points": effective_score * multiplier,
                "max_points": len(states) * multiplier,
            }
        )
    return {
        "score": sum(component["points"] for component in breakdown),
        "component_breakdown": breakdown,
        "base_average": base_average,
        "magnetism_capped": magnetism_capped,
    }


def _validate_breakdown(evaluation: dict[str, Any]) -> None:
    breakdown = evaluation.get("component_breakdown")
    if not isinstance(breakdown, list) or len(breakdown) != len(PRESENTATION_ORDER):
        raise EvidenceVaultCanonicalScoringError(
            "canonical score component breakdown is incomplete"
        )
    for position, component_key in enumerate(PRESENTATION_ORDER):
        row = breakdown[position]
        if not isinstance(row, dict) or set(row) != _COMPONENT_BREAKDOWN_FIELDS:
            raise EvidenceVaultCanonicalScoringError(
                "canonical score component breakdown fields mismatch"
            )
        expected_tile_count = len(COMPONENTS[component_key]["tiles"])
        expected_multiplier = int(COMPONENTS[component_key]["multiplier"])
        if (
            row.get("component_key") != component_key
            or row.get("tile_count") != expected_tile_count
            or row.get("multiplier") != expected_multiplier
            or row.get("max_points") != expected_tile_count * expected_multiplier
        ):
            raise EvidenceVaultCanonicalScoringError(
                "canonical score component metadata mismatch"
            )
        counts = [
            row.get("ok_count"),
            row.get("no_count"),
            row.get("sin_evidencia_count"),
        ]
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts):
            raise EvidenceVaultCanonicalScoringError(
                "canonical score component counts are invalid"
            )
        if sum(counts) != expected_tile_count or row.get("raw_score") != counts[0]:
            raise EvidenceVaultCanonicalScoringError(
                "canonical score component counts are inconsistent"
            )
        effective_score = row.get("effective_score")
        if (
            not isinstance(effective_score, int)
            or isinstance(effective_score, bool)
            or effective_score < 0
            or effective_score > row["raw_score"]
            or row.get("points") != effective_score * expected_multiplier
        ):
            raise EvidenceVaultCanonicalScoringError(
                "canonical score component points are inconsistent"
            )
    base_average = evaluation.get("base_average")
    if (
        not isinstance(base_average, (int, float))
        or isinstance(base_average, bool)
        or not 0 <= float(base_average) <= 10
    ):
        raise EvidenceVaultCanonicalScoringError(
            "canonical score base average is invalid"
        )
    expected_base_average = round(
        sum(
            breakdown[PRESENTATION_ORDER.index(component_key)]["raw_score"]
            * (10 / int(COMPONENTS[component_key]["scale"]))
            for component_key in BASE_COMPONENTS
        )
        / len(BASE_COMPONENTS),
        2,
    )
    if float(base_average) != expected_base_average:
        raise EvidenceVaultCanonicalScoringError(
            "canonical score base average is inconsistent"
        )
    magnetism_capped = evaluation.get("magnetism_capped")
    if not isinstance(magnetism_capped, bool):
        raise EvidenceVaultCanonicalScoringError(
            "canonical score magnetism cap flag is invalid"
        )
    policy = build_canonical_aggregation_policy()["magnetism_cap"]
    magnetism = breakdown[PRESENTATION_ORDER.index("magnetism")]
    expected_capped = (
        expected_base_average < float(policy["base_average_below"])
        and magnetism["raw_score"] > int(policy["maximum_lit_tiles"])
    )
    if magnetism_capped != expected_capped:
        raise EvidenceVaultCanonicalScoringError(
            "canonical score magnetism cap flag is inconsistent"
        )
    for row in breakdown:
        expected_effective = (
            int(policy["maximum_lit_tiles"])
            if row["component_key"] == "magnetism" and expected_capped
            else row["raw_score"]
        )
        if row["effective_score"] != expected_effective:
            raise EvidenceVaultCanonicalScoringError(
                "canonical score effective component score is inconsistent"
            )
    total = sum(row["points"] for row in breakdown)
    score = evaluation.get("score")
    if (
        not isinstance(score, int)
        or isinstance(score, bool)
        or score < 0
        or score > 100
        or score != total
    ):
        raise EvidenceVaultCanonicalScoringError(
            "canonical score total is inconsistent"
        )


def _timestamp(value: Any) -> str:
    text = _text(value, field="created_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceVaultCanonicalScoringError(
            "created_at must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceVaultCanonicalScoringError(
            "created_at must include a timezone"
        )
    return parsed.astimezone(timezone.utc).isoformat()


def _sha256(value: Any, *, field: str) -> str:
    text = _text(value, field=field).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise EvidenceVaultCanonicalScoringError(
            f"{field} must be a sha256 fingerprint"
        )
    return text


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise EvidenceVaultCanonicalScoringError(f"{field} must be text")
    text = value.strip()
    if not text or "\x00" in text:
        raise EvidenceVaultCanonicalScoringError(f"{field} is invalid")
    return text


__all__ = [
    "EVIDENCE_VAULT_CANONICAL_SCORE_EVALUATION_VERSION",
    "EVIDENCE_VAULT_EVALUATION_IDENTITY_VERSION",
    "EVIDENCE_VAULT_SCORE_INPUT_VERSION",
    "EvidenceVaultCanonicalScoringError",
    "build_canonical_score_evaluation",
    "calculate_score_from_tile_states",
    "validate_canonical_score_evaluation",
]
