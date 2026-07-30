"""Claim-scoped corroboration review set over real captured sources."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from src.services.evidence_review_packet import (
    EVIDENCE_REVIEW_PACKET_SCHEMA_VERSION,
    review_packet_fingerprint,
)


EVIDENCE_CLAIM_CORROBORATION_REVIEW_SCHEMA_VERSION = (
    "evidence-claim-corroboration-review-v1"
)
EVIDENCE_CLAIM_CORROBORATION_REVIEW_EVENT_SCHEMA_VERSION = (
    "evidence-claim-corroboration-review-event-v1"
)
EVIDENCE_CLAIM_CORROBORATION_REVIEW_SELECTION_SCHEMA_VERSION = (
    "evidence-claim-corroboration-selection-v1"
)
EVIDENCE_CLAIM_CORROBORATION_REVIEW_DATASET_VERSION = (
    "evidence-claim-corroboration-review-2026-07-v1"
)
CORROBORATION_DECISIONS = frozenset(
    {
        "independently_corroborated",
        "mixed",
        "disputed",
        "excluded",
    }
)
CORROBORATION_BASES = frozenset(
    {
        "editorial_observation",
        "independent_testing",
        "third_party_documentation",
        "first_party_statement",
        "first_party_material",
        "mixed_provenance",
        "absence_check",
        "unresolved",
    }
)
_INDEPENDENT_BASES = frozenset(
    {
        "editorial_observation",
        "independent_testing",
        "third_party_documentation",
        "absence_check",
    }
)
_FIRST_PARTY_BASES = frozenset(
    {"first_party_statement", "first_party_material"}
)
DEFAULT_REVIEW_ROOT = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "evidence_claim_corroboration_review"
    / "v1"
)
DEFAULT_SOURCE_REVIEW_ROOT = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "evidence_source_review"
    / "v1"
)
DEFAULT_CAPTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "vercel"
    / "vercel_fresh_capture_envelope.json"
)


class EvidenceClaimCorroborationReviewSetError(ValueError):
    """The claim-corroboration review dataset violates its contract."""


def load_review_manifest(path: Path | None = None) -> dict[str, Any]:
    target = path or DEFAULT_REVIEW_ROOT / "manifest.json"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceClaimCorroborationReviewSetError(
            f"cannot load claim-corroboration manifest: {target}"
        ) from exc
    if not isinstance(payload, dict):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim-corroboration manifest must be an object"
        )
    if (
        payload.get("schema_version")
        != EVIDENCE_CLAIM_CORROBORATION_REVIEW_SCHEMA_VERSION
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "unsupported claim-corroboration schema version"
        )
    if (
        payload.get("review_event_schema_version")
        != EVIDENCE_CLAIM_CORROBORATION_REVIEW_EVENT_SCHEMA_VERSION
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "unsupported claim-corroboration event schema version"
        )
    if (
        payload.get("dataset_version")
        != EVIDENCE_CLAIM_CORROBORATION_REVIEW_DATASET_VERSION
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "unsupported claim-corroboration dataset version"
        )
    if (
        payload.get("selection_schema_version")
        != EVIDENCE_CLAIM_CORROBORATION_REVIEW_SELECTION_SCHEMA_VERSION
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "unsupported claim-corroboration selection schema"
        )
    for field in (
        "candidate_fingerprint",
        "review_packet_fingerprint",
        "selection_fingerprint",
        "capture_artifact_sha256",
    ):
        _required_sha256(payload.get(field), field=field)
    if (
        payload.get("review_packet_schema_version")
        != EVIDENCE_REVIEW_PACKET_SCHEMA_VERSION
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "unsupported claim-corroboration review packet schema"
        )
    review_event_fingerprint = payload.get("review_event_fingerprint")
    if review_event_fingerprint is not None:
        _required_sha256(
            review_event_fingerprint,
            field="review_event_fingerprint",
        )
    return payload


def load_review_selections(
    path: Path | None = None,
) -> list[dict[str, Any]]:
    target = path or DEFAULT_REVIEW_ROOT / "selections.jsonl"
    rows = _load_jsonl(target, label="claim-corroboration selection")
    seen: set[str] = set()
    for row in rows:
        _validate_selection(row)
        case_id = str(row["case_id"])
        if case_id in seen:
            raise EvidenceClaimCorroborationReviewSetError(
                f"duplicate claim-corroboration selection: {case_id}"
            )
        seen.add(case_id)
    return sorted(rows, key=lambda row: str(row["case_id"]))


def load_review_candidates(
    path: Path | None = None,
) -> list[dict[str, Any]]:
    target = path or DEFAULT_REVIEW_ROOT / "candidates.jsonl"
    rows = _load_jsonl(target, label="claim-corroboration candidate")
    seen: set[str] = set()
    for row in rows:
        _validate_candidate(row)
        case_id = str(row["case_id"])
        if case_id in seen:
            raise EvidenceClaimCorroborationReviewSetError(
                f"duplicate claim-corroboration candidate: {case_id}"
            )
        seen.add(case_id)
    return sorted(rows, key=lambda row: str(row["case_id"]))


def load_review_events(
    path: Path | None = None,
) -> list[dict[str, Any]]:
    target = path or DEFAULT_REVIEW_ROOT / "review_events.jsonl"
    if not target.is_file():
        return []
    rows = _load_jsonl(target, label="claim-corroboration event")
    seen: set[str] = set()
    candidate_fingerprints: set[str] = set()
    review_packet_fingerprints: set[str] = set()
    for row in rows:
        _validate_event_shape(row)
        event_id = str(row["event_id"])
        if event_id in seen:
            raise EvidenceClaimCorroborationReviewSetError(
                f"duplicate claim-corroboration event: {event_id}"
            )
        seen.add(event_id)
        candidate_fingerprints.add(
            str(row["candidate_fingerprint"])
        )
        review_packet_fingerprints.add(
            str(row["review_packet_fingerprint"])
        )
    if len(candidate_fingerprints) > 1:
        raise EvidenceClaimCorroborationReviewSetError(
            "claim-corroboration events mix candidate fingerprints"
        )
    if len(review_packet_fingerprints) > 1:
        raise EvidenceClaimCorroborationReviewSetError(
            "claim-corroboration events mix review packet fingerprints"
        )
    return sorted(
        rows,
        key=lambda row: (str(row["case_id"]), int(row["sequence"])),
    )


def load_source_candidates(
    path: Path | None = None,
) -> list[dict[str, Any]]:
    target = path or DEFAULT_SOURCE_REVIEW_ROOT / "candidates.jsonl"
    return _load_jsonl(target, label="source-review candidate")


def build_review_candidates(
    selections: Iterable[dict[str, Any]],
    source_candidates: Iterable[dict[str, Any]],
    capture: dict[str, Any],
    *,
    capture_artifact_path: str,
    capture_artifact_sha256: str,
) -> list[dict[str, Any]]:
    """Bind literal selections to reproducible offsets in a real capture."""

    selection_rows = [dict(row) for row in selections]
    source_rows = [dict(row) for row in source_candidates]
    for row in selection_rows:
        _validate_selection(row)
    source_by_case_id = {
        str(row.get("case_id") or ""): row
        for row in source_rows
        if str(row.get("case_id") or "")
    }
    required_source_urls: set[str] = set()
    for selection in selection_rows:
        parent_case_id = str(selection["parent_source_case_id"])
        source_candidate = source_by_case_id.get(parent_case_id)
        source = (
            source_candidate.get("source")
            if isinstance(source_candidate, dict)
            else None
        )
        source_url = (
            str(source.get("url") or "")
            if isinstance(source, dict)
            else ""
        )
        if not source_url:
            raise EvidenceClaimCorroborationReviewSetError(
                f"unknown or incomplete parent source case: {parent_case_id}"
            )
        required_source_urls.add(source_url)
    captured_text_by_url = _captured_text_by_url(
        capture,
        required_urls=required_source_urls,
    )
    artifact_hash = _required_sha256(
        capture_artifact_sha256,
        field="capture_artifact_sha256",
    )
    artifact_path = str(capture_artifact_path or "").strip()
    if not artifact_path or Path(artifact_path).is_absolute():
        raise EvidenceClaimCorroborationReviewSetError(
            "capture_artifact_path must be a relative path"
        )

    candidates: list[dict[str, Any]] = []
    for selection in sorted(
        selection_rows,
        key=lambda row: str(row["case_id"]),
    ):
        parent_case_id = str(selection["parent_source_case_id"])
        source_candidate = source_by_case_id.get(parent_case_id)
        if source_candidate is None:
            raise EvidenceClaimCorroborationReviewSetError(
                f"unknown parent source case: {parent_case_id}"
            )
        source = source_candidate.get("source")
        brand = source_candidate.get("brand")
        if not isinstance(source, dict) or not isinstance(brand, dict):
            raise EvidenceClaimCorroborationReviewSetError(
                f"incomplete parent source case: {parent_case_id}"
            )
        source_url = str(source.get("url") or "")
        source_text = captured_text_by_url.get(source_url)
        if source_text is None:
            raise EvidenceClaimCorroborationReviewSetError(
                f"source URL is absent from capture: {source_url}"
            )
        evidence_span = str(selection["evidence_span"])
        occurrence_count = source_text.count(evidence_span)
        if occurrence_count != 1:
            raise EvidenceClaimCorroborationReviewSetError(
                "evidence span must occur exactly once for case "
                f"{selection['case_id']}: found {occurrence_count}"
            )
        span_start = source_text.index(evidence_span)
        span_end = span_start + len(evidence_span)
        candidates.append(
            {
                "schema_version": (
                    EVIDENCE_CLAIM_CORROBORATION_REVIEW_SCHEMA_VERSION
                ),
                "dataset_version": (
                    EVIDENCE_CLAIM_CORROBORATION_REVIEW_DATASET_VERSION
                ),
                "case_id": str(selection["case_id"]),
                "parent_source_case_id": parent_case_id,
                "provenance": "real_capture",
                "brand": {
                    "name": str(brand.get("name") or ""),
                    "domain": str(brand.get("domain") or ""),
                },
                "claim": {
                    "claim_id": str(selection["claim_id"]),
                    "statement": str(selection["claim_statement"]),
                    "scope": str(selection["claim_scope"]),
                    "subject": str(selection["claim_subject"]),
                    "derivation": "literal_span_frozen_for_review",
                    "canonical": False,
                },
                "source": {
                    "url": source_url,
                    "article_title": str(
                        source.get("article_title") or ""
                    ),
                    "publisher_name": str(
                        source.get("publisher_name") or ""
                    ),
                    "publisher_domain": str(
                        source.get("publisher_domain") or ""
                    ),
                    "publisher_kind": str(
                        source.get("publisher_kind") or ""
                    ),
                },
                "evidence": {
                    "capture_artifact_path": artifact_path,
                    "capture_artifact_sha256": artifact_hash,
                    "source_text_sha256": _sha256_text(source_text),
                    "evidence_span": evidence_span,
                    "evidence_span_sha256": _sha256_text(
                        evidence_span
                    ),
                    "span_start": span_start,
                    "span_end": span_end,
                    "offset_unit": "unicode_codepoint",
                },
                "review_prompt": {
                    "support": (
                        "¿La pieza aporta soporte verificable para este "
                        "claim concreto?"
                    ),
                    "independence": (
                        "¿Ese soporte procede de observación, prueba o "
                        "documentación independiente, o de Vercel?"
                    ),
                    "decision": (
                        "Elige independently_corroborated, mixed, "
                        "disputed o excluded y justifica la procedencia."
                    ),
                },
                "runtime_effect": False,
                "authority": False,
            }
        )
    return candidates


def build_review_candidates_from_files(
    *,
    selections_path: Path | None = None,
    source_candidates_path: Path | None = None,
    capture_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Rebuild candidates from the frozen real-capture provenance."""

    actual_capture_path = capture_path or DEFAULT_CAPTURE_PATH
    try:
        capture_bytes = actual_capture_path.read_bytes()
        capture = json.loads(capture_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceClaimCorroborationReviewSetError(
            f"cannot load capture artifact: {actual_capture_path}"
        ) from exc
    if not isinstance(capture, dict):
        raise EvidenceClaimCorroborationReviewSetError(
            "capture artifact must be an object"
        )
    project_root = Path(__file__).resolve().parents[2]
    try:
        relative_capture_path = actual_capture_path.resolve().relative_to(
            project_root.resolve()
        )
    except ValueError as exc:
        raise EvidenceClaimCorroborationReviewSetError(
            "capture artifact must live under the project root"
        ) from exc
    return build_review_candidates(
        load_review_selections(selections_path),
        load_source_candidates(source_candidates_path),
        capture,
        capture_artifact_path=relative_capture_path.as_posix(),
        capture_artifact_sha256=hashlib.sha256(capture_bytes).hexdigest(),
    )


def verify_frozen_candidate_provenance(
    candidates: Iterable[dict[str, Any]],
    *,
    selections_path: Path | None = None,
    source_candidates_path: Path | None = None,
    capture_path: Path | None = None,
) -> bool:
    """Return whether frozen candidates reproduce byte-for-byte."""

    frozen = sorted(
        (dict(row) for row in candidates),
        key=lambda row: str(row.get("case_id") or ""),
    )
    rebuilt = build_review_candidates_from_files(
        selections_path=selections_path,
        source_candidates_path=source_candidates_path,
        capture_path=capture_path,
    )
    return frozen == rebuilt


def build_review_template(
    candidates: Iterable[dict[str, Any]],
    *,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return unsigned decision events; candidates are never labels."""

    candidate_rows = [dict(row) for row in candidates]
    candidate_fingerprint = _validate_candidate_fingerprint(
        manifest, candidate_rows
    )
    packet_fingerprint = _validate_review_packet_fingerprint(
        manifest, candidate_rows
    )
    return [
        {
            "schema_version": (
                EVIDENCE_CLAIM_CORROBORATION_REVIEW_EVENT_SCHEMA_VERSION
            ),
            "dataset_version": (
                EVIDENCE_CLAIM_CORROBORATION_REVIEW_DATASET_VERSION
            ),
            "candidate_fingerprint": candidate_fingerprint,
            "review_packet_fingerprint": packet_fingerprint,
            "event_id": None,
            "case_id": str(candidate["case_id"]),
            "sequence": 1,
            "event_type": "decision",
            "previous_event_id": None,
            "decision": None,
            "corroboration_bases": [],
            "reviewer_id": None,
            "rationale": None,
            "reviewed_at": None,
            "runtime_effect": False,
            "authority": False,
        }
        for candidate in sorted(
            candidate_rows,
            key=lambda row: str(row["case_id"]),
        )
    ]


def evaluate_claim_corroboration_review_set(
    candidates: Iterable[dict[str, Any]],
    review_events: Iterable[dict[str, Any]],
    *,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate attributable claim reviews without operational authority."""

    candidate_rows = [dict(row) for row in candidates]
    event_rows = [dict(row) for row in review_events]
    for row in candidate_rows:
        _validate_candidate(row)
    for row in event_rows:
        _validate_event_shape(row)
    candidates_by_id = _unique_by_case_id(
        candidate_rows,
        label="candidate",
    )
    candidate_fingerprint = _validate_candidate_fingerprint(
        manifest, candidate_rows
    )
    packet_fingerprint = _validate_review_packet_fingerprint(
        manifest, candidate_rows
    )
    _validate_candidate_fingerprint_bindings(
        event_rows,
        expected=candidate_fingerprint,
    )
    _validate_review_packet_fingerprint_bindings(
        event_rows,
        expected=packet_fingerprint,
    )
    fingerprint = review_set_fingerprint(candidate_rows)
    event_fingerprint = review_set_fingerprint(event_rows)
    expected_event_fingerprint = str(
        manifest.get("review_event_fingerprint") or ""
    )
    if (
        expected_event_fingerprint
        and expected_event_fingerprint != event_fingerprint
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim-corroboration event fingerprint mismatch"
        )

    active_reviews, revoked_case_ids = resolve_active_reviews(
        event_rows,
        known_case_ids=set(candidates_by_id),
    )
    pending_case_ids = sorted(set(candidates_by_id) - set(active_reviews))
    decision_counts: Counter[str] = Counter()
    basis_counts: Counter[str] = Counter()
    reviewed_sources: set[str] = set()
    reviewed_claims: set[str] = set()
    reviewed_brands: set[str] = set()
    evaluated: list[dict[str, Any]] = []
    for case_id in sorted(active_reviews):
        candidate = candidates_by_id[case_id]
        review = active_reviews[case_id]
        decision = str(review["decision"])
        bases = sorted(
            str(value) for value in review["corroboration_bases"]
        )
        decision_counts[decision] += 1
        basis_counts.update(bases)
        source_url = str(candidate["source"]["url"])
        claim_id = str(candidate["claim"]["claim_id"])
        brand_domain = str(candidate["brand"]["domain"])
        reviewed_sources.add(source_url)
        reviewed_claims.add(claim_id)
        reviewed_brands.add(brand_domain)
        evaluated.append(
            {
                "case_id": case_id,
                "parent_source_case_id": str(
                    candidate["parent_source_case_id"]
                ),
                "claim_id": claim_id,
                "source_url": source_url,
                "decision": decision,
                "corroboration_bases": bases,
                "reviewer_id": str(review["reviewer_id"]),
                "event_id": str(review["event_id"]),
                "runtime_effect": False,
                "authority": False,
            }
        )

    candidate_count = len(candidates_by_id)
    reviewed_count = len(active_reviews)
    review_gate_ready = (
        candidate_count > 0 and reviewed_count == candidate_count
    )
    thresholds = (
        manifest.get("promotion_thresholds")
        if isinstance(manifest.get("promotion_thresholds"), dict)
        else {}
    )
    promotion_blockers: list[str] = []
    if not review_gate_ready:
        promotion_blockers.append("human_reviews_incomplete")
    if reviewed_count == 0:
        promotion_blockers.append("no_human_reviews")
    if len(reviewed_sources) < int(
        thresholds.get("minimum_reviewed_real_sources", 5)
    ):
        promotion_blockers.append("insufficient_reviewed_real_sources")
    if len(reviewed_claims) < int(
        thresholds.get("minimum_reviewed_claims", 10)
    ):
        promotion_blockers.append("insufficient_reviewed_claims")
    if len(reviewed_brands) < int(
        thresholds.get("minimum_reviewed_real_brands", 3)
    ):
        promotion_blockers.append("insufficient_reviewed_real_brands")
    semantic_paraphrase = manifest.get("semantic_paraphrase_evaluation")
    if (
        not isinstance(semantic_paraphrase, dict)
        or semantic_paraphrase.get("status") != "measured"
    ):
        promotion_blockers.append(
            "semantic_paraphrase_recall_unmeasured"
        )
    operational_contract = manifest.get("operational_contract")
    if (
        not isinstance(operational_contract, dict)
        or operational_contract.get("status") != "adopted"
    ):
        promotion_blockers.append(
            "operational_two_axis_contract_not_adopted"
        )

    return {
        "schema_version": (
            EVIDENCE_CLAIM_CORROBORATION_REVIEW_SCHEMA_VERSION
        ),
        "review_event_schema_version": (
            EVIDENCE_CLAIM_CORROBORATION_REVIEW_EVENT_SCHEMA_VERSION
        ),
        "dataset_version": (
            EVIDENCE_CLAIM_CORROBORATION_REVIEW_DATASET_VERSION
        ),
        "dataset_fingerprint": fingerprint,
        "review_packet_fingerprint": packet_fingerprint,
        "review_event_fingerprint": event_fingerprint,
        "runtime_effect": False,
        "authority": False,
        "review_gate_ready": review_gate_ready,
        "promotion_ready": not promotion_blockers,
        "promotion_blockers": sorted(set(promotion_blockers)),
        "summary": {
            "candidate_count": candidate_count,
            "candidate_source_count": len(
                {
                    str(row["source"]["url"])
                    for row in candidate_rows
                }
            ),
            "candidate_claim_count": len(
                {
                    str(row["claim"]["claim_id"])
                    for row in candidate_rows
                }
            ),
            "candidate_brand_count": len(
                {
                    str(row["brand"]["domain"])
                    for row in candidate_rows
                }
            ),
            "review_event_count": len(event_rows),
            "reviewed_count": reviewed_count,
            "pending_count": len(pending_case_ids),
            "revoked_case_count": len(revoked_case_ids),
            "reviewed_source_count": len(reviewed_sources),
            "reviewed_claim_count": len(reviewed_claims),
            "reviewed_brand_count": len(reviewed_brands),
            "decision_counts": dict(sorted(decision_counts.items())),
            "corroboration_basis_counts": dict(
                sorted(basis_counts.items())
            ),
        },
        "pending_case_ids": pending_case_ids,
        "revoked_case_ids": sorted(revoked_case_ids),
        "evaluated": evaluated,
    }


def resolve_active_reviews(
    review_events: Iterable[dict[str, Any]],
    *,
    known_case_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Resolve append-only decision and revocation chains."""

    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_event_ids: set[str] = set()
    for raw in review_events:
        event = dict(raw)
        _validate_event_shape(event)
        event_id = str(event["event_id"])
        if event_id in seen_event_ids:
            raise EvidenceClaimCorroborationReviewSetError(
                f"duplicate claim-corroboration event: {event_id}"
            )
        seen_event_ids.add(event_id)
        case_id = str(event["case_id"])
        if case_id not in known_case_ids:
            raise EvidenceClaimCorroborationReviewSetError(
                f"event references unknown claim case: {case_id}"
            )
        grouped[case_id].append(event)

    active_reviews: dict[str, dict[str, Any]] = {}
    revoked_case_ids: set[str] = set()
    for case_id, events in sorted(grouped.items()):
        ordered = sorted(events, key=lambda row: int(row["sequence"]))
        active: dict[str, Any] | None = None
        previous: dict[str, Any] | None = None
        for expected_sequence, event in enumerate(ordered, start=1):
            if int(event["sequence"]) != expected_sequence:
                raise EvidenceClaimCorroborationReviewSetError(
                    f"event sequence gap for claim case: {case_id}"
                )
            expected_previous = (
                None if previous is None else str(previous["event_id"])
            )
            if event.get("previous_event_id") != expected_previous:
                raise EvidenceClaimCorroborationReviewSetError(
                    f"event chain mismatch for claim case: {case_id}"
                )
            if event["event_type"] == "decision":
                _validate_decision_event(event)
                active = event
                revoked_case_ids.discard(case_id)
            else:
                _validate_revocation_event(event)
                if active is None:
                    raise EvidenceClaimCorroborationReviewSetError(
                        f"cannot revoke inactive claim case: {case_id}"
                    )
                if event["revoked_event_id"] != active["event_id"]:
                    raise EvidenceClaimCorroborationReviewSetError(
                        f"revocation target is not active: {case_id}"
                    )
                active = None
                revoked_case_ids.add(case_id)
            previous = event
        if active is not None:
            active_reviews[case_id] = active
    return active_reviews, revoked_case_ids


def review_set_fingerprint(rows: Iterable[dict[str, Any]]) -> str:
    canonical = json.dumps(
        sorted(
            (dict(row) for row in rows),
            key=lambda row: (
                str(row.get("case_id") or ""),
                int(row.get("sequence") or 0),
                str(row.get("event_id") or ""),
            ),
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def claim_corroboration_review_packet_fingerprint(
    manifest: dict[str, Any],
    candidates: Iterable[dict[str, Any]],
) -> str:
    return review_packet_fingerprint(
        packet_kind="claim_corroboration",
        manifest=manifest,
        candidates=candidates,
        schema_versions={
            "candidate_manifest": (
                EVIDENCE_CLAIM_CORROBORATION_REVIEW_SCHEMA_VERSION
            ),
            "review_event": (
                EVIDENCE_CLAIM_CORROBORATION_REVIEW_EVENT_SCHEMA_VERSION
            ),
            "selection": (
                EVIDENCE_CLAIM_CORROBORATION_REVIEW_SELECTION_SCHEMA_VERSION
            ),
        },
    )


def render_claim_corroboration_review_set_markdown(
    result: dict[str, Any],
) -> str:
    summary = result.get("summary") or {}
    lines = [
        "# Evidence claim-corroboration review set",
        "",
        f"- Dataset: `{result.get('dataset_version', 'unknown')}`",
        f"- Runtime effect: `{str(bool(result.get('runtime_effect'))).lower()}`",
        f"- Authority: `{str(bool(result.get('authority'))).lower()}`",
        f"- Review gate ready: `{str(bool(result.get('review_gate_ready'))).lower()}`",
        f"- Promotion ready: `{str(bool(result.get('promotion_ready'))).lower()}`",
        f"- Candidates: `{summary.get('candidate_count', 0)}`",
        f"- Sources: `{summary.get('candidate_source_count', 0)}`",
        f"- Claims: `{summary.get('candidate_claim_count', 0)}`",
        f"- Reviewed: `{summary.get('reviewed_count', 0)}`",
        f"- Pending: `{summary.get('pending_count', 0)}`",
        "",
        "## Promotion blockers",
        "",
    ]
    blockers = result.get("promotion_blockers") or []
    lines.extend(f"- `{blocker}`" for blocker in blockers)
    if not blockers:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def _validate_selection(row: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "dataset_version",
        "case_id",
        "parent_source_case_id",
        "claim_id",
        "claim_statement",
        "claim_scope",
        "claim_subject",
        "evidence_span",
    }
    missing = sorted(required - set(row))
    if missing:
        raise EvidenceClaimCorroborationReviewSetError(
            f"claim selection missing fields: {', '.join(missing)}"
        )
    if (
        row["schema_version"]
        != EVIDENCE_CLAIM_CORROBORATION_REVIEW_SELECTION_SCHEMA_VERSION
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim selection schema version mismatch"
        )
    if (
        row["dataset_version"]
        != EVIDENCE_CLAIM_CORROBORATION_REVIEW_DATASET_VERSION
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim selection dataset version mismatch"
        )
    for field in required - {"schema_version", "dataset_version"}:
        if not str(row[field]).strip():
            raise EvidenceClaimCorroborationReviewSetError(
                f"claim selection {field} is required"
            )
    if len(str(row["evidence_span"])) < 40:
        raise EvidenceClaimCorroborationReviewSetError(
            "claim selection evidence_span is too short"
        )


def _validate_candidate(row: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "dataset_version",
        "case_id",
        "parent_source_case_id",
        "provenance",
        "brand",
        "claim",
        "source",
        "evidence",
        "review_prompt",
        "runtime_effect",
        "authority",
    }
    missing = sorted(required - set(row))
    if missing:
        raise EvidenceClaimCorroborationReviewSetError(
            f"claim candidate missing fields: {', '.join(missing)}"
        )
    if (
        row["schema_version"]
        != EVIDENCE_CLAIM_CORROBORATION_REVIEW_SCHEMA_VERSION
        or row["dataset_version"]
        != EVIDENCE_CLAIM_CORROBORATION_REVIEW_DATASET_VERSION
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim candidate version mismatch"
        )
    if row["provenance"] != "real_capture":
        raise EvidenceClaimCorroborationReviewSetError(
            "claim candidate must come from a real capture"
        )
    if row["runtime_effect"] is not False or row["authority"] is not False:
        raise EvidenceClaimCorroborationReviewSetError(
            "claim candidates must remain non-authoritative"
        )
    if (
        not str(row["case_id"]).strip()
        or not str(row["parent_source_case_id"]).strip()
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim candidate identifiers are incomplete"
        )
    brand = row["brand"]
    if not isinstance(brand, dict) or any(
        not str(brand.get(field) or "").strip()
        for field in ("name", "domain")
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim candidate brand is incomplete"
        )
    claim = row["claim"]
    if not isinstance(claim, dict) or any(
        not str(claim.get(field) or "").strip()
        for field in ("claim_id", "statement", "scope", "subject")
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim candidate claim is incomplete"
        )
    if claim.get("canonical") is not False:
        raise EvidenceClaimCorroborationReviewSetError(
            "review claim cannot be canonical"
        )
    if claim.get("derivation") != "literal_span_frozen_for_review":
        raise EvidenceClaimCorroborationReviewSetError(
            "claim candidate derivation is invalid"
        )
    source = row["source"]
    if not isinstance(source, dict) or any(
        not str(source.get(field) or "").strip()
        for field in (
            "url",
            "article_title",
            "publisher_name",
            "publisher_domain",
            "publisher_kind",
        )
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim candidate source is incomplete"
        )
    parsed = urlsplit(str(source["url"]))
    if parsed.scheme != "https" or not parsed.netloc:
        raise EvidenceClaimCorroborationReviewSetError(
            "claim candidate source URL must be absolute HTTPS"
        )
    evidence = row["evidence"]
    if not isinstance(evidence, dict):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim candidate evidence must be an object"
        )
    for field in (
        "capture_artifact_sha256",
        "source_text_sha256",
        "evidence_span_sha256",
    ):
        _required_sha256(evidence.get(field), field=field)
    capture_artifact_path = str(
        evidence.get("capture_artifact_path") or ""
    )
    if (
        not capture_artifact_path
        or Path(capture_artifact_path).is_absolute()
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim candidate capture path must be relative"
        )
    span = str(evidence.get("evidence_span") or "")
    start = evidence.get("span_start")
    end = evidence.get("span_end")
    if (
        not span
        or not isinstance(start, int)
        or not isinstance(end, int)
        or start < 0
        or end != start + len(span)
        or _sha256_text(span) != evidence["evidence_span_sha256"]
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim candidate span provenance is invalid"
        )
    if evidence.get("offset_unit") != "unicode_codepoint":
        raise EvidenceClaimCorroborationReviewSetError(
            "claim candidate offset unit is invalid"
        )
    review_prompt = row["review_prompt"]
    if not isinstance(review_prompt, dict) or any(
        not str(review_prompt.get(field) or "").strip()
        for field in ("support", "independence", "decision")
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim candidate review prompt is incomplete"
        )


def _validate_event_shape(row: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "dataset_version",
        "candidate_fingerprint",
        "review_packet_fingerprint",
        "event_id",
        "case_id",
        "sequence",
        "event_type",
        "previous_event_id",
        "reviewer_id",
        "rationale",
        "reviewed_at",
        "runtime_effect",
        "authority",
    }
    missing = sorted(required - set(row))
    if missing:
        raise EvidenceClaimCorroborationReviewSetError(
            f"claim review event missing fields: {', '.join(missing)}"
        )
    if (
        row["schema_version"]
        != EVIDENCE_CLAIM_CORROBORATION_REVIEW_EVENT_SCHEMA_VERSION
        or row["dataset_version"]
        != EVIDENCE_CLAIM_CORROBORATION_REVIEW_DATASET_VERSION
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim review event version mismatch"
        )
    _required_sha256(
        row.get("candidate_fingerprint"),
        field="candidate_fingerprint",
    )
    _required_sha256(
        row.get("review_packet_fingerprint"),
        field="review_packet_fingerprint",
    )
    if row["event_type"] not in {"decision", "revocation"}:
        raise EvidenceClaimCorroborationReviewSetError(
            "claim review event_type is invalid"
        )
    if (
        not str(row["event_id"]).strip()
        or not str(row["case_id"]).strip()
        or not isinstance(row["sequence"], int)
        or row["sequence"] < 1
        or not str(row["reviewer_id"]).strip()
        or not str(row["rationale"]).strip()
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim review event attribution is incomplete"
        )
    try:
        datetime.fromisoformat(
            str(row["reviewed_at"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise EvidenceClaimCorroborationReviewSetError(
            "claim review reviewed_at must be ISO-8601"
        ) from exc
    if row["runtime_effect"] is not False or row["authority"] is not False:
        raise EvidenceClaimCorroborationReviewSetError(
            "claim review events must remain non-authoritative"
        )


def _validate_candidate_fingerprint(
    manifest: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str:
    candidate_fingerprint = review_set_fingerprint(candidates)
    if (
        str(manifest.get("candidate_fingerprint") or "")
        != candidate_fingerprint
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim-corroboration candidate fingerprint mismatch"
        )
    return candidate_fingerprint


def _validate_candidate_fingerprint_bindings(
    events: Iterable[dict[str, Any]],
    *,
    expected: str,
) -> None:
    fingerprints = {
        str(row.get("candidate_fingerprint") or "")
        for row in events
    }
    if len(fingerprints) > 1:
        raise EvidenceClaimCorroborationReviewSetError(
            "claim-corroboration events mix candidate fingerprints"
        )
    if fingerprints and fingerprints != {expected}:
        raise EvidenceClaimCorroborationReviewSetError(
            "claim-corroboration review candidate fingerprint mismatch"
        )


def _validate_review_packet_fingerprint(
    manifest: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str:
    fingerprint = claim_corroboration_review_packet_fingerprint(
        manifest,
        candidates,
    )
    if (
        str(manifest.get("review_packet_fingerprint") or "")
        != fingerprint
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim-corroboration review packet fingerprint mismatch"
        )
    return fingerprint


def _validate_review_packet_fingerprint_bindings(
    events: Iterable[dict[str, Any]],
    *,
    expected: str,
) -> None:
    fingerprints = {
        str(row.get("review_packet_fingerprint") or "")
        for row in events
    }
    if len(fingerprints) > 1:
        raise EvidenceClaimCorroborationReviewSetError(
            "claim-corroboration events mix review packet fingerprints"
        )
    if fingerprints and fingerprints != {expected}:
        raise EvidenceClaimCorroborationReviewSetError(
            "claim-corroboration review packet fingerprint mismatch"
        )


def _validate_decision_event(row: dict[str, Any]) -> None:
    decision = str(row.get("decision") or "")
    bases = row.get("corroboration_bases")
    if decision not in CORROBORATION_DECISIONS:
        raise EvidenceClaimCorroborationReviewSetError(
            "claim corroboration decision is invalid"
        )
    if (
        not isinstance(bases, list)
        or not bases
        or any(str(value) not in CORROBORATION_BASES for value in bases)
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim corroboration bases are invalid"
        )
    basis_set = {str(value) for value in bases}
    if (
        decision == "independently_corroborated"
        and not basis_set.intersection(_INDEPENDENT_BASES)
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "independent decision requires an independent basis"
        )
    if (
        decision == "mixed"
        and "mixed_provenance" not in basis_set
        and not (
            basis_set.intersection(_INDEPENDENT_BASES)
            and basis_set.intersection(_FIRST_PARTY_BASES)
        )
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "mixed decision requires mixed provenance"
        )
    if row.get("revoked_event_id") is not None:
        raise EvidenceClaimCorroborationReviewSetError(
            "decision event cannot revoke another event"
        )


def _validate_revocation_event(row: dict[str, Any]) -> None:
    if (
        not str(row.get("revoked_event_id") or "").strip()
        or row.get("decision") is not None
        or row.get("corroboration_bases") not in (None, [])
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            "claim corroboration revocation is invalid"
        )


def _captured_text_by_url(
    capture: dict[str, Any],
    *,
    required_urls: set[str],
) -> dict[str, str]:
    by_url: dict[str, str] = {}
    stack: list[Any] = [capture]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            url = value.get("url")
            text = value.get("text")
            if (
                isinstance(url, str)
                and isinstance(text, str)
                and url in required_urls
                and text
            ):
                previous = by_url.setdefault(url, text)
                if previous != text:
                    raise EvidenceClaimCorroborationReviewSetError(
                        f"capture has conflicting text for URL: {url}"
                    )
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return by_url


def _unique_by_case_id(
    rows: Iterable[dict[str, Any]],
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        case_id = str(row.get("case_id") or "")
        if not case_id or case_id in by_id:
            raise EvidenceClaimCorroborationReviewSetError(
                f"duplicate or empty {label} case_id: {case_id}"
            )
        by_id[case_id] = row
    return by_id


def _load_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvidenceClaimCorroborationReviewSetError(
            f"cannot load {label}: {path}"
        ) from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceClaimCorroborationReviewSetError(
                f"invalid {label} JSON at line {line_number}"
            ) from exc
        if not isinstance(payload, dict):
            raise EvidenceClaimCorroborationReviewSetError(
                f"{label} line {line_number} must be an object"
            )
        rows.append(payload)
    return rows


def _required_sha256(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if (
        len(normalized) != 64
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        raise EvidenceClaimCorroborationReviewSetError(
            f"{field} must be a SHA-256 digest"
        )
    return normalized


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
