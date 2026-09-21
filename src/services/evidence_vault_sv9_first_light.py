"""Pure, offline first-light rescan projection.

This module is deliberately not wired to Vault persistence, HTTP, providers,
authority application, or publication.  It consumes signed SV9 judgments and
frozen evidence identities, then produces a non-authoritative candidate view.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from src.sv9 import assessment_kernel, judgment_memory
from src.services.evidence_vault_canonical_core import canonical_fingerprint


SNAPSHOT_VERSION = "sv9-first-light-snapshot-v1"
REPORT_VERSION = "sv9-first-light-candidate-report-v1"


class FirstLightEvaluationError(ValueError):
    """The frozen rescan cannot be reconciled safely."""


def evaluate_first_light_rescan(
    *,
    accepted_snapshot: Mapping[str, Any],
    rescan_candidate: Mapping[str, Any],
    frozen_evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a pending candidate from independently evaluated rescan rows.

    A first-light change is eligible only when an accepted blind tile becomes a
    cited ``ok`` or ``no`` judgment.  A change to an established verdict is
    returned as review-only.  The accepted snapshot is copied, never mutated.
    """

    accepted = _snapshot(accepted_snapshot, expected_authority="accepted")
    candidate = _snapshot(rescan_candidate, expected_authority="pending")
    if (
        accepted["series_contract"] != candidate["series_contract"]
        or accepted["series_fingerprint"] != candidate["series_fingerprint"]
    ):
        raise FirstLightEvaluationError(
            "SV9 series contract/fingerprint mismatch; candidate is non-authoritative "
            "and must not mix series"
        )
    evidence = _frozen_evidence(frozen_evidence)
    accepted_by_tile = {row["tile_id"]: row for row in accepted["tiles"]}
    candidate_by_tile = {row["tile_id"]: row for row in candidate["tiles"]}
    if set(accepted_by_tile) != set(candidate_by_tile):
        raise FirstLightEvaluationError("candidate tile coverage is ambiguous")

    eligible: list[str] = []
    review: list[str] = []
    bindings: list[dict[str, str]] = []
    for tile_id, previous in accepted_by_tile.items():
        current = candidate_by_tile[tile_id]
        _validate_row_bindings(current, evidence)
        if previous["assessment_state"] == "sin_evidencia":
            if current["assessment_state"] in {"ok", "no"}:
                eligible.append(tile_id)
                bindings.extend(
                    {
                        "tile_id": tile_id,
                        "evidence_ref": item["evidence_ref"],
                        "evidence_fingerprint": item["evidence_fingerprint"],
                    }
                    for item in current["supporting_evidence"]
                )
        elif current["assessment_state"] != previous["assessment_state"]:
            review.append(tile_id)

    try:
        candidate_evidence = _frozen_evidence(candidate["evidence"])
    except FirstLightEvaluationError as exc:
        raise FirstLightEvaluationError("candidate evidence inventory is invalid") from exc
    supported = {
        (item["evidence_ref"], item["evidence_fingerprint"])
        for row in candidate["tiles"]
        for item in row["supporting_evidence"]
    }
    if candidate_evidence != evidence or set(candidate_evidence) != supported:
        raise FirstLightEvaluationError("candidate evidence binding inventory mismatch")

    eligible.sort()
    review.sort()
    effective_tiles = [
        deepcopy(candidate_by_tile[row["tile_id"]]) if row["tile_id"] in eligible else deepcopy(row)
        for row in accepted["tiles"]
    ]
    assessment = _assessment(effective_tiles)
    report_body = {
        "schema_version": REPORT_VERSION,
        "accepted_snapshot_fingerprint": accepted["snapshot_fingerprint"],
        "rescan_candidate_fingerprint": candidate["snapshot_fingerprint"],
        "tiles": effective_tiles,
        "assessment": assessment,
        "evidence_bindings": bindings,
        "authority": False,
        "publication": "not_published",
    }
    report = {
        **report_body,
        "report_fingerprint": canonical_fingerprint(
            "sv9-first-light-candidate-report-fingerprint-v1", report_body
        ),
    }
    return {
        "status": "candidate_ready" if eligible else "review_required",
        "accepted_snapshot": deepcopy(accepted_snapshot),
        "accepted_snapshot_fingerprint": accepted["snapshot_fingerprint"],
        "rescan_candidate_fingerprint": candidate["snapshot_fingerprint"],
        "eligible_tile_ids": eligible,
        "review_tile_ids": review,
        "evidence_bindings": bindings,
        "candidate_report": report,
        "candidate_fingerprint": canonical_fingerprint(
            "sv9-first-light-candidate-fingerprint-v1", report
        ),
    }


