"""Versioned shadow ledger linking evidence to claims and SV9 tiles."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from typing import Any, Iterable
from urllib.parse import urlparse

from src.evidence_identity import (
    canonical_evidence_digest,
    normalize_evidence_text,
    normalize_evidence_url,
    stable_artifact_digest,
)
from src.services.evidence_claim_memory import (
    EVIDENCE_CLAIM_MEMORY_POLICY_VERSION,
    build_evidence_claim_memory,
)
from src.services.evidence_memory_identity_v2 import (
    EVIDENCE_MEMORY_IDENTITY_V2_POLICY_VERSION,
    build_evidence_memory_identity_v2,
)
from src.services.scanner_evidence_comparison import MATERIAL_SOURCE_CLASSES
from src.sv9_flow.claim_slot_producer import (
    resolve_candidate_claim_memory_evidence,
)


EVIDENCE_CLAIM_TILE_LEDGER_VERSION = "evidence-claim-tile-ledger-v1"
EVIDENCE_CLAIM_TILE_LEDGER_POLICY_VERSION = (
    "evidence-claim-tile-ledger-policy-v1"
)
EVIDENCE_CLAIM_TILE_MAPPING_VERSION = "evidence-claim-tile-mapping-v1"
EVIDENCE_CLAIM_TILE_LEDGER_MODE_ENV = (
    "B3S_EVIDENCE_CLAIM_TILE_LEDGER_MODE"
)
EVIDENCE_CLAIM_TILE_LEDGER_MODES = {"disabled", "shadow"}

_TILE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_ELLIPSIS_RE = re.compile(r"(?:\.{3,}|…+)")
_TILE_POLARITY = {
    "ok": "supports",
    "no": "weakens",
    "sin_evidencia": "insufficient_evidence",
}


def evidence_claim_tile_ledger_mode(value: str | None = None) -> str:
    raw = str(
        value
        if value is not None
        else os.environ.get(
            EVIDENCE_CLAIM_TILE_LEDGER_MODE_ENV,
            "disabled",
        )
    ).strip().lower()
    return (
        raw
        if raw in EVIDENCE_CLAIM_TILE_LEDGER_MODES
        else "disabled"
    )


def build_evidence_claim_tile_ledger(
    reports: Iterable[dict[str, Any]],
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic, non-authoritative mapping projection."""

    ordered = _ordered_reports(reports)
    effective_mode = evidence_claim_tile_ledger_mode(mode)
    latest = ordered[-1] if ordered else {}
    latest_report_id = str(latest.get("id") or "")
    brand_domain = _domain(str(latest.get("url") or ""))
    result: dict[str, Any] = {
        "schema_version": EVIDENCE_CLAIM_TILE_LEDGER_VERSION,
        "policy_version": EVIDENCE_CLAIM_TILE_LEDGER_POLICY_VERSION,
        "mapping_version": EVIDENCE_CLAIM_TILE_MAPPING_VERSION,
        "mode": effective_mode,
        "runtime_effect": False,
        "authority": False,
        "brand": {
            "name": str(latest.get("brand_name") or ""),
            "domain": brand_domain,
        },
        "report_count": len(ordered),
        "latest_report_id": latest_report_id or None,
        "latest_mapping_series_id": None,
        "policy": {
            "literal_source_quote_required": True,
            "claim_text_anchor_required": True,
            "ambiguous_mapping_is_rejected": True,
            "mapping_series_is_versioned": True,
            "exact_repeat_increases_breadth": False,
            "automatic_tile_state_change": False,
            "automatic_scoring_effect": False,
            "raw_claim_text_returned": False,
            "raw_tile_quote_returned": False,
        },
        "warnings": [
            "shadow_only_no_scoring_or_selection_effect",
            "mapping_does_not_validate_claim_or_tile",
            "same_page_without_literal_claim_anchor_is_not_support",
            "unmatched_and_ambiguous_tiles_remain_unmapped",
            "new_evaluator_or_policy_versions_create_new_mapping_series",
        ],
        "summary": _empty_summary(),
        "mapping_series": [],
        "mappings": [],
    }
    if effective_mode != "shadow" or not ordered:
        result["state_fingerprint"] = _state_fingerprint(result)
        return result

    identity_projection = build_evidence_memory_identity_v2(
        ordered,
        mode="shadow",
    )
    claim_projection = build_evidence_claim_memory(
        ordered,
        mode="shadow",
    )
    evidence_by_id = {
        str(entry.get("evidence_id") or ""): entry
        for entry in identity_projection.get("entries") or []
        if isinstance(entry, dict) and str(entry.get("evidence_id") or "")
    }
    occurrences_by_report_evidence: dict[
        tuple[str, str],
        list[dict[str, Any]],
    ] = defaultdict(list)
    for occurrence in claim_projection.get("occurrences") or []:
        if not isinstance(occurrence, dict):
            continue
        key = (
            str(occurrence.get("report_id") or ""),
            str(occurrence.get("evidence_id") or ""),
        )
        occurrences_by_report_evidence[key].append(occurrence)

    mapping_accumulators: dict[str, dict[str, Any]] = {}
    series_accumulators: dict[str, dict[str, Any]] = {}
    diagnostic_counts: Counter[str] = Counter()
    tile_state_counts: Counter[str] = Counter()
    mapped_polarity_counts: Counter[str] = Counter()
    latest_series_by_report: dict[str, str] = {}

    for report in ordered:
        report_id = str(report.get("id") or "")
        observed_at = _timestamp(report.get("created_at")).isoformat()
        series = _mapping_series(
            report,
            identity_projection=identity_projection,
            claim_projection=claim_projection,
        )
        series_id = str(series["mapping_series_id"])
        latest_series_by_report[report_id] = series_id
        series_accumulator = series_accumulators.setdefault(
            series_id,
            {
                **series,
                "report_ids": set(),
                "observed_at_by_report": {},
            },
        )
        series_accumulator["report_ids"].add(report_id)
        series_accumulator["observed_at_by_report"][
            report_id
        ] = observed_at
        claim_contexts = _claim_contexts_for_report(
            report,
            occurrences_by_report_evidence=(
                occurrences_by_report_evidence
            ),
            evidence_by_id=evidence_by_id,
        )
        tiles = _tile_rows(report)
        if tiles and not claim_contexts:
            diagnostic_counts["report_has_no_claim_occurrence"] += len(
                tiles
            )
        for tile in tiles:
            tile_state = str(tile["tile_state"])
            tile_state_counts[tile_state] += 1
            diagnostic_counts["tile_observation_count"] += 1
            match = _match_tile_to_claim(
                tile,
                claim_contexts=claim_contexts,
            )
            if match["status"] != "mapped":
                diagnostic_counts[str(match["reason"])] += 1
                continue
            context = match["context"]
            polarity = str(tile["polarity"])
            mapped_polarity_counts[polarity] += 1
            mapping_id = stable_artifact_digest(
                EVIDENCE_CLAIM_TILE_MAPPING_VERSION,
                {
                    "mapping_series_id": series_id,
                    "source_evidence_id": context[
                        "source_evidence_id"
                    ],
                    "claim_variant_id": context["claim_variant_id"],
                    "tile_key": tile["tile_key"],
                    "polarity": polarity,
                },
            )
            accumulator = mapping_accumulators.setdefault(
                mapping_id,
                {
                    "mapping_id": mapping_id,
                    "mapping_series_id": series_id,
                    "source_evidence_id": context[
                        "source_evidence_id"
                    ],
                    "claim_evidence_id": context["claim_evidence_id"],
                    "claim_slot_id": context["claim_slot_id"],
                    "claim_variant_id": context["claim_variant_id"],
                    "component_key": tile["component_key"],
                    "tile_id": tile["tile_id"],
                    "tile_key": tile["tile_key"],
                    "polarity": polarity,
                    "source_class": context["source_class"],
                    "identity_status": context["identity_status"],
                    "source_independence_status": context[
                        "source_independence_status"
                    ],
                    "source_evidence_refs": set(),
                    "match_methods": set(),
                    "quote_hashes": set(),
                    "observations_by_report": {},
                },
            )
            accumulator["source_evidence_refs"].add(
                context["source_evidence_ref"]
            )
            accumulator["match_methods"].add(match["match_method"])
            accumulator["quote_hashes"].add(tile["quote_hash"])
            observation = accumulator["observations_by_report"].setdefault(
                report_id,
                {
                    "report_id": report_id,
                    "observed_at": observed_at,
                    "claim_occurrence_id": context[
                        "claim_occurrence_id"
                    ],
                    "tile_state": tile_state,
                    "quote_hash": tile["quote_hash"],
                    "match_method": match["match_method"],
                    "duplicate_observation_count": 0,
                },
            )
            observation["duplicate_observation_count"] += 1

    series = _finalize_series(series_accumulators)
    latest_mapping_series_id = latest_series_by_report.get(
        latest_report_id
    )
    result["latest_mapping_series_id"] = latest_mapping_series_id
    latest_report_by_series = {
        str(item["mapping_series_id"]): str(
            item["latest_report_id"] or ""
        )
        for item in series
    }
    mappings = _finalize_mappings(
        mapping_accumulators,
        latest_report_by_series=latest_report_by_series,
        latest_report_id=latest_report_id,
    )
    state_counts = Counter(
        str(mapping["state"]) for mapping in mappings
    )
    result["mapping_series"] = series
    result["mappings"] = mappings
    result["summary"] = {
        "mapping_series_count": len(series),
        "mapping_count": len(mappings),
        "mapping_observation_count": sum(
            int(mapping["observation_count"]) for mapping in mappings
        ),
        "current_series_mapping_count": sum(
            1
            for mapping in mappings
            if mapping["mapping_series_id"]
            == latest_mapping_series_id
            and mapping["present_in_series_latest"]
        ),
        "source_evidence_count": len(
            {
                str(mapping["source_evidence_id"])
                for mapping in mappings
            }
        ),
        "claim_variant_count": len(
            {
                str(mapping["claim_variant_id"])
                for mapping in mappings
            }
        ),
        "tile_count": len(
            {str(mapping["tile_key"]) for mapping in mappings}
        ),
        "state_counts": dict(sorted(state_counts.items())),
        "polarity_counts": dict(
            sorted(mapped_polarity_counts.items())
        ),
        "tile_state_observation_counts": dict(
            sorted(tile_state_counts.items())
        ),
        "diagnostic_counts": dict(
            sorted(diagnostic_counts.items())
        ),
    }
    result["state_fingerprint"] = _state_fingerprint(result)
    return result


