"""Non-authoritative longitudinal claim projection for evidence memory."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Iterable
from urllib.parse import urlparse

from src.evidence_identity import (
    canonical_evidence_digest,
    normalize_evidence_text,
    normalize_evidence_url,
    stable_artifact_digest,
)
from src.services.evidence_memory_identity_v2 import (
    build_evidence_memory_identity_v2,
)
from src.services.scanner_evidence_comparison import MATERIAL_SOURCE_CLASSES


EVIDENCE_CLAIM_MEMORY_VERSION = "evidence-claim-memory-v1"
EVIDENCE_CLAIM_MEMORY_POLICY_VERSION = "evidence-claim-memory-policy-v1"
_SLOT_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,199}$")


def build_evidence_claim_memory(
    reports: Iterable[dict[str, Any]],
    *,
    mode: str = "shadow",
    evidence_adjudications: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build a deterministic claim projection from immutable evidence history."""

    ordered = _ordered_reports(reports)
    effective_mode = (
        "shadow" if str(mode).strip().lower() == "shadow" else "disabled"
    )
    latest = ordered[-1] if ordered else {}
    latest_report_id = str(latest.get("id") or "")
    brand_domain = _domain(str(latest.get("url") or ""))
    result: dict[str, Any] = {
        "schema_version": EVIDENCE_CLAIM_MEMORY_VERSION,
        "policy_version": EVIDENCE_CLAIM_MEMORY_POLICY_VERSION,
        "mode": effective_mode,
        "runtime_effect": False,
        "authority": False,
        "brand": {
            "name": str(latest.get("brand_name") or ""),
            "domain": brand_domain,
        },
        "report_count": len(ordered),
        "latest_report_id": latest_report_id or None,
        "policy": {
            "automatic_claim_acceptance": False,
            "automatic_claim_replacement": False,
            "automatic_contradiction": False,
            "content_derived_claim_id_is_stable_slot": False,
            "visual_tile_is_semantic_claim_slot": False,
            "checked_block_is_semantic_claim_slot": False,
            "explicit_stable_slot_declaration_required": True,
            "legacy_claim_id_requires_stable_semantics": True,
        },
        "warnings": [
            "shadow_only_no_scoring_or_selection_effect",
            "claim_relation_candidates_are_not_adjudications",
            "bare_claim_id_is_not_a_longitudinal_slot",
            "raw_claim_text_is_not_returned",
        ],
        "summary": _empty_summary(),
        "slots": [],
        "variants": [],
        "occurrences": [],
    }
    if effective_mode != "shadow" or not ordered:
        result["state_fingerprint"] = _state_fingerprint(result)
        return result

    evidence_projection = build_evidence_memory_identity_v2(
        ordered,
        mode="shadow",
        adjudications=evidence_adjudications,
    )
    evidence_by_id = {
        str(entry.get("evidence_id") or ""): entry
        for entry in evidence_projection.get("entries") or []
        if isinstance(entry, dict) and str(entry.get("evidence_id") or "")
    }

    occurrence_by_id: dict[str, dict[str, Any]] = {}
    variant_accumulators: dict[str, dict[str, Any]] = {}
    slot_accumulators: dict[str, dict[str, Any]] = {}
    ignored_reason_counts: Counter[str] = Counter()
    ignored_bare_claim_ids: set[str] = set()

    for report in ordered:
        report_id = str(report.get("id") or "")
        observed_at = _timestamp(report.get("created_at")).isoformat()
        for row in _evidence_rows(report):
            metadata = (
                row.get("metadata")
                if isinstance(row.get("metadata"), dict)
                else {}
            )
            slot = _explicit_slot(metadata)
            if slot is None:
                explicit_slot_value = normalize_evidence_text(
                    metadata.get("claim_slot_key")
                )
                legacy_claim_id = normalize_evidence_text(
                    metadata.get("claim_id")
                )
                legacy_semantics = str(
                    metadata.get("claim_id_semantics") or ""
                ).strip().lower()
                if explicit_slot_value:
                    ignored_reason_counts["invalid_explicit_claim_slot_key"] += 1
                elif legacy_claim_id and legacy_semantics == "stable_slot":
                    ignored_reason_counts[
                        "invalid_declared_stable_claim_id"
                    ] += 1
                elif legacy_claim_id:
                    ignored_reason_counts["bare_claim_id_without_stable_semantics"] += 1
                    ignored_bare_claim_ids.add(legacy_claim_id)
                elif metadata.get("tile"):
                    ignored_reason_counts["visual_tile_is_not_semantic_claim_slot"] += 1
                elif metadata.get("checked_block"):
                    ignored_reason_counts["checked_block_is_not_semantic_claim_slot"] += 1
                continue
            slot_key, slot_method = slot
            source = str(row.get("source") or "unknown").strip().lower()
            evidence_type = (
                str(row.get("evidence_type") or "unknown").strip().lower()
            )
            source_class = (
                str(
                    metadata.get("source_class")
                    or _infer_source_class(source, evidence_type)
                )
                .strip()
                .lower()
            )
            if source_class not in MATERIAL_SOURCE_CLASSES:
                ignored_reason_counts["unsupported_source_class"] += 1
                continue
            content = normalize_evidence_text(row.get("content"))
            if not content:
                ignored_reason_counts["empty_claim_content"] += 1
                continue
            url = normalize_evidence_url(row.get("url"))
            evidence_id = canonical_evidence_digest(
                source_class=source_class,
                evidence_type=evidence_type,
                url=url,
                content=content,
            )
            evidence_entry = evidence_by_id.get(evidence_id)
            if evidence_entry is None:
                ignored_reason_counts["evidence_identity_not_projected"] += 1
                continue
            entity_scope = _entity_scope(metadata, brand_domain=brand_domain)
            claim_type = _claim_type(metadata)
            claim_slot_id = stable_artifact_digest(
                "evidence-claim-slot-v1",
                {
                    "brand_domain": brand_domain,
                    "entity_scope": entity_scope,
                    "claim_slot_key": slot_key,
                },
            )
            content_hash = str(evidence_entry.get("content_hash") or "")
            claim_variant_id = stable_artifact_digest(
                "evidence-claim-variant-v1",
                {
                    "claim_slot_id": claim_slot_id,
                    "content_hash": content_hash,
                },
            )
            claim_occurrence_id = stable_artifact_digest(
                "evidence-claim-occurrence-v1",
                {
                    "report_id": report_id,
                    "evidence_id": evidence_id,
                    "claim_variant_id": claim_variant_id,
                },
            )
            occurrence = occurrence_by_id.setdefault(
                claim_occurrence_id,
                {
                    "claim_occurrence_id": claim_occurrence_id,
                    "claim_variant_id": claim_variant_id,
                    "claim_slot_id": claim_slot_id,
                    "evidence_id": evidence_id,
                    "report_id": report_id,
                    "observed_at": observed_at,
                    "present_in_latest": report_id == latest_report_id,
                    "duplicate_ref_count": 0,
                },
            )
            occurrence["duplicate_ref_count"] += 1

            variant = variant_accumulators.setdefault(
                claim_variant_id,
                {
                    "claim_variant_id": claim_variant_id,
                    "claim_slot_id": claim_slot_id,
                    "content_hash": content_hash,
                    "evidence_ids": set(),
                    "occurrence_ids": set(),
                    "report_ids": set(),
                    "observed_at": [],
                    "identity_statuses": set(),
                    "adjudication_states": set(),
                    "source_classes": set(),
                },
            )
            variant["evidence_ids"].add(evidence_id)
            variant["occurrence_ids"].add(claim_occurrence_id)
            variant["report_ids"].add(report_id)
            variant["observed_at"].append(observed_at)
            variant["identity_statuses"].add(
                str(evidence_entry.get("identity_status") or "unverified")
            )
            variant["adjudication_states"].add(
                str(evidence_entry.get("adjudication_state") or "proposed")
            )
            variant["source_classes"].add(source_class)

            slot_accumulator = slot_accumulators.setdefault(
                claim_slot_id,
                {
                    "claim_slot_id": claim_slot_id,
                    "claim_slot_key": slot_key,
                    "claim_slot_method": slot_method,
                    "entity_scope": entity_scope,
                    "claim_types": set(),
                    "variant_ids": set(),
                    "report_ids": set(),
                },
            )
            slot_accumulator["claim_types"].add(claim_type)
            slot_accumulator["variant_ids"].add(claim_variant_id)
            slot_accumulator["report_ids"].add(report_id)

    variants = _finalize_variants(
        variant_accumulators,
        latest_report_id=latest_report_id,
    )
    variants_by_id = {
        str(variant["claim_variant_id"]): variant for variant in variants
    }
    slots = _finalize_slots(
        slot_accumulators,
        variants_by_id=variants_by_id,
        latest_report_id=latest_report_id,
    )
    occurrences = sorted(
        occurrence_by_id.values(),
        key=lambda item: (
            str(item["observed_at"]),
            str(item["report_id"]),
            str(item["claim_occurrence_id"]),
        ),
    )
    relation_counts = Counter(
        str(relation.get("relation") or "")
        for slot in slots
        for relation in slot.get("relation_candidates") or []
    )
    variant_state_counts = Counter(
        str(variant.get("state") or "")
        for variant in variants
    )
    result["slots"] = slots
    result["variants"] = variants
    result["occurrences"] = occurrences
    result["summary"] = {
        "claim_slot_count": len(slots),
        "claim_variant_count": len(variants),
        "claim_occurrence_count": len(occurrences),
        "current_claim_variant_count": sum(
            1 for variant in variants if variant["present_in_latest"] is True
        ),
        "multi_variant_slot_count": sum(
            1 for slot in slots if int(slot["variant_count"]) > 1
        ),
        "claim_type_conflict_count": sum(
            1 for slot in slots if slot["claim_type_conflict"] is True
        ),
        "relation_candidate_count": sum(relation_counts.values()),
        "relation_candidate_counts": dict(sorted(relation_counts.items())),
        "variant_state_counts": dict(sorted(variant_state_counts.items())),
        "ignored_claim_metadata_count": sum(ignored_reason_counts.values()),
        "ignored_claim_reason_counts": dict(
            sorted(ignored_reason_counts.items())
        ),
        "ignored_bare_claim_id_count": len(ignored_bare_claim_ids),
        "claim_slot_method_counts": dict(
            sorted(
                Counter(
                    str(slot.get("claim_slot_method") or "")
                    for slot in slots
                ).items()
            )
        ),
    }
    result["state_fingerprint"] = _state_fingerprint(result)
    return result


