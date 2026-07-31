"""Private, packet-bound reviewer context for claim-to-tile mappings.

The public ledger intentionally omits raw claim and quote text. This module
reconstructs the exact reviewer context from immutable report snapshots and
binds it to the persisted ledger identity without changing runtime or scoring.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from src.evidence_identity import stable_artifact_digest
from src.services.evidence_claim_memory import build_evidence_claim_memory
from src.services.evidence_claim_tile_ledger import (
    EVIDENCE_CLAIM_TILE_MAPPING_VERSION,
    _claim_contexts_for_report,
    _mapping_series,
    _match_tile_to_claim,
    _ordered_reports,
    _tile_rows,
    _timestamp,
    build_evidence_claim_tile_ledger,
)
from src.services.evidence_claim_tile_review import (
    claim_tile_review_case_id,
)
from src.services.evidence_memory_identity_v2 import (
    build_evidence_memory_identity_v2,
)
from src.services.evidence_review_packet import (
    EVIDENCE_REVIEW_PACKET_SCHEMA_VERSION,
    review_packet_fingerprint,
)
from src.sv9.rubric import COMPONENTS, RUBRIC_VERSION


EVIDENCE_CLAIM_TILE_REVIEW_PACKET_MANIFEST_VERSION = (
    "evidence-claim-tile-review-packet-manifest-v1"
)
EVIDENCE_CLAIM_TILE_REVIEW_PACKET_CANDIDATE_VERSION = (
    "evidence-claim-tile-review-packet-candidate-v1"
)
EVIDENCE_CLAIM_TILE_REVIEW_PACKET_CANDIDATE_SET_VERSION = (
    "evidence-claim-tile-review-packet-candidates-v1"
)
EVIDENCE_CLAIM_TILE_REVIEW_PACKET_KIND = "claim_tile"

_REVIEW_PROMPT = (
    "¿El claim y el pasaje citados satisfacen específicamente el contrato "
    "de esta baldosa, y no solo una coincidencia literal?"
)
_MAPPING_IDENTITY_FIELDS = (
    "mapping_id",
    "mapping_series_id",
    "source_evidence_id",
    "claim_variant_id",
    "component_key",
    "tile_id",
    "tile_key",
    "polarity",
)


class EvidenceClaimTileReviewPacketError(ValueError):
    """The reconstructed packet violates its fail-closed contract."""


def build_evidence_claim_tile_review_packet(
    reports: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build one deterministic private packet for the latest mapping series."""

    ordered = _ordered_reports(reports)
    if not ordered:
        raise EvidenceClaimTileReviewPacketError(
            "immutable report snapshots are required"
        )
    ledger = build_evidence_claim_tile_ledger(ordered, mode="shadow")
    latest_series_id = str(
        ledger.get("latest_mapping_series_id") or ""
    )
    if not latest_series_id:
        raise EvidenceClaimTileReviewPacketError(
            "the latest report has no claim-to-tile mapping series"
        )
    series = next(
        (
            dict(row)
            for row in ledger.get("mapping_series") or []
            if isinstance(row, dict)
            and str(row.get("mapping_series_id") or "")
            == latest_series_id
        ),
        None,
    )
    if series is None:
        raise EvidenceClaimTileReviewPacketError(
            "the latest claim-to-tile mapping series is unavailable"
        )
    if str(series.get("rubric_version") or "") != RUBRIC_VERSION:
        raise EvidenceClaimTileReviewPacketError(
            "the latest mapping series does not use the current tile rubric"
        )

    mappings = [
        dict(mapping)
        for mapping in ledger.get("mappings") or []
        if isinstance(mapping, dict)
        and str(mapping.get("mapping_series_id") or "")
        == latest_series_id
    ]
    contexts = _review_contexts_by_mapping(ordered)
    candidates = [
        _candidate_for_mapping(
            mapping,
            contexts=contexts.get(str(mapping["mapping_id"]), []),
            brand=dict(ledger.get("brand") or {}),
            rubric_version=str(series["rubric_version"]),
        )
        for mapping in sorted(
            mappings,
            key=lambda row: str(row.get("mapping_id") or ""),
        )
    ]
    candidates.sort(key=lambda row: str(row["case_id"]))
    candidate_fingerprint = stable_artifact_digest(
        EVIDENCE_CLAIM_TILE_REVIEW_PACKET_CANDIDATE_SET_VERSION,
        candidates,
    )
    case_ids = [str(row["case_id"]) for row in candidates]
    subject_ids = [str(row["subject_id"]) for row in candidates]
    manifest: dict[str, Any] = {
        "schema_version": (
            EVIDENCE_CLAIM_TILE_REVIEW_PACKET_MANIFEST_VERSION
        ),
        "review_packet_schema_version": (
            EVIDENCE_REVIEW_PACKET_SCHEMA_VERSION
        ),
        "candidate_schema_version": (
            EVIDENCE_CLAIM_TILE_REVIEW_PACKET_CANDIDATE_VERSION
        ),
        "packet_kind": EVIDENCE_CLAIM_TILE_REVIEW_PACKET_KIND,
        "scope": "latest_mapping_series",
        "brand": dict(ledger.get("brand") or {}),
        "report_count": int(ledger.get("report_count") or 0),
        "latest_report_id": ledger.get("latest_report_id"),
        "mapping_series_id": latest_series_id,
        "ledger_schema_version": str(
            ledger.get("schema_version") or ""
        ),
        "ledger_policy_version": str(
            ledger.get("policy_version") or ""
        ),
        "mapping_version": str(
            ledger.get("mapping_version") or ""
        ),
        "ledger_state_fingerprint": str(
            ledger.get("state_fingerprint") or ""
        ),
        "rubric_version": str(series["rubric_version"]),
        "candidate_count": len(candidates),
        "case_ids": case_ids,
        "subject_ids": subject_ids,
        "candidate_fingerprint": candidate_fingerprint,
        "decision_semantics": {
            "accepted": (
                "The cited claim and evidence satisfy the exact tile "
                "contract."
            ),
            "disputed": (
                "The packet does not resolve the semantic mapping safely."
            ),
            "rejected": (
                "Literal provenance exists, but the semantic tile contract "
                "is not satisfied."
            ),
        },
        "runtime_effect": False,
        "authority": False,
        "automatic_tile_effect": False,
        "automatic_scoring_effect": False,
    }
    schema_versions = _schema_versions(manifest)
    manifest["review_packet_fingerprint"] = review_packet_fingerprint(
        packet_kind=EVIDENCE_CLAIM_TILE_REVIEW_PACKET_KIND,
        manifest=manifest,
        candidates=candidates,
        schema_versions=schema_versions,
    )
    packet = {
        "manifest": manifest,
        "candidates": candidates,
        "review_template": build_review_template(
            manifest,
            candidates,
        ),
    }
    validate_evidence_claim_tile_review_packet(packet)
    return packet