def _claim_contexts_for_report(
    report: dict[str, Any],
    *,
    occurrences_by_report_evidence: dict[
        tuple[str, str],
        list[dict[str, Any]],
    ],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    report_id = str(report.get("id") or "")
    candidate = _candidate(report)
    evidence_rows = _candidate_evidence_rows(candidate)
    source_rows_by_ref = {
        str(row.get("ref") or ""): row
        for row in evidence_rows
        if str(row.get("ref") or "")
    }
    resolution = resolve_candidate_claim_memory_evidence(candidate)
    resolved_rows = resolution.get("records")
    if not isinstance(resolved_rows, list):
        resolved_rows = []
    contexts: dict[str, dict[str, Any]] = {}
    for claim_row in [*evidence_rows, *resolved_rows]:
        if not isinstance(claim_row, dict):
            continue
        claim_identity = _row_evidence_identity(claim_row)
        if claim_identity is None:
            continue
        claim_evidence_id = str(claim_identity["evidence_id"])
        occurrences = occurrences_by_report_evidence.get(
            (report_id, claim_evidence_id),
            [],
        )
        if not occurrences:
            continue
        metadata = (
            claim_row.get("metadata")
            if isinstance(claim_row.get("metadata"), dict)
            else {}
        )
        source_evidence_ref = str(
            metadata.get("source_evidence_ref")
            or claim_row.get("ref")
            or ""
        )
        source_row = source_rows_by_ref.get(source_evidence_ref)
        if source_row is None and source_evidence_ref == str(
            claim_row.get("ref") or ""
        ):
            source_row = claim_row
        if source_row is None:
            continue
        source_identity = _row_evidence_identity(source_row)
        if source_identity is None:
            continue
        source_evidence_id = str(source_identity["evidence_id"])
        source_entry = evidence_by_id.get(source_evidence_id, {})
        for occurrence in occurrences:
            occurrence_id = str(
                occurrence.get("claim_occurrence_id") or ""
            )
            if not occurrence_id:
                continue
            contexts[occurrence_id] = {
                "claim_occurrence_id": occurrence_id,
                "claim_slot_id": str(
                    occurrence.get("claim_slot_id") or ""
                ),
                "claim_slot_key": str(
                    metadata.get("claim_slot_key")
                    or metadata.get("claim_id")
                    or ""
                ),
                "claim_type": str(
                    metadata.get("claim_type") or "unknown"
                ),
                "claim_derivation_mode": str(
                    occurrence.get("claim_slot_derivation_mode")
                    or metadata.get("claim_slot_derivation_mode")
                    or "upstream_unknown"
                ),
                "claim_variant_id": str(
                    occurrence.get("claim_variant_id") or ""
                ),
                "claim_evidence_id": claim_evidence_id,
                "claim_content": normalize_evidence_text(
                    claim_row.get("content")
                ),
                "source_evidence_id": source_evidence_id,
                "source_evidence_ref": source_evidence_ref,
                "source_url": str(
                    source_identity.get("url") or ""
                ),
                "source_content": normalize_evidence_text(
                    source_row.get("content")
                ),
                "source_class": str(
                    source_identity["source_class"]
                ),
                "identity_status": str(
                    source_entry.get("identity_status")
                    or "unverified"
                ),
                "source_independence_status": (
                    _source_independence_status(
                        source_entry,
                        source_class=str(
                            source_identity["source_class"]
                        ),
                    )
                ),
            }
    return [
        contexts[key]
        for key in sorted(contexts)
    ]


def _match_tile_to_claim(
    tile: dict[str, Any],
    *,
    claim_contexts: list[dict[str, Any]],
) -> dict[str, Any]:
    if not claim_contexts:
        return {
            "status": "unmapped",
            "reason": "tile_has_no_claim_context",
        }
    quote = str(tile.get("quote") or "")
    if not normalize_evidence_text(quote):
        return {
            "status": "unmapped",
            "reason": "tile_has_no_evidence_quote",
        }
    explicit_ref = str(tile.get("evidence_ref") or "")
    literal_candidates: list[dict[str, Any]] = []
    anchored_candidates: list[dict[str, Any]] = []
    for context in claim_contexts:
        if (
            explicit_ref
            and explicit_ref != context["source_evidence_ref"]
        ):
            continue
        if not _literal_quote_matches(
            quote,
            context["source_content"],
        ):
            continue
        literal_candidates.append(context)
        if _quote_anchors_claim(
            quote,
            context["claim_content"],
        ):
            anchored_candidates.append(context)
    if not literal_candidates:
        return {
            "status": "unmapped",
            "reason": (
                "explicit_evidence_ref_does_not_match_claim_source"
                if explicit_ref
                else "tile_quote_not_literal_in_claim_source"
            ),
        }
    if not anchored_candidates:
        return {
            "status": "unmapped",
            "reason": "tile_quote_does_not_anchor_claim",
        }
    unique = {
        (
            str(context["source_evidence_id"]),
            str(context["claim_variant_id"]),
        ): context
        for context in anchored_candidates
    }
    if len(unique) != 1:
        return {
            "status": "unmapped",
            "reason": "tile_claim_mapping_is_ambiguous",
        }
    return {
        "status": "mapped",
        "reason": "",
        "context": next(iter(unique.values())),
        "match_method": (
            "explicit_evidence_ref"
            if explicit_ref
            else "unique_literal_quote"
        ),
    }


def _literal_quote_matches(quote: str, source_content: str) -> bool:
    source = normalize_evidence_text(source_content).casefold()
    fragments = _quote_fragments(quote)
    if not source or not fragments:
        return False
    cursor = 0
    for fragment in fragments:
        position = source.find(fragment, cursor)
        if position < 0:
            return False
        cursor = position + len(fragment)
    return True


def _quote_anchors_claim(quote: str, claim_content: str) -> bool:
    claim = normalize_evidence_text(claim_content).casefold()
    if not claim:
        return False
    fragments = _quote_fragments(quote)
    return any(
        claim in fragment or fragment in claim
        for fragment in fragments
        if len(fragment) >= 8
    )


def _quote_fragments(value: str) -> list[str]:
    return [
        normalized
        for fragment in _ELLIPSIS_RE.split(str(value or ""))
        if (
            normalized := normalize_evidence_text(fragment).casefold()
        )
    ]


def _tile_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    sv9 = raw.get("sv9") if isinstance(raw.get("sv9"), dict) else {}
    result = (
        sv9.get("result")
        if isinstance(sv9.get("result"), dict)
        else {}
    )
    components = (
        result.get("components")
        if isinstance(result.get("components"), dict)
        else {}
    )
    rows: list[dict[str, Any]] = []
    for component_key in sorted(components):
        component = components.get(component_key)
        if not isinstance(component, dict):
            continue
        tile_profile = component.get("tile_profile")
        if not isinstance(tile_profile, list):
            continue
        for tile in tile_profile:
            if not isinstance(tile, dict):
                continue
            tile_id = str(
                tile.get("id") or tile.get("tile_id") or ""
            ).strip()
            tile_key = f"{component_key}.{tile_id}"
            tile_state = str(
                tile.get("estado") or tile.get("state") or ""
            ).strip()
            if (
                not tile_id
                or not _TILE_KEY_PATTERN.fullmatch(tile_key)
                or tile_state not in _TILE_POLARITY
            ):
                continue
            quote = str(
                tile.get("evidencia")
                or tile.get("evidence_quote")
                or ""
            ).strip()
            rows.append(
                {
                    "component_key": str(component_key),
                    "tile_id": tile_id,
                    "tile_key": tile_key,
                    "tile_state": tile_state,
                    "polarity": _TILE_POLARITY[tile_state],
                    "evidence_ref": str(
                        tile.get("evidence_ref") or ""
                    ).strip(),
                    "quote": quote,
                    "quote_hash": hashlib.sha256(
                        normalize_evidence_text(quote)
                        .casefold()
                        .encode("utf-8")
                    ).hexdigest(),
                }
            )
    return rows


def _mapping_series(
    report: dict[str, Any],
    *,
    identity_projection: dict[str, Any],
    claim_projection: dict[str, Any],
) -> dict[str, Any]:
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    sv9 = raw.get("sv9") if isinstance(raw.get("sv9"), dict) else {}
    result = (
        sv9.get("result")
        if isinstance(sv9.get("result"), dict)
        else {}
    )
    flow = raw.get("flow") if isinstance(raw.get("flow"), dict) else {}
    debug = (
        flow.get("interpretation_debug")
        if isinstance(flow.get("interpretation_debug"), dict)
        else {}
    )
    source_independence = (
        identity_projection.get("source_independence")
        if isinstance(
            identity_projection.get("source_independence"),
            dict,
        )
        else {}
    )
    material = {
        "mapping_version": EVIDENCE_CLAIM_TILE_MAPPING_VERSION,
        "mapping_policy_version": (
            EVIDENCE_CLAIM_TILE_LEDGER_POLICY_VERSION
        ),
        "identity_policy_version": (
            EVIDENCE_MEMORY_IDENTITY_V2_POLICY_VERSION
        ),
        "claim_memory_policy_version": (
            EVIDENCE_CLAIM_MEMORY_POLICY_VERSION
        ),
        "source_registry_fingerprint": str(
            source_independence.get("registry_fingerprint") or ""
        ),
        "pipeline_version": str(
            raw.get("schema_version") or "unknown"
        ),
        "rubric_version": str(
            result.get("rubric_version")
            or result.get("model")
            or "unknown"
        ),
        "prompt_version": str(
            debug.get("prompt_version") or "unknown"
        ),
        "evaluator_model": str(
            result.get("evaluator_model")
            or sv9.get("evaluator_model")
            or "unknown"
        ),
        "claim_projection_version": str(
            claim_projection.get("schema_version") or ""
        ),
        "identity_projection_version": str(
            identity_projection.get("schema_version") or ""
        ),
    }
    return {
        "mapping_series_id": stable_artifact_digest(
            "evidence-claim-tile-mapping-series-v1",
            material,
        ),
        **material,
        "runtime_effect": False,
        "authority": False,
    }


def _finalize_series(
    accumulators: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for series_id in sorted(accumulators):
        accumulator = accumulators[series_id]
        timeline = sorted(
            (
                {
                    "report_id": report_id,
                    "observed_at": observed_at,
                }
                for report_id, observed_at in accumulator[
                    "observed_at_by_report"
                ].items()
            ),
            key=lambda item: (
                str(item["observed_at"]),
                str(item["report_id"]),
            ),
        )
        rows.append(
            {
                key: value
                for key, value in accumulator.items()
                if key
                not in {"report_ids", "observed_at_by_report"}
            }
            | {
                "first_seen_at": (
                    timeline[0]["observed_at"] if timeline else None
                ),
                "last_seen_at": (
                    timeline[-1]["observed_at"] if timeline else None
                ),
                "latest_report_id": (
                    timeline[-1]["report_id"] if timeline else None
                ),
                "report_count": len(timeline),
                "report_ids": [
                    str(item["report_id"]) for item in timeline
                ],
            }
        )
    return rows


def _finalize_mappings(
    accumulators: dict[str, dict[str, Any]],
    *,
    latest_report_by_series: dict[str, str],
    latest_report_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for mapping_id in sorted(accumulators):
        accumulator = accumulators[mapping_id]
        observations = sorted(
            accumulator["observations_by_report"].values(),
            key=lambda item: (
                str(item["observed_at"]),
                str(item["report_id"]),
            ),
        )
        report_ids = [
            str(observation["report_id"])
            for observation in observations
        ]
        series_latest_report_id = latest_report_by_series.get(
            str(accumulator["mapping_series_id"]),
            "",
        )
        present_in_series_latest = (
            series_latest_report_id in report_ids
        )
        observation_count = len(observations)
        state = (
            "repeated"
            if present_in_series_latest and observation_count > 1
            else "observed"
            if present_in_series_latest
            else "not_reacquired"
        )
        rows.append(
            {
                key: value
                for key, value in accumulator.items()
                if key
                not in {
                    "source_evidence_refs",
                    "match_methods",
                    "quote_hashes",
                    "observations_by_report",
                }
            }
            | {
                "state": state,
                "present_in_series_latest": (
                    present_in_series_latest
                ),
                "present_in_latest_report": (
                    latest_report_id in report_ids
                ),
                "first_seen_at": observations[0]["observed_at"],
                "last_seen_at": observations[-1]["observed_at"],
                "observation_count": observation_count,
                "report_ids": report_ids,
                "source_evidence_refs": sorted(
                    accumulator["source_evidence_refs"]
                ),
                "match_methods": sorted(
                    accumulator["match_methods"]
                ),
                "quote_hashes": sorted(
                    accumulator["quote_hashes"]
                ),
                "observations": observations,
                "runtime_effect": False,
                "authority": False,
            }
        )
    return rows


def _row_evidence_identity(
    row: dict[str, Any],
) -> dict[str, str] | None:
    metadata = (
        row.get("metadata")
        if isinstance(row.get("metadata"), dict)
        else {}
    )
    source = str(row.get("source") or "unknown").strip().lower()
    evidence_type = str(
        row.get("evidence_type") or "unknown"
    ).strip().lower()
    source_class = str(
        metadata.get("source_class")
        or _infer_source_class(source, evidence_type)
    ).strip().lower()
    content = normalize_evidence_text(row.get("content"))
    if source_class not in MATERIAL_SOURCE_CLASSES or not content:
        return None
    url = normalize_evidence_url(row.get("url"))
    return {
        "evidence_id": canonical_evidence_digest(
            source_class=source_class,
            evidence_type=evidence_type,
            url=url,
            content=content,
        ),
        "source_class": source_class,
        "evidence_type": evidence_type,
        "url": url,
    }


def _source_independence_status(
    entry: dict[str, Any],
    *,
    source_class: str,
) -> str:
    if source_class == "owned_copy":
        return "owned_source"
    if source_class != "external_proof":
        return "not_applicable"
    return str(entry.get("independence_status") or "unknown")


def _candidate(report: dict[str, Any]) -> dict[str, Any]:
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    flow = raw.get("flow") if isinstance(raw.get("flow"), dict) else {}
    return (
        flow.get("candidate")
        if isinstance(flow.get("candidate"), dict)
        else {}
    )


def _candidate_evidence_rows(
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    pack = (
        candidate.get("evidence_pack")
        if isinstance(candidate.get("evidence_pack"), dict)
        else {}
    )
    rows = pack.get("evidence")
    return [
        row for row in rows or [] if isinstance(row, dict)
    ]


def _infer_source_class(source: str, evidence_type: str) -> str:
    text = f"{source} {evidence_type}".lower()
    if "visual" in text:
        return "visual_signal"
    if "external" in text or "exa" in text:
        return "external_proof"
    if "derived" in text or "interpret" in text:
        return "derived_strategy"
    if "web" in text or "owned" in text or "raw_input" in text:
        return "owned_copy"
    return "other"


def _ordered_reports(
    reports: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        (report for report in reports if isinstance(report, dict)),
        key=lambda report: (
            _timestamp(report.get("created_at")),
            str(report.get("id") or ""),
        ),
    )


def _timestamp(value: Any) -> datetime:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _domain(value: str) -> str:
    parsed = urlparse(
        value if "://" in value else f"https://{value}"
    )
    return str(parsed.hostname or "").lower().removeprefix("www.")


def _empty_summary() -> dict[str, Any]:
    return {
        "mapping_series_count": 0,
        "mapping_count": 0,
        "mapping_observation_count": 0,
        "current_series_mapping_count": 0,
        "source_evidence_count": 0,
        "claim_variant_count": 0,
        "tile_count": 0,
        "state_counts": {},
        "polarity_counts": {},
        "tile_state_observation_counts": {},
        "diagnostic_counts": {},
    }


def _state_fingerprint(payload: dict[str, Any]) -> str:
    material = {
        "schema_version": payload["schema_version"],
        "policy_version": payload["policy_version"],
        "mapping_version": payload["mapping_version"],
        "mode": payload["mode"],
        "brand": payload["brand"],
        "latest_report_id": payload["latest_report_id"],
        "latest_mapping_series_id": payload[
            "latest_mapping_series_id"
        ],
        "summary": payload["summary"],
        "mapping_series": payload["mapping_series"],
        "mappings": payload["mappings"],
    }
    rendered = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