def _finalize_variants(
    accumulators: dict[str, dict[str, Any]],
    *,
    latest_report_id: str,
) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    for variant_id in sorted(accumulators):
        accumulator = accumulators[variant_id]
        report_ids = sorted(accumulator["report_ids"])
        present_in_latest = latest_report_id in accumulator["report_ids"]
        observation_count = len(accumulator["occurrence_ids"])
        if present_in_latest and observation_count > 1:
            state = "repeated"
        elif present_in_latest:
            state = "observed"
        else:
            state = "not_reacquired"
        observed_at = sorted(accumulator["observed_at"])
        variants.append(
            {
                "claim_variant_id": variant_id,
                "claim_slot_id": str(accumulator["claim_slot_id"]),
                "content_hash": str(accumulator["content_hash"]),
                "state": state,
                "present_in_latest": present_in_latest,
                "first_seen_at": observed_at[0],
                "last_seen_at": observed_at[-1],
                "observation_count": observation_count,
                "report_ids": report_ids,
                "evidence_ids": sorted(accumulator["evidence_ids"]),
                "occurrence_ids": sorted(accumulator["occurrence_ids"]),
                "identity_statuses": sorted(accumulator["identity_statuses"]),
                "adjudication_states": sorted(
                    accumulator["adjudication_states"]
                ),
                "source_classes": sorted(accumulator["source_classes"]),
                "runtime_effect": False,
                "authority": False,
            }
        )
    return variants


