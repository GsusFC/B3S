"""Document/passage identity projection for evidence-memory Gate 1.

This shadow projection fixes one specific v1 error: several passages from one
document are not temporal revisions of one another. Revision proposals require
an explicit stable claim slot; URL equality alone never implies brand change.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable
from urllib.parse import urlparse

from src.evidence_identity import (
    canonical_evidence_digest,
    normalize_evidence_text,
    normalize_evidence_url,
    stable_artifact_digest,
)
from src.external_identity_provenance import (
    verify_external_identity_provenance,
)
from src.services.evidence_memory_adjudication import (
    apply_evidence_memory_adjudications,
)
from src.services.evidence_source_independence import (
    assign_external_source_independence,
    content_shingle_hashes,
    normalize_distribution_types,
    source_independence_contract,
)
from src.services.scanner_evidence_comparison import (
    MATERIAL_SOURCE_CLASSES,
    build_evidence_snapshot,
)


EVIDENCE_MEMORY_IDENTITY_V2_VERSION = "evidence-memory-identity-v2"
EVIDENCE_MEMORY_IDENTITY_V2_POLICY_VERSION = "evidence-memory-identity-policy-v3"
_CURRENT_STATES = {"observed", "repeated", "validation_candidate"}
_GOOD_ACQUISITION_STATES = {"pass", "warning"}
_TTL_DAYS = {
    "owned_copy": 180,
    "external_proof": 90,
    "visual_signal": 30,
    "derived_strategy": 90,
    "other": 90,
}


def build_evidence_memory_identity_v2(
    reports: Iterable[dict[str, Any]],
    *,
    mode: str = "shadow",
    adjudications: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build a deterministic non-authoritative document/passage projection."""

    ordered = _ordered_reports(reports)
    effective_mode = "shadow" if str(mode).strip().lower() == "shadow" else "disabled"
    latest = ordered[-1] if ordered else {}
    latest_report_id = str(latest.get("id") or "")
    latest_at = _timestamp(latest.get("created_at"))
    brand_domain = _domain(str(latest.get("url") or ""))
    result: dict[str, Any] = {
        "schema_version": EVIDENCE_MEMORY_IDENTITY_V2_VERSION,
        "policy_version": EVIDENCE_MEMORY_IDENTITY_V2_POLICY_VERSION,
        "mode": effective_mode,
        "runtime_effect": False,
        "authority": False,
        "brand": {
            "name": str(latest.get("brand_name") or ""),
            "domain": brand_domain,
        },
        "report_count": len(ordered),
        "latest_report_id": latest_report_id or None,
        "source_independence": source_independence_contract(),
        "policy": {
            "automatic_validation": False,
            "automatic_rejection": False,
            "automatic_claim_replacement": False,
            "url_equality_implies_revision": False,
            "llm_only_identity_is_eligible": False,
            "brand_name_only_external_identity_is_eligible": False,
            "upstream_identity_label_is_eligible": False,
            "reproducible_external_identity_is_eligible": True,
            "exact_syndication_clustering": True,
            "same_publisher_is_one_independence_cluster": True,
            "same_publisher_group_is_one_independence_cluster": True,
            "canonical_source_lineage_clustering": True,
            "deterministic_shingle_similarity_clustering": True,
            "unknown_source_relationship_is_not_independence": True,
            "confirmed_independence_required_for_future_corroboration": True,
        },
        "warnings": [
            "shadow_only_no_scoring_or_selection_effect",
            "passages_from_one_document_are_not_revisions",
            "revision_requires_explicit_claim_slot",
            "external_brand_name_match_requires_adjudication",
            "external_identity_requires_reproducible_provenance",
            "unreviewed_publisher_ownership_remains_unknown",
            "ambiguous_source_independence_requires_human_review",
            "source_independence_has_no_current_corroboration_or_scoring_effect",
            "adjudications_do_not_change_scoring_or_canonical_selection",
        ],
        "summary": _empty_summary(),
        "entries": [],
    }
    if effective_mode != "shadow" or not ordered:
        result = apply_evidence_memory_adjudications(result, adjudications)
        result["state_fingerprint"] = _state_fingerprint(result)
        return result

    atoms: dict[str, dict[str, Any]] = {}
    observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    latest_evidence_ids: set[str] = set()
    latest_by_claim_slot: dict[str, set[str]] = defaultdict(set)
    passages_by_document: dict[str, set[str]] = defaultdict(set)
    latest_passages_by_document: dict[str, set[str]] = defaultdict(set)
    evidence_by_claim_slot: dict[str, set[str]] = defaultdict(set)
    claim_slot_methods: dict[str, str] = {}

    for report in ordered:
        report_id = str(report.get("id") or "")
        observed_at = _timestamp(report.get("created_at")).isoformat()
        snapshot = build_evidence_snapshot(report)
        grouped: dict[str, dict[str, Any]] = {}
        for row in _evidence_rows(report):
            atom = _atom_from_row(row, brand_domain=brand_domain)
            if atom is None:
                continue
            evidence_id = str(atom["evidence_id"])
            existing_atom = atoms.setdefault(evidence_id, atom)
            if existing_atom is not atom:
                _merge_source_independence_metadata(existing_atom, atom)
            group = grouped.setdefault(
                evidence_id,
                {
                    "atom": atom,
                    "identity_statuses": [],
                    "identity_reason_codes": set(),
                    "deterministic_identity_matches": set(),
                    "llm_identity_matches": set(),
                    "external_identity_provenance": {},
                    "ref_count": 0,
                },
            )
            identity_status, identity_reasons = _row_identity_status(
                row,
                source_class=str(atom["source_class"]),
                source_domain=str(atom["source_domain"]),
                brand_domain=brand_domain,
            )
            group["identity_statuses"].append(identity_status)
            group["identity_reason_codes"].update(identity_reasons)
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            deterministic = str(metadata.get("identity_match") or "").strip().lower()
            llm = str(metadata.get("identity_match_llm") or "").strip().lower()
            if deterministic:
                group["deterministic_identity_matches"].add(deterministic)
            if llm:
                group["llm_identity_matches"].add(llm)
            provenance = _external_identity_provenance_projection(
                metadata.get("external_identity_provenance")
            )
            if provenance:
                group["external_identity_provenance"][
                    json.dumps(
                        provenance,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ] = provenance
            group["ref_count"] += 1

        for evidence_id, group in grouped.items():
            atom = group["atom"]
            identity_status = _merge_identity_statuses(group["identity_statuses"])
            validation_eligible = (
                identity_status == "eligible"
                and not snapshot.invalid
                and snapshot.acquisition_state in _GOOD_ACQUISITION_STATES
            )
            observation = {
                "report_id": report_id,
                "observed_at": observed_at,
                "identity_status": identity_status,
                "identity_reason_codes": sorted(group["identity_reason_codes"]),
                "deterministic_identity_matches": sorted(
                    group["deterministic_identity_matches"]
                ),
                "llm_identity_matches": sorted(group["llm_identity_matches"]),
                "external_identity_provenance": [
                    group["external_identity_provenance"][key]
                    for key in sorted(group["external_identity_provenance"])
                ],
                "validation_eligible": validation_eligible,
                "acquisition_state": snapshot.acquisition_state,
                "invalid": snapshot.invalid,
                "duplicate_ref_count": int(group["ref_count"]),
            }
            observations[evidence_id].append(observation)
            document_id = str(atom["document_id"])
            passage_id = str(atom["passage_id"])
            passages_by_document[document_id].add(passage_id)
            claim_slot_id = str(atom.get("claim_slot_id") or "")
            if claim_slot_id:
                evidence_by_claim_slot[claim_slot_id].add(evidence_id)
                claim_slot_methods[claim_slot_id] = str(
                    atom.get("claim_slot_method") or ""
                )
            if report_id == latest_report_id:
                latest_evidence_ids.add(evidence_id)
                latest_passages_by_document[document_id].add(passage_id)
                if claim_slot_id:
                    latest_by_claim_slot[claim_slot_id].add(evidence_id)

    entries: list[dict[str, Any]] = []
    for evidence_id in sorted(atoms):
        atom = atoms[evidence_id]
        atom_observations = sorted(
            observations[evidence_id],
            key=lambda item: (str(item["observed_at"]), str(item["report_id"])),
        )
        last_seen = _timestamp(atom_observations[-1]["observed_at"])
        present_in_latest = evidence_id in latest_evidence_ids
        age_days = max(0, (latest_at - last_seen).days)
        ttl_days = _TTL_DAYS.get(
            str(atom["source_class"]),
            _TTL_DAYS["other"],
        )
        qualified_count = sum(
            1
            for observation in atom_observations
            if observation["validation_eligible"] is True
        )
        identity_status = _merge_identity_statuses(
            [str(item["identity_status"]) for item in atom_observations]
        )
        claim_slot_id = str(atom.get("claim_slot_id") or "")
        state, state_reasons = _entry_state(
            observation_count=len(atom_observations),
            qualified_observation_count=qualified_count,
            identity_status=identity_status,
            present_in_latest=present_in_latest,
            claim_slot_id=claim_slot_id,
            latest_claim_slot_evidence=latest_by_claim_slot.get(claim_slot_id, set()),
            age_days=age_days,
            ttl_days=ttl_days,
        )
        document_id = str(atom["document_id"])
        entry = {
            **atom,
            "state": state,
            "state_reason_codes": state_reasons,
            "identity_status": identity_status,
            "adjudication_state": "proposed",
            "present_in_latest": present_in_latest,
            "first_seen_at": str(atom_observations[0]["observed_at"]),
            "last_seen_at": str(atom_observations[-1]["observed_at"]),
            "age_days": age_days,
            "ttl_days": ttl_days,
            "observation_count": len(atom_observations),
            "qualified_observation_count": qualified_count,
            "report_ids": [str(item["report_id"]) for item in atom_observations],
            "document_passage_count": len(passages_by_document[document_id]),
            "document_latest_passage_count": len(
                latest_passages_by_document.get(document_id, set())
            ),
            "claim_slot_variant_count": (
                len(evidence_by_claim_slot[claim_slot_id])
                if claim_slot_id
                else 0
            ),
            "observations": atom_observations,
        }
        entries.append(entry)

    current_entries = [
        entry for entry in entries if str(entry["state"]) in _CURRENT_STATES
    ]
    independence_summary = assign_external_source_independence(
        entries,
        current_evidence_ids={
            str(entry["evidence_id"])
            for entry in current_entries
        },
    )
    state_counts = Counter(str(entry["state"]) for entry in entries)
    identity_counts = Counter(str(entry["identity_status"]) for entry in entries)
    source_counts = Counter(str(entry["source_class"]) for entry in current_entries)
    current_external = [
        entry
        for entry in current_entries
        if entry["source_class"] == "external_proof"
    ]
    result["entries"] = entries
    result["summary"] = {
        "entry_count": len(entries),
        "document_count": len(passages_by_document),
        "passage_count": len(
            {
                str(entry["passage_id"])
                for entry in entries
            }
        ),
        "claim_slot_count": len(evidence_by_claim_slot),
        "claim_slot_method_counts": dict(
            sorted(Counter(claim_slot_methods.values()).items())
        ),
        "multi_passage_document_count": sum(
            1 for passages in passages_by_document.values() if len(passages) > 1
        ),
        "current_entry_count": len(current_entries),
        "revision_candidate_count": state_counts.get("revision_candidate", 0),
        "state_counts": dict(sorted(state_counts.items())),
        "identity_status_counts": dict(sorted(identity_counts.items())),
        "current_source_class_counts": dict(sorted(source_counts.items())),
        "current_external_publisher_count": len(
            {
                str(entry["publisher_id"])
                for entry in current_external
                if str(entry.get("publisher_id") or "")
            }
        ),
        "current_external_syndication_cluster_count": len(
            {
                str(entry["syndication_cluster_id"])
                for entry in current_external
                if str(entry.get("syndication_cluster_id") or "")
            }
        ),
        "current_independent_external_cluster_count": int(
            independence_summary[
                "current_confirmed_independent_external_cluster_count"
            ]
        ),
        **independence_summary,
    }
    result = apply_evidence_memory_adjudications(result, adjudications)
    result["state_fingerprint"] = _state_fingerprint(result)
    return result


def _atom_from_row(
    row: dict[str, Any],
    *,
    brand_domain: str,
) -> dict[str, Any] | None:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    source = str(row.get("source") or "unknown").strip().lower()
    evidence_type = str(row.get("evidence_type") or "unknown").strip().lower()
    source_class = str(
        metadata.get("source_class")
        or _infer_source_class(source, evidence_type)
    ).strip().lower()
    if source_class not in MATERIAL_SOURCE_CLASSES:
        return None
    url = normalize_evidence_url(row.get("url"))
    content = normalize_evidence_text(row.get("content"))
    if not content:
        return None
    content_hash = stable_artifact_digest(
        "evidence-memory-content-v2",
        {"content": content.casefold()},
    )
    evidence_id = canonical_evidence_digest(
        source_class=source_class,
        evidence_type=evidence_type,
        url=url,
        content=content,
    )
    document_basis = (
        {"kind": "url", "source_class": source_class, "url": url}
        if url
        else {
            "kind": "source_surface",
            "source_class": source_class,
            "source": source,
            "evidence_type": evidence_type,
            "capture": str(metadata.get("capture") or ""),
        }
    )
    document_id = stable_artifact_digest(
        "evidence-memory-document-v2",
        document_basis,
    )
    passage_id = stable_artifact_digest(
        "evidence-memory-passage-v2",
        {
            "document_id": document_id,
            "evidence_type": evidence_type,
            "content_hash": content_hash,
        },
    )
    claim_slot_id, claim_slot_method = _claim_slot(
        metadata,
        document_id=document_id,
        evidence_type=evidence_type,
    )
    source_domain = _domain(url)
    canonical_urls = _normalized_urls(
        (
            url,
            metadata.get("canonical_url"),
            metadata.get("canonical_source_url"),
        )
    )
    original_source_urls = _normalized_urls(
        (
            metadata.get("original_source_url"),
            metadata.get("syndication_source_url"),
            metadata.get("origin_url"),
        )
    )
    distribution_types = normalize_distribution_types(
        (
            metadata.get("distribution_type"),
            metadata.get("source_type"),
            metadata.get("syndication_type"),
        )
    )
    publisher_id = (
        stable_artifact_digest(
            "evidence-memory-publisher-v2",
            {"source_domain": source_domain},
        )
        if source_class == "external_proof" and source_domain
        else ""
    )
    syndication_cluster_id = (
        stable_artifact_digest(
            "evidence-memory-syndication-v2",
            {"content_hash": content_hash},
        )
        if source_class == "external_proof"
        else ""
    )
    return {
        "evidence_id": evidence_id,
        "document_id": document_id,
        "passage_id": passage_id,
        "claim_slot_id": claim_slot_id,
        "claim_slot_method": claim_slot_method,
        "source": source,
        "source_class": source_class,
        "evidence_type": evidence_type,
        "url": url,
        "source_domain": source_domain,
        "brand_domain": brand_domain,
        "content_hash": content_hash,
        "publisher_id": publisher_id,
        "syndication_cluster_id": syndication_cluster_id,
        "canonical_urls": canonical_urls,
        "original_source_urls": original_source_urls,
        "distribution_types": distribution_types,
        "_source_independence_shingles": content_shingle_hashes(content),
    }


def _claim_slot(
    metadata: dict[str, Any],
    *,
    document_id: str,
    evidence_type: str,
) -> tuple[str, str]:
    explicit_claim_id = str(metadata.get("claim_id") or "").strip()
    if explicit_claim_id:
        return (
            stable_artifact_digest(
                "evidence-memory-claim-slot-v2",
                {
                    "method": "explicit_claim_id",
                    "document_id": document_id,
                    "claim_id": explicit_claim_id,
                },
            ),
            "explicit_claim_id",
        )
    tile = str(metadata.get("tile") or "").strip()
    if tile:
        return (
            stable_artifact_digest(
                "evidence-memory-claim-slot-v2",
                {
                    "method": "visual_tile",
                    "document_id": document_id,
                    "tile": tile,
                },
            ),
            "visual_tile",
        )
    checked_block = str(metadata.get("checked_block") or "").strip()
    if checked_block:
        return (
            stable_artifact_digest(
                "evidence-memory-claim-slot-v2",
                {
                    "method": "checked_block",
                    "document_id": document_id,
                    "checked_block": checked_block,
                },
            ),
            "checked_block",
        )
    if evidence_type.startswith("acquisition.absence."):
        return (
            stable_artifact_digest(
                "evidence-memory-claim-slot-v2",
                {
                    "method": "absence_type",
                    "document_id": document_id,
                    "evidence_type": evidence_type,
                },
            ),
            "absence_type",
        )
    return "", ""


def _row_identity_status(
    row: dict[str, Any],
    *,
    source_class: str,
    source_domain: str,
    brand_domain: str,
) -> tuple[str, list[str]]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    deterministic = str(metadata.get("identity_match") or "").strip().lower()
    llm = str(metadata.get("identity_match_llm") or "").strip().lower()
    if source_class == "owned_copy":
        if _same_brand_domain(source_domain, brand_domain):
            return "eligible", ["owned_document_matches_brand_domain"]
        return "mismatch", ["owned_document_outside_brand_domain"]
    if source_class != "external_proof":
        return "unverified", ["source_class_not_identity_validated"]
    provenance = metadata.get("external_identity_provenance")
    reproduced, provenance_reasons = verify_external_identity_provenance(
        provenance,
        brand_domain=brand_domain,
        source_domain=source_domain,
        content=normalize_evidence_text(row.get("content")),
    )
    if reproduced:
        if deterministic == "none" or llm == "none":
            return "disputed", [
                *provenance_reasons,
                "reproduced_external_identity_label_disagrees",
            ]
        return "eligible", provenance_reasons
    if deterministic == "none":
        return "mismatch", [
            "deterministic_identity_mismatch",
            *provenance_reasons,
        ]
    if isinstance(provenance, dict):
        return "unverified", provenance_reasons
    if deterministic == "domain":
        return "unverified", [
            "upstream_domain_label_not_independently_reproduced"
        ]
    if deterministic == "brand_name":
        return "unverified", ["brand_name_match_requires_adjudication"]
    if llm in {"domain", "brand_name"}:
        return "unverified", ["llm_only_identity_match_not_eligible"]
    if llm == "none":
        return "unverified", ["llm_only_identity_mismatch_not_authoritative"]
    return "unverified", ["identity_not_established"]


def _external_identity_provenance_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    fields = (
        "schema_version",
        "policy_version",
        "provider",
        "subject_domain",
        "source_domain",
        "matched_alias",
        "match_method",
        "match_score",
        "collector_source_class",
        "collector_relation",
        "requires_human_review",
        "candidate_strength",
    )
    return {
        field: value.get(field)
        for field in fields
        if field in value
    }


def _merge_identity_statuses(statuses: Iterable[str]) -> str:
    values = {str(status) for status in statuses if str(status)}
    if values == {"eligible"}:
        return "eligible"
    if "disputed" in values or ("eligible" in values and len(values) > 1):
        return "disputed"
    if values == {"mismatch"}:
        return "mismatch"
    return "unverified"


def _entry_state(
    *,
    observation_count: int,
    qualified_observation_count: int,
    identity_status: str,
    present_in_latest: bool,
    claim_slot_id: str,
    latest_claim_slot_evidence: set[str],
    age_days: int,
    ttl_days: int,
) -> tuple[str, list[str]]:
    if present_in_latest:
        if (
            observation_count >= 2
            and qualified_observation_count >= 2
            and identity_status == "eligible"
        ):
            return "validation_candidate", [
                "exact_passage_repeated",
                "identity_and_acquisition_eligible",
                "shadow_candidate_only",
            ]
        if observation_count >= 2:
            return "repeated", [
                "exact_passage_repeated",
                "validation_eligibility_incomplete",
            ]
        return "observed", ["first_exact_passage_observation"]
    if claim_slot_id and latest_claim_slot_evidence:
        return "revision_candidate", [
            "explicit_claim_slot_has_new_passage",
            "semantic_replacement_not_assumed",
        ]
    if age_days >= ttl_days:
        return "stale_candidate", [
            "passage_not_present_in_latest_capture",
            "shadow_ttl_elapsed",
            "automatic_retirement_disabled",
        ]
    return "not_reacquired", [
        "passage_not_present_in_latest_capture",
        "same_document_new_passage_is_not_change",
    ]


def _evidence_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    flow = raw.get("flow") if isinstance(raw.get("flow"), dict) else {}
    candidate = flow.get("candidate") if isinstance(flow.get("candidate"), dict) else {}
    pack = candidate.get("evidence_pack") if isinstance(candidate.get("evidence_pack"), dict) else {}
    evidence_rows = (
        pack.get("evidence")
        if isinstance(pack.get("evidence"), list)
        else []
    )
    claim_rows = (
        candidate.get("claim_memory_evidence")
        if isinstance(candidate.get("claim_memory_evidence"), list)
        else []
    )
    return [
        row
        for row in [*evidence_rows, *claim_rows]
        if isinstance(row, dict)
    ]


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


def _same_brand_domain(source_domain: str, brand_domain: str) -> bool:
    return bool(
        source_domain
        and brand_domain
        and (
            source_domain == brand_domain
            or source_domain.endswith(f".{brand_domain}")
        )
    )


def _domain(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return str(parsed.hostname or "").strip(".").lower().removeprefix("www.")


def _normalized_urls(values: Iterable[Any]) -> list[str]:
    return sorted(
        {
            normalized
            for value in values
            if (normalized := normalize_evidence_url(value))
        }
    )


def _merge_source_independence_metadata(
    target: dict[str, Any],
    candidate: dict[str, Any],
) -> None:
    for key in (
        "canonical_urls",
        "original_source_urls",
        "distribution_types",
        "_source_independence_shingles",
    ):
        target[key] = sorted(
            {
                str(value)
                for value in (
                    list(target.get(key) or [])
                    + list(candidate.get(key) or [])
                )
                if str(value)
            }
        )


def _empty_summary() -> dict[str, Any]:
    return {
        "entry_count": 0,
        "document_count": 0,
        "passage_count": 0,
        "claim_slot_count": 0,
        "claim_slot_method_counts": {},
        "multi_passage_document_count": 0,
        "current_entry_count": 0,
        "revision_candidate_count": 0,
        "state_counts": {},
        "identity_status_counts": {},
        "current_source_class_counts": {},
        "current_external_publisher_count": 0,
        "current_external_syndication_cluster_count": 0,
        "current_independent_external_cluster_count": 0,
        "current_external_cluster_count": 0,
        "current_confirmed_independent_external_cluster_count": 0,
        "current_external_independence_status_counts": {},
        "current_external_independence_cluster_status_counts": {},
        "adjudicated_entry_count": 0,
        "adjudication_state_counts": {},
    }


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


def _state_fingerprint(payload: dict[str, Any]) -> str:
    rendered = json.dumps(
        {
            "schema_version": payload["schema_version"],
            "policy_version": payload["policy_version"],
            "mode": payload["mode"],
            "brand": payload["brand"],
            "latest_report_id": payload["latest_report_id"],
            "source_independence": payload["source_independence"],
            "summary": payload["summary"],
            "entries": payload["entries"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