def build_review_template(
    manifest: dict[str, Any],
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return unsigned decision rows bound to the exact registered packet."""

    packet_fingerprint = str(
        manifest.get("review_packet_fingerprint") or ""
    )
    candidate_fingerprint = str(
        manifest.get("candidate_fingerprint") or ""
    )
    return [
        {
            "case_id": str(candidate.get("case_id") or ""),
            "subject_id": str(candidate.get("subject_id") or ""),
            "candidate_fingerprint": candidate_fingerprint,
            "review_packet_fingerprint": packet_fingerprint,
            "decision": None,
            "rationale": None,
            "reviewer_id": None,
            "reviewed_at": None,
            "event_id": None,
        }
        for candidate in sorted(
            (dict(row) for row in candidates),
            key=lambda row: str(row.get("case_id") or ""),
        )
    ]


def validate_evidence_claim_tile_review_packet(
    packet: dict[str, Any],
) -> None:
    """Recompute every packet identity and reject mixed or altered content."""

    if not isinstance(packet, dict):
        raise EvidenceClaimTileReviewPacketError(
            "claim-to-tile review packet must be an object"
        )
    manifest = packet.get("manifest")
    candidates = packet.get("candidates")
    if not isinstance(manifest, dict) or not isinstance(candidates, list):
        raise EvidenceClaimTileReviewPacketError(
            "packet manifest and candidates are required"
        )
    if (
        manifest.get("schema_version")
        != EVIDENCE_CLAIM_TILE_REVIEW_PACKET_MANIFEST_VERSION
        or manifest.get("review_packet_schema_version")
        != EVIDENCE_REVIEW_PACKET_SCHEMA_VERSION
        or manifest.get("candidate_schema_version")
        != EVIDENCE_CLAIM_TILE_REVIEW_PACKET_CANDIDATE_VERSION
        or manifest.get("packet_kind")
        != EVIDENCE_CLAIM_TILE_REVIEW_PACKET_KIND
        or manifest.get("scope") != "latest_mapping_series"
    ):
        raise EvidenceClaimTileReviewPacketError(
            "unsupported claim-to-tile review packet contract"
        )
    for field in (
        "runtime_effect",
        "authority",
        "automatic_tile_effect",
        "automatic_scoring_effect",
    ):
        if manifest.get(field) is not False:
            raise EvidenceClaimTileReviewPacketError(
                f"review packet {field} must remain false"
            )
    normalized_candidates = [
        dict(row) for row in candidates if isinstance(row, dict)
    ]
    if len(normalized_candidates) != len(candidates):
        raise EvidenceClaimTileReviewPacketError(
            "every review candidate must be an object"
        )
    case_ids: list[str] = []
    subject_ids: list[str] = []
    for candidate in normalized_candidates:
        _validate_candidate(candidate, manifest=manifest)
        case_ids.append(str(candidate["case_id"]))
        subject_ids.append(str(candidate["subject_id"]))
    if len(case_ids) != len(set(case_ids)):
        raise EvidenceClaimTileReviewPacketError(
            "review packet contains duplicate case ids"
        )
    if len(subject_ids) != len(set(subject_ids)):
        raise EvidenceClaimTileReviewPacketError(
            "review packet contains duplicate mapping subjects"
        )
    if case_ids != sorted(case_ids):
        raise EvidenceClaimTileReviewPacketError(
            "review candidates must be canonically ordered"
        )
    if (
        manifest.get("case_ids") != case_ids
        or manifest.get("subject_ids") != subject_ids
        or int(manifest.get("candidate_count") or 0) != len(candidates)
    ):
        raise EvidenceClaimTileReviewPacketError(
            "review packet manifest coverage does not match candidates"
        )
    expected_candidate_fingerprint = stable_artifact_digest(
        EVIDENCE_CLAIM_TILE_REVIEW_PACKET_CANDIDATE_SET_VERSION,
        normalized_candidates,
    )
    if (
        str(manifest.get("candidate_fingerprint") or "")
        != expected_candidate_fingerprint
    ):
        raise EvidenceClaimTileReviewPacketError(
            "review candidate fingerprint mismatch"
        )
    expected_packet_fingerprint = review_packet_fingerprint(
        packet_kind=EVIDENCE_CLAIM_TILE_REVIEW_PACKET_KIND,
        manifest=manifest,
        candidates=normalized_candidates,
        schema_versions=_schema_versions(manifest),
    )
    if (
        str(manifest.get("review_packet_fingerprint") or "")
        != expected_packet_fingerprint
    ):
        raise EvidenceClaimTileReviewPacketError(
            "review packet fingerprint mismatch"
        )


def packet_candidate_for_subject(
    packet: dict[str, Any],
    subject_id: str,
) -> dict[str, Any] | None:
    """Return a candidate only after validating the full packet."""

    validate_evidence_claim_tile_review_packet(packet)
    normalized = str(subject_id or "").strip().lower()
    return next(
        (
            dict(candidate)
            for candidate in packet["candidates"]
            if str(candidate.get("subject_id") or "") == normalized
        ),
        None,
    )


def _review_contexts_by_mapping(
    reports: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    identity_projection = build_evidence_memory_identity_v2(
        reports,
        mode="shadow",
    )
    claim_projection = build_evidence_claim_memory(
        reports,
        mode="shadow",
    )
    evidence_by_id = {
        str(entry.get("evidence_id") or ""): entry
        for entry in identity_projection.get("entries") or []
        if isinstance(entry, dict)
        and str(entry.get("evidence_id") or "")
    }
    occurrences_by_report_evidence: dict[
        tuple[str, str],
        list[dict[str, Any]],
    ] = defaultdict(list)
    for occurrence in claim_projection.get("occurrences") or []:
        if not isinstance(occurrence, dict):
            continue
        occurrences_by_report_evidence[
            (
                str(occurrence.get("report_id") or ""),
                str(occurrence.get("evidence_id") or ""),
            )
        ].append(occurrence)

    contexts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for report in reports:
        report_id = str(report.get("id") or "")
        observed_at = _timestamp(
            report.get("created_at")
        ).isoformat()
        series = _mapping_series(
            report,
            identity_projection=identity_projection,
            claim_projection=claim_projection,
        )
        series_id = str(series["mapping_series_id"])
        claim_contexts = _claim_contexts_for_report(
            report,
            occurrences_by_report_evidence=(
                occurrences_by_report_evidence
            ),
            evidence_by_id=evidence_by_id,
        )
        for tile in _tile_rows(report):
            match = _match_tile_to_claim(
                tile,
                claim_contexts=claim_contexts,
            )
            if match["status"] != "mapped":
                continue
            context = dict(match["context"])
            mapping_id = stable_artifact_digest(
                EVIDENCE_CLAIM_TILE_MAPPING_VERSION,
                {
                    "mapping_series_id": series_id,
                    "source_evidence_id": context[
                        "source_evidence_id"
                    ],
                    "claim_variant_id": context["claim_variant_id"],
                    "tile_key": tile["tile_key"],
                    "polarity": tile["polarity"],
                },
            )
            contexts[mapping_id].append(
                {
                    "report_id": report_id,
                    "observed_at": observed_at,
                    "mapping_series_id": series_id,
                    "match_method": str(match["match_method"]),
                    "claim": context,
                    "tile": dict(tile),
                }
            )
    return contexts


def _candidate_for_mapping(
    mapping: dict[str, Any],
    *,
    contexts: list[dict[str, Any]],
    brand: dict[str, Any],
    rubric_version: str,
) -> dict[str, Any]:
    observations = [
        dict(row)
        for row in mapping.get("observations") or []
        if isinstance(row, dict)
    ]
    if not observations:
        raise EvidenceClaimTileReviewPacketError(
            f"mapping has no immutable observations: {mapping['mapping_id']}"
        )
    latest_observation = observations[-1]
    matching_contexts = [
        context
        for context in contexts
        if (
            str(context.get("report_id") or "")
            == str(latest_observation.get("report_id") or "")
            and str(
                (context.get("claim") or {}).get(
                    "claim_occurrence_id"
                )
                or ""
            )
            == str(
                latest_observation.get("claim_occurrence_id") or ""
            )
            and str(
                (context.get("tile") or {}).get("quote_hash") or ""
            )
            == str(latest_observation.get("quote_hash") or "")
            and str(context.get("match_method") or "")
            == str(latest_observation.get("match_method") or "")
        )
    ]
    if len(matching_contexts) != 1:
        raise EvidenceClaimTileReviewPacketError(
            "mapping reviewer context is missing or ambiguous: "
            f"{mapping['mapping_id']}"
        )
    context = matching_contexts[0]
    claim = dict(context["claim"])
    tile = dict(context["tile"])
    tile_spec = _tile_spec(
        str(mapping["component_key"]),
        str(mapping["tile_id"]),
    )
    domain = str(brand.get("domain") or "")
    case_id = claim_tile_review_case_id(domain, mapping)
    return {
        "schema_version": (
            EVIDENCE_CLAIM_TILE_REVIEW_PACKET_CANDIDATE_VERSION
        ),
        "case_id": case_id,
        "case_type": "literal_claim_to_semantic_tile_contract",
        "subject_id": str(mapping["mapping_id"]),
        "brand": {
            "name": str(brand.get("name") or ""),
            "domain": domain,
        },
        "report": {
            "report_id": str(context["report_id"]),
            "observed_at": str(context["observed_at"]),
        },
        "source": {
            "source_evidence_id": str(
                claim["source_evidence_id"]
            ),
            "source_evidence_ref": str(
                claim["source_evidence_ref"]
            ),
            "source_class": str(claim["source_class"]),
            "identity_status": str(claim["identity_status"]),
            "source_independence_status": str(
                claim["source_independence_status"]
            ),
            "url": str(claim["source_url"]),
            "passage": str(claim["source_content"]),
        },
        "claim": {
            "claim_slot_id": str(claim["claim_slot_id"]),
            "claim_slot_key": str(claim["claim_slot_key"]),
            "claim_type": str(claim["claim_type"]),
            "claim_variant_id": str(claim["claim_variant_id"]),
            "content": str(claim["claim_content"]),
            "derivation_mode": str(
                claim["claim_derivation_mode"]
            ),
        },
        "tile": {
            "rubric_version": rubric_version,
            "component_key": str(mapping["component_key"]),
            "tile_id": str(mapping["tile_id"]),
            "tile_key": str(mapping["tile_key"]),
            "name": str(tile_spec["name"]),
            "condition": str(tile_spec["condition"]),
            "state": str(tile["tile_state"]),
            "polarity": str(mapping["polarity"]),
            "evidence_quote": str(tile["quote"]),
            "evidence_contract": dict(
                tile_spec["evidence_contract"]
            ),
        },
        "mapping": {
            field: str(mapping.get(field) or "")
            for field in _MAPPING_IDENTITY_FIELDS
        }
        | {
            "match_method": str(context["match_method"]),
            "state": str(mapping["state"]),
            "observation_count": int(
                mapping["observation_count"]
            ),
            "present_in_series_latest": bool(
                mapping["present_in_series_latest"]
            ),
            "present_in_latest_report": bool(
                mapping["present_in_latest_report"]
            ),
        },
        "review_prompt": _REVIEW_PROMPT,
        "runtime_effect": False,
        "authority": False,
        "automatic_tile_effect": False,
        "automatic_scoring_effect": False,
    }


def _validate_candidate(
    candidate: dict[str, Any],
    *,
    manifest: dict[str, Any],
) -> None:
    if (
        candidate.get("schema_version")
        != EVIDENCE_CLAIM_TILE_REVIEW_PACKET_CANDIDATE_VERSION
    ):
        raise EvidenceClaimTileReviewPacketError(
            "review candidate schema version mismatch"
        )
    for field in (
        "runtime_effect",
        "authority",
        "automatic_tile_effect",
        "automatic_scoring_effect",
    ):
        if candidate.get(field) is not False:
            raise EvidenceClaimTileReviewPacketError(
                f"review candidate {field} must remain false"
            )
    for field in ("case_id", "subject_id", "review_prompt"):
        if not str(candidate.get(field) or "").strip():
            raise EvidenceClaimTileReviewPacketError(
                f"review candidate {field} is required"
            )
    subject_id = str(candidate["subject_id"])
    if len(subject_id) != 64 or any(
        character not in "0123456789abcdef"
        for character in subject_id
    ):
        raise EvidenceClaimTileReviewPacketError(
            "review candidate subject_id must be sha256"
        )
    mapping = candidate.get("mapping")
    source = candidate.get("source")
    claim = candidate.get("claim")
    tile = candidate.get("tile")
    report = candidate.get("report")
    if not all(
        isinstance(value, dict)
        for value in (mapping, source, claim, tile, report)
    ):
        raise EvidenceClaimTileReviewPacketError(
            "review candidate context is incomplete"
        )
    if str(mapping.get("mapping_id") or "") != subject_id:
        raise EvidenceClaimTileReviewPacketError(
            "review candidate mapping does not match subject"
        )
    for field in _MAPPING_IDENTITY_FIELDS:
        if not str(mapping.get(field) or ""):
            raise EvidenceClaimTileReviewPacketError(
                f"review candidate mapping {field} is required"
            )
    if (
        str(mapping["mapping_series_id"])
        != str(manifest.get("mapping_series_id") or "")
        or str(tile.get("rubric_version") or "")
        != str(manifest.get("rubric_version") or "")
    ):
        raise EvidenceClaimTileReviewPacketError(
            "review candidate uses another packet version"
        )
    required_context = (
        (source, ("source_evidence_id", "source_evidence_ref", "url", "passage")),
        (claim, ("claim_slot_id", "claim_variant_id", "content")),
        (
            tile,
            (
                "component_key",
                "tile_id",
                "tile_key",
                "name",
                "condition",
                "state",
                "polarity",
                "evidence_quote",
                "evidence_contract",
            ),
        ),
        (report, ("report_id", "observed_at")),
    )
    for section, fields in required_context:
        if any(
            section.get(field) is None or section.get(field) == ""
            for field in fields
        ):
            raise EvidenceClaimTileReviewPacketError(
                "review candidate human context is incomplete"
            )
    if (
        str(source.get("source_evidence_id") or "")
        != str(mapping["source_evidence_id"])
        or str(claim.get("claim_variant_id") or "")
        != str(mapping["claim_variant_id"])
        or str(tile.get("component_key") or "")
        != str(mapping["component_key"])
        or str(tile.get("tile_id") or "")
        != str(mapping["tile_id"])
        or str(tile.get("tile_key") or "")
        != str(mapping["tile_key"])
        or str(tile.get("polarity") or "")
        != str(mapping["polarity"])
    ):
        raise EvidenceClaimTileReviewPacketError(
            "review candidate identity fields diverge"
        )
    contract = tile.get("evidence_contract")
    if not isinstance(contract, dict) or not all(
        str(contract.get(field) or "").strip()
        for field in ("ok", "no", "sin_evidencia", "reject")
    ):
        raise EvidenceClaimTileReviewPacketError(
            "review candidate tile contract is incomplete"
        )


def _tile_spec(component_key: str, tile_id: str) -> dict[str, Any]:
    component = COMPONENTS.get(component_key)
    if not isinstance(component, dict):
        raise EvidenceClaimTileReviewPacketError(
            f"unknown tile component: {component_key}"
        )
    matches = [
        dict(tile)
        for tile in component.get("tiles") or []
        if isinstance(tile, dict)
        and str(tile.get("id") or "") == tile_id
    ]
    if len(matches) != 1:
        raise EvidenceClaimTileReviewPacketError(
            f"unknown or ambiguous tile contract: {component_key}.{tile_id}"
        )
    return matches[0]


def _schema_versions(manifest: dict[str, Any]) -> dict[str, str]:
    return {
        "candidate": str(
            manifest.get("candidate_schema_version") or ""
        ),
        "ledger": str(manifest.get("ledger_schema_version") or ""),
        "manifest": str(manifest.get("schema_version") or ""),
        "mapping": str(manifest.get("mapping_version") or ""),
        "packet": str(
            manifest.get("review_packet_schema_version") or ""
        ),
        "rubric": str(manifest.get("rubric_version") or ""),
    }