def _snapshot(value: Mapping[str, Any], *, expected_authority: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "tiles",
        "evidence",
        "snapshot_fingerprint",
    }:
        raise FirstLightEvaluationError("snapshot fields are invalid")
    if not isinstance(value["tiles"], list) or not isinstance(value["evidence"], list):
        raise FirstLightEvaluationError("snapshot payload is invalid")
    expected = canonical_fingerprint(
        SNAPSHOT_VERSION,
        {"tiles": value["tiles"], "evidence": value["evidence"]},
    )
    if value["snapshot_fingerprint"] != expected:
        raise FirstLightEvaluationError("snapshot fingerprint mismatch")
    rows = []
    for raw in value["tiles"]:
        try:
            row = judgment_memory.validate_tile_judgment(raw)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise FirstLightEvaluationError("candidate judgment is invalid") from exc
        if row["authority_state"] != expected_authority:
            label = "accepted snapshot" if expected_authority == "accepted" else "candidate"
            raise FirstLightEvaluationError(f"{label} authority state is invalid")
        if expected_authority == "accepted" and (
            row["review_state"] != "resolved" or row["lifecycle_state"] != "active"
        ):
            raise FirstLightEvaluationError("accepted snapshot row is not active authority")
        if expected_authority == "pending" and (
            row["review_state"] != "none" or row["lifecycle_state"] != "active"
        ):
            raise FirstLightEvaluationError("candidate row requires review or is not active")
        rows.append(row)
    if len({row["tile_id"] for row in rows}) != len(rows):
        raise FirstLightEvaluationError("candidate tile coverage is ambiguous")
    if expected_authority == "accepted":
        inventory = _frozen_evidence(value["evidence"])
        for row in rows:
            for item in row["supporting_evidence"]:
                pair = (item["evidence_ref"], item["evidence_fingerprint"])
                frozen = inventory.get(pair)
                if frozen is None or (
                    frozen["capture_origin"] != row["capture_origin"]
                    or frozen["operation_origin"] != row["operation_origin"]
                ):
                    raise FirstLightEvaluationError(
                        "accepted snapshot evidence binding is missing or invalid"
                    )
    series_contract, series_fingerprint = _series_identity(
        rows, "accepted snapshot" if expected_authority == "accepted" else "candidate"
    )
    return {
        "tiles": rows,
        "evidence": deepcopy(value["evidence"]),
        "snapshot_fingerprint": expected,
        "series_contract": series_contract,
        "series_fingerprint": series_fingerprint,
    }


def _series_identity(
    rows: Sequence[Mapping[str, Any]], label: str
) -> tuple[dict[str, Any], str]:
    if not rows:
        raise FirstLightEvaluationError(f"{label} series contract is missing")
    contract = judgment_memory.validate_judgment_series_contract(rows[0]["series_contract"])
    fingerprint = judgment_memory.canonical_fingerprint(
        "sv9-judgment-series-fingerprint-v1", contract
    )
    if rows[0]["series_fingerprint"] != fingerprint:
        raise FirstLightEvaluationError(f"{label} series fingerprint is invalid")
    for row in rows[1:]:
        if (
            row["series_contract"] != contract
            or row["series_fingerprint"] != fingerprint
        ):
            raise FirstLightEvaluationError(
                f"{label} mixes SV9 series contract/fingerprint"
            )
    return deepcopy(contract), fingerprint


def _frozen_evidence(values: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise FirstLightEvaluationError("frozen evidence is invalid")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in values:
        if not isinstance(raw, Mapping) or set(raw) != {
            "evidence_ref",
            "evidence_fingerprint",
            "capture_origin",
            "operation_origin",
            "provenance",
        }:
            raise FirstLightEvaluationError("frozen evidence binding is invalid")
        try:
            if not isinstance(raw["evidence_ref"], str) or not isinstance(
                raw["evidence_fingerprint"], str
            ):
                raise ValueError("evidence binding values must be strings")
            pair = (
                judgment_memory._text(raw["evidence_ref"], "evidence_ref"),
                judgment_memory._sha(raw["evidence_fingerprint"]),
            )
            if (raw["evidence_ref"], raw["evidence_fingerprint"]) != pair:
                raise ValueError("evidence binding is not canonical")
            judgment_memory._origin(raw["capture_origin"], "capture")
            judgment_memory._origin(raw["operation_origin"], "operation")
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise FirstLightEvaluationError("frozen evidence binding is invalid") from exc
        if raw["provenance"] != "evaluated":
            raise FirstLightEvaluationError("shadow evidence is not eligible")
        if pair in result or any(
            pair[0] == existing[0] or pair[1] == existing[1] for existing in result
        ):
            raise FirstLightEvaluationError("ambiguous frozen evidence binding")
        result[pair] = deepcopy(dict(raw))
        result[pair]["evidence_ref"] = pair[0]
        result[pair]["evidence_fingerprint"] = pair[1]
    return result


def _validate_row_bindings(row: Mapping[str, Any], evidence: Mapping[tuple[str, str], Mapping[str, Any]]) -> None:
    for item in row["supporting_evidence"]:
        pair = (item["evidence_ref"], item["evidence_fingerprint"])
        frozen = evidence.get(pair)
        if frozen is None:
            raise FirstLightEvaluationError("missing or invalid evidence binding")
        if (
            frozen["capture_origin"] != row["capture_origin"]
            or frozen["operation_origin"] != row["operation_origin"]
        ):
            raise FirstLightEvaluationError("evidence binding origin mismatch")


def _assessment(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    registry = {
        tile["tile_id"]: tile
        for component in assessment_kernel.build_sv9_tile_contract_registry()["components"]
        for tile in component["tiles"]
    }
    try:
        vector = [
            {
                "tile_id": row["tile_id"],
                "component_key": row["component_key"],
                "tile_key": registry[row["tile_id"]]["tile_key"],
                "assessment_state": row["assessment_state"],
            }
            for row in rows
        ]
        return assessment_kernel.build_sv9_assessment(vector)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise FirstLightEvaluationError("candidate assessment is invalid") from exc