def _finalize_slots(
    accumulators: dict[str, dict[str, Any]],
    *,
    variants_by_id: dict[str, dict[str, Any]],
    latest_report_id: str,
) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    for slot_id in sorted(accumulators):
        accumulator = accumulators[slot_id]
        variant_ids = sorted(accumulator["variant_ids"])
        latest_variant_ids = sorted(
            variant_id
            for variant_id in variant_ids
            if latest_report_id in variants_by_id[variant_id]["report_ids"]
        )
        relation_candidates = _relation_candidates(
            variant_ids,
            variants_by_id=variants_by_id,
            latest_variant_ids=latest_variant_ids,
        )
        claim_types = sorted(accumulator["claim_types"])
        claim_type_conflict = len(claim_types) > 1
        slots.append(
            {
                "claim_slot_id": slot_id,
                "claim_slot_key": str(accumulator["claim_slot_key"]),
                "claim_slot_method": str(accumulator["claim_slot_method"]),
                "entity_scope": str(accumulator["entity_scope"]),
                "claim_types": claim_types,
                "claim_type_conflict": claim_type_conflict,
                "variant_ids": variant_ids,
                "latest_variant_ids": latest_variant_ids,
                "variant_count": len(variant_ids),
                "latest_variant_count": len(latest_variant_ids),
                "report_ids": sorted(accumulator["report_ids"]),
                "relation_candidates": relation_candidates,
                "requires_human_review": (
                    bool(relation_candidates) or claim_type_conflict
                ),
                "runtime_effect": False,
                "authority": False,
            }
        )
    return slots


def _relation_candidates(
    variant_ids: list[str],
    *,
    variants_by_id: dict[str, dict[str, Any]],
    latest_variant_ids: list[str],
) -> list[dict[str, Any]]:
    relations: dict[tuple[str, str, str], dict[str, Any]] = {}
    latest_set = set(latest_variant_ids)
    for index, left_id in enumerate(variant_ids):
        left_reports = set(variants_by_id[left_id]["report_ids"])
        for right_id in variant_ids[index + 1 :]:
            right_reports = set(variants_by_id[right_id]["report_ids"])
            if left_reports & right_reports:
                relation = "coexistence_candidate"
                reason_codes = ["variants_observed_in_same_report"]
                from_id, to_id = left_id, right_id
            elif len(latest_set) == 1 and (
                (left_id in latest_set) != (right_id in latest_set)
            ):
                relation = "replacement_candidate"
                reason_codes = [
                    "one_historical_variant_and_one_latest_variant",
                    "semantic_replacement_not_assumed",
                ]
                from_id = right_id if left_id in latest_set else left_id
                to_id = left_id if left_id in latest_set else right_id
            else:
                continue
            key = (relation, from_id, to_id)
            relations[key] = {
                "relation_candidate_id": stable_artifact_digest(
                    "evidence-claim-relation-candidate-v1",
                    {
                        "relation": relation,
                        "from_claim_variant_id": from_id,
                        "to_claim_variant_id": to_id,
                    },
                ),
                "relation": relation,
                "from_claim_variant_id": from_id,
                "to_claim_variant_id": to_id,
                "reason_codes": reason_codes,
                "adjudication_state": "proposed",
                "runtime_effect": False,
                "authority": False,
            }
    return [
        relations[key]
        for key in sorted(relations)
    ]


def _explicit_slot(metadata: dict[str, Any]) -> tuple[str, str] | None:
    explicit = _normalize_slot_key(metadata.get("claim_slot_key"))
    if explicit:
        return explicit, "explicit_claim_slot_key"
    semantics = str(metadata.get("claim_id_semantics") or "").strip().lower()
    legacy = _normalize_slot_key(metadata.get("claim_id"))
    if legacy and semantics == "stable_slot":
        return legacy, "declared_stable_claim_id"
    return None


def _normalize_slot_key(value: Any) -> str:
    normalized = normalize_evidence_text(value).casefold()
    return normalized if _SLOT_KEY_PATTERN.fullmatch(normalized) else ""


def _entity_scope(metadata: dict[str, Any], *, brand_domain: str) -> str:
    value = normalize_evidence_text(metadata.get("entity_scope")).casefold()
    return value[:200] if value else f"brand:{brand_domain}"


def _claim_type(metadata: dict[str, Any]) -> str:
    value = normalize_evidence_text(metadata.get("claim_type")).casefold()
    return value[:100] if value else "unknown"


def _evidence_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    flow = raw.get("flow") if isinstance(raw.get("flow"), dict) else {}
    candidate = (
        flow.get("candidate")
        if isinstance(flow.get("candidate"), dict)
        else {}
    )
    pack = (
        candidate.get("evidence_pack")
        if isinstance(candidate.get("evidence_pack"), dict)
        else {}
    )
    rows = (
        pack.get("evidence")
        if isinstance(pack.get("evidence"), list)
        else []
    )
    return [row for row in rows if isinstance(row, dict)]


def _infer_source_class(source: str, evidence_type: str) -> str:
    if evidence_type.startswith("visual_") or source in {
        "visual_signature",
        "visual_acquisition",
        "screenshot_capture",
    }:
        return "visual_signal"
    if evidence_type.startswith("external_proof.") or source in {
        "exa",
        "searchapi",
        "github",
    }:
        return "external_proof"
    if evidence_type == "raw_input" or source in {"web", "context"}:
        return "owned_copy"
    return "other"


def _ordered_reports(
    reports: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for report in reports:
        if not isinstance(report, dict):
            continue
        report_id = str(report.get("id") or "").strip()
        if report_id:
            by_id[report_id] = dict(report)
    return sorted(
        by_id.values(),
        key=lambda report: (
            _timestamp(report.get("created_at")),
            str(report.get("id") or ""),
        ),
    )


def _domain(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return str(parsed.hostname or "").strip(".").lower().removeprefix("www.")


def _timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _empty_summary() -> dict[str, Any]:
    return {
        "claim_slot_count": 0,
        "claim_variant_count": 0,
        "claim_occurrence_count": 0,
        "current_claim_variant_count": 0,
        "multi_variant_slot_count": 0,
        "claim_type_conflict_count": 0,
        "relation_candidate_count": 0,
        "relation_candidate_counts": {},
        "variant_state_counts": {},
        "ignored_claim_metadata_count": 0,
        "ignored_claim_reason_counts": {},
        "ignored_bare_claim_id_count": 0,
        "claim_slot_method_counts": {},
    }


def _state_fingerprint(payload: dict[str, Any]) -> str:
    rendered = json.dumps(
        {
            "schema_version": payload["schema_version"],
            "policy_version": payload["policy_version"],
            "mode": payload["mode"],
            "brand": payload["brand"],
            "latest_report_id": payload["latest_report_id"],
            "summary": payload["summary"],
            "slots": payload["slots"],
            "variants": payload["variants"],
            "occurrences": payload["occurrences"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
