"""Deterministic temporal comparison for B3S evidence and evaluations.

The model may propose a different interpretation on every call. This module
keeps that variability separate from observed brand change by comparing
normalized evidence before any report is allowed to become canonical.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import os
import re
import unicodedata
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


EVIDENCE_COMPARISON_VERSION = "evidence-comparison-v1"
CANONICAL_POLICY_VERSION = "brand-canonical-policy-v1"
ENFORCEMENT_ENV = "B3S_CANONICAL_ENFORCEMENT_MODE"
ENFORCEMENT_MODES = {"observe", "repeated", "all"}

_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}
_INVALID_RELIABILITY = {"broken", "invalid", "error", "failed"}
_MATERIAL_SOURCE_CLASSES = {
    "owned_copy",
    "external_proof",
    "visual_signal",
    "derived_strategy",
    "other",
}
_TOKEN_RE = re.compile(r"[\wÀ-ÖØ-öø-ÿ]{3,}", flags=re.UNICODE)


@dataclass(frozen=True, slots=True)
class CanonicalEvidenceRecord:
    locator: str
    fingerprint: str
    source: str
    source_class: str
    evidence_type: str
    url: str
    content_hash: str
    normalized_content: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator,
            "fingerprint": self.fingerprint,
            "source": self.source,
            "source_class": self.source_class,
            "evidence_type": self.evidence_type,
            "url": self.url,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    report_id: str
    fingerprint: str
    semantic_fingerprint: str
    component_fingerprints: dict[str, str]
    material_records: tuple[CanonicalEvidenceRecord, ...]
    material_url_count: int
    owned_count: int
    external_count: int
    independent_external_cluster_count: int
    visual_evidence_count: int
    acquisition_state: str
    acquisition_warning_codes: tuple[str, ...]
    reliability_status: str
    evaluation_fingerprint: str
    interpretation_fingerprint: str
    invalid: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_COMPARISON_VERSION,
            "report_id": self.report_id,
            "fingerprint": self.fingerprint,
            "semantic_fingerprint": self.semantic_fingerprint,
            "component_fingerprints": dict(self.component_fingerprints),
            "counts": {
                "material_records": len(self.material_records),
                "material_urls": self.material_url_count,
                "owned": self.owned_count,
                "external": self.external_count,
                "independent_external_clusters": self.independent_external_cluster_count,
                "visual": self.visual_evidence_count,
            },
            "acquisition": {
                "state": self.acquisition_state,
                "warning_codes": list(self.acquisition_warning_codes),
            },
            "reliability_status": self.reliability_status,
            "evaluation_fingerprint": self.evaluation_fingerprint,
            "interpretation_fingerprint": self.interpretation_fingerprint,
            "invalid": self.invalid,
        }


@dataclass(frozen=True, slots=True)
class EvidenceComparison:
    baseline_report_id: str
    candidate_report_id: str
    classification: str
    equivalent_evidence: bool
    acquisition_comparable: bool
    evaluation_changed: bool
    interpretation_changed: bool
    reason_codes: tuple[str, ...]
    delta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_COMPARISON_VERSION,
            "baseline_report_id": self.baseline_report_id,
            "candidate_report_id": self.candidate_report_id,
            "classification": self.classification,
            "equivalent_evidence": self.equivalent_evidence,
            "acquisition_comparable": self.acquisition_comparable,
            "evaluation_changed": self.evaluation_changed,
            "interpretation_changed": self.interpretation_changed,
            "reason_codes": list(self.reason_codes),
            "delta": dict(self.delta),
        }


def build_evidence_snapshot(report: dict[str, Any]) -> EvidenceSnapshot:
    """Build an order- and ref-insensitive snapshot from one immutable report."""

    records = _canonical_records(report)
    material = tuple(record for record in records if record.source_class in _MATERIAL_SOURCE_CLASSES)
    material_payload = [record.public_dict() for record in material]
    urls = sorted({record.url for record in material if record.url})
    owned = [record for record in material if record.source_class == "owned_copy"]
    external = [record for record in material if record.source_class == "external_proof"]
    visual = [record for record in material if record.source_class == "visual_signal"]
    component_fingerprints = _component_fingerprints(report, records)
    acquisition_gate = report.get("acquisition_gate") if isinstance(report.get("acquisition_gate"), dict) else {}
    warnings = [
        str(item.get("code") or "")
        for item in acquisition_gate.get("warnings") or []
        if isinstance(item, dict) and str(item.get("code") or "").strip()
    ]
    reliability = str(report.get("reliability_status") or "unknown").strip().lower()
    invalid = reliability in _INVALID_RELIABILITY or _has_not_evaluated_component(report)
    semantic_payload = {
        "urls": urls,
        "owned_content": sorted(record.content_hash for record in owned),
        "external_clusters": _external_cluster_fingerprints(external),
        "visual_content": sorted(record.content_hash for record in visual),
    }
    return EvidenceSnapshot(
        report_id=str(report.get("id") or ""),
        fingerprint=_stable_hash(material_payload),
        semantic_fingerprint=_stable_hash(semantic_payload),
        component_fingerprints=component_fingerprints,
        material_records=material,
        material_url_count=len(urls),
        owned_count=len(owned),
        external_count=len(external),
        independent_external_cluster_count=len(_external_clusters(external)),
        visual_evidence_count=len(visual),
        acquisition_state=str(acquisition_gate.get("state") or "unknown").strip().lower(),
        acquisition_warning_codes=tuple(sorted(set(warnings))),
        reliability_status=reliability,
        evaluation_fingerprint=_evaluation_fingerprint(report),
        interpretation_fingerprint=_interpretation_fingerprint(report),
        invalid=invalid,
    )


def compare_reports(
    baseline_report: dict[str, Any],
    candidate_report: dict[str, Any],
) -> EvidenceComparison:
    """Compare one candidate with a prior canonical/provisional report."""

    baseline = build_evidence_snapshot(baseline_report)
    candidate = build_evidence_snapshot(candidate_report)
    baseline_by_locator = {record.locator: record for record in baseline.material_records}
    candidate_by_locator = {record.locator: record for record in candidate.material_records}
    baseline_urls = {record.url for record in baseline.material_records if record.url}
    candidate_urls = {record.url for record in candidate.material_records if record.url}

    unchanged = sorted(
        locator
        for locator in baseline_by_locator.keys() & candidate_by_locator.keys()
        if baseline_by_locator[locator].content_hash == candidate_by_locator[locator].content_hash
    )
    modified = sorted(
        locator
        for locator in baseline_by_locator.keys() & candidate_by_locator.keys()
        if baseline_by_locator[locator].content_hash != candidate_by_locator[locator].content_hash
    )
    lost = sorted(baseline_by_locator.keys() - candidate_by_locator.keys())
    added = sorted(candidate_by_locator.keys() - baseline_by_locator.keys())
    lost_urls = sorted(baseline_urls - candidate_urls)
    added_urls = sorted(candidate_urls - baseline_urls)
    url_similarity = _jaccard(baseline_urls, candidate_urls)
    exact_record_ratio = len(unchanged) / max(1, len(baseline_by_locator))
    equivalent = (
        baseline.fingerprint == candidate.fingerprint
        or baseline.semantic_fingerprint == candidate.semantic_fingerprint
        or (
            url_similarity >= 0.9
            and exact_record_ratio >= 0.8
            and candidate.owned_count >= baseline.owned_count
        )
    )
    acquisition_regression_reasons = _acquisition_regression_reasons(
        baseline,
        candidate,
        lost_urls=lost_urls,
    )
    acquisition_comparable = not acquisition_regression_reasons
    evaluation_changed = baseline.evaluation_fingerprint != candidate.evaluation_fingerprint
    interpretation_changed = baseline.interpretation_fingerprint != candidate.interpretation_fingerprint

    reasons: list[str] = []
    if candidate.invalid:
        classification = "invalid"
        reasons.append("candidate_invalid")
        reasons.extend(acquisition_regression_reasons)
    elif acquisition_regression_reasons:
        classification = "acquisition_regression"
        reasons.extend(acquisition_regression_reasons)
    elif equivalent and (evaluation_changed or interpretation_changed):
        classification = "evaluation_drift"
        if interpretation_changed:
            reasons.append("interpretation_changed_without_material_evidence_delta")
        if evaluation_changed:
            reasons.append("evaluation_changed_without_material_evidence_delta")
    elif equivalent:
        classification = "stable"
        reasons.append("material_evidence_equivalent")
    else:
        classification = "candidate"
        reasons.append("material_evidence_changed")

    return EvidenceComparison(
        baseline_report_id=baseline.report_id,
        candidate_report_id=candidate.report_id,
        classification=classification,
        equivalent_evidence=equivalent,
        acquisition_comparable=acquisition_comparable,
        evaluation_changed=evaluation_changed,
        interpretation_changed=interpretation_changed,
        reason_codes=tuple(dict.fromkeys(reasons)),
        delta={
            "unchanged_record_count": len(unchanged),
            "modified_record_count": len(modified),
            "added_record_count": len(added),
            "lost_record_count": len(lost),
            "added_urls": added_urls,
            "lost_urls": lost_urls,
            "acquisition_unknown_urls": lost_urls,
            "verified_removed_urls": [],
            "url_jaccard": round(url_similarity, 4),
            "exact_record_ratio": round(exact_record_ratio, 4),
            "baseline_counts": baseline.to_dict()["counts"],
            "candidate_counts": candidate.to_dict()["counts"],
            "changed_components": _changed_components(baseline_report, candidate_report),
        },
    )


def classify_report_history(reports: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Classify a brand history without mutating its immutable reports."""

    ordered = sorted(
        (dict(report) for report in reports if isinstance(report, dict)),
        key=lambda report: (str(report.get("created_at") or ""), str(report.get("id") or "")),
    )
    if not ordered:
        return {
            "schema_version": EVIDENCE_COMPARISON_VERSION,
            "policy_version": CANONICAL_POLICY_VERSION,
            "report_count": 0,
            "canonical_report_id": None,
            "provisional_report_id": None,
            "selected_report_id": None,
            "entries": [],
        }

    entries: list[dict[str, Any]] = []
    selected_report: dict[str, Any] | None = None
    selected_kind = ""
    previous_report: dict[str, Any] | None = None

    for index, report in enumerate(ordered):
        snapshot = build_evidence_snapshot(report)
        eligible = _eligible_for_canonical(report, snapshot)
        if selected_report is None:
            if snapshot.invalid:
                classification = "invalid"
                canonical_status = "invalid"
                reasons = ["candidate_invalid"]
            else:
                selected_report = report
                selected_kind = "canonical" if eligible else "provisional"
                classification = selected_kind
                canonical_status = selected_kind
                reasons = [
                    "first_reliable_baseline" if eligible else "first_non_invalid_baseline_is_provisional"
                ]
            entry = _history_entry(
                report,
                snapshot,
                classification=classification,
                canonical_status=canonical_status,
                reasons=reasons,
                baseline_comparison=None,
                previous_comparison=None,
            )
            entries.append(entry)
            previous_report = report
            continue

        baseline_comparison = compare_reports(selected_report, report)
        previous_comparison = compare_reports(previous_report, report) if previous_report is not None else None
        effective = previous_comparison if previous_comparison and previous_comparison.classification in {
            "invalid",
            "acquisition_regression",
            "evaluation_drift",
        } else baseline_comparison

        classification = effective.classification
        reasons = list(effective.reason_codes)
        canonical_status = "non_canonical"

        if (
            selected_kind == "provisional"
            and eligible
            and classification == "stable"
        ):
            selected_report = report
            selected_kind = "canonical"
            classification = "canonical"
            canonical_status = "canonical"
            reasons.append("reliable_repeat_promoted")
            for prior_entry in entries:
                if prior_entry["canonical_status"] == "provisional":
                    prior_entry["canonical_status"] = "non_canonical"
        elif selected_kind == "canonical" and str(report.get("id") or "") == str(selected_report.get("id") or ""):
            canonical_status = "canonical"

        entry = _history_entry(
            report,
            snapshot,
            classification=classification,
            canonical_status=canonical_status,
            reasons=reasons,
            baseline_comparison=baseline_comparison,
            previous_comparison=previous_comparison,
        )
        entries.append(entry)
        previous_report = report

    selected_id = str(selected_report.get("id") or "") if selected_report else None
    canonical_id = selected_id if selected_kind == "canonical" else None
    provisional_id = selected_id if selected_kind == "provisional" else None
    return {
        "schema_version": EVIDENCE_COMPARISON_VERSION,
        "policy_version": CANONICAL_POLICY_VERSION,
        "report_count": len(ordered),
        "canonical_report_id": canonical_id,
        "provisional_report_id": provisional_id,
        "selected_report_id": selected_id,
        "entries": entries,
    }


def annotate_report_history(reports: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return newest-first report copies with derived stability metadata."""

    source = [dict(report) for report in reports if isinstance(report, dict)]
    state = classify_report_history(source)
    entry_by_id = {str(entry.get("report_id") or ""): entry for entry in state["entries"]}
    annotated: list[dict[str, Any]] = []
    for report in source:
        item = dict(report)
        entry = entry_by_id.get(str(report.get("id") or ""))
        if entry:
            item["stability"] = entry
            item["canonical_status"] = entry["canonical_status"]
        annotated.append(item)
    annotated.sort(
        key=lambda report: (str(report.get("created_at") or ""), str(report.get("id") or "")),
        reverse=True,
    )
    return annotated, state


def annotate_candidate_report(
    report: dict[str, Any],
    previous_reports: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Attach the dry-run stability projection to a newly composed report."""

    history, state = annotate_report_history([*previous_reports, report])
    current_id = str(report.get("id") or "")
    current = next((item for item in history if str(item.get("id") or "") == current_id), dict(report))
    current["evidence_snapshot"] = build_evidence_snapshot(report).to_dict()
    current["canonical_selection"] = {
        "policy_version": CANONICAL_POLICY_VERSION,
        "canonical_report_id": state.get("canonical_report_id"),
        "provisional_report_id": state.get("provisional_report_id"),
        "selected_report_id": state.get("selected_report_id"),
        "enforcement_mode": canonical_enforcement_mode(),
    }
    return current


def selected_report_for_display(
    reports: Iterable[dict[str, Any]],
    *,
    mode: str | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any]]:
    """Choose the visible report while keeping observe mode backwards compatible."""

    annotated, state = annotate_report_history(reports)
    if not annotated:
        return None, annotated, state
    effective_mode = canonical_enforcement_mode(mode)
    enforce = effective_mode == "all" or (effective_mode == "repeated" and len(annotated) >= 2)
    selected_id = str(state.get("selected_report_id") or "")
    if enforce and selected_id:
        selected = next((item for item in annotated if str(item.get("id") or "") == selected_id), None)
        if selected is not None:
            return selected, annotated, state
    return annotated[0], annotated, state


def canonical_enforcement_mode(value: str | None = None) -> str:
    raw = str(value if value is not None else os.environ.get(ENFORCEMENT_ENV, "observe")).strip().lower()
    return raw if raw in ENFORCEMENT_MODES else "observe"


def render_history_dry_run(histories: dict[str, Iterable[dict[str, Any]]]) -> dict[str, Any]:
    """Build a global, read-only impact report grouped by domain."""

    brands: list[dict[str, Any]] = []
    classifications: Counter[str] = Counter()
    repeated = 0
    changed_visible_report = 0
    changed_repeated_visible_report = 0
    for domain in sorted(histories):
        reports = sorted(
            (dict(report) for report in histories[domain] if isinstance(report, dict)),
            key=lambda report: (str(report.get("created_at") or ""), str(report.get("id") or "")),
        )
        state = classify_report_history(reports)
        if state["report_count"] >= 2:
            repeated += 1
        for entry in state["entries"]:
            classifications[str(entry.get("classification") or "unknown")] += 1
        latest = reports[-1] if reports else {}
        latest_report_id = str(latest.get("id") or "") or None
        selected_report_id = state.get("selected_report_id")
        selected = next(
            (
                report
                for report in reports
                if str(report.get("id") or "") == str(selected_report_id or "")
            ),
            {},
        )
        selection_changes = bool(
            latest_report_id
            and selected_report_id
            and latest_report_id != selected_report_id
        )
        if selection_changes:
            changed_visible_report += 1
            if state["report_count"] >= 2:
                changed_repeated_visible_report += 1
        brands.append(
            {
                "domain": domain,
                **state,
                "latest_report_id": latest_report_id,
                "latest_score": latest.get("score"),
                "selected_score": selected.get("score"),
                "score_delta_latest_minus_selected": _numeric_delta(
                    latest.get("score"),
                    selected.get("score"),
                ),
                "selection_changes_visible_report": selection_changes,
            }
        )
    return {
        "schema_version": "canonical-evidence-dry-run-v1",
        "comparison_version": EVIDENCE_COMPARISON_VERSION,
        "policy_version": CANONICAL_POLICY_VERSION,
        "mutates_state": False,
        "brand_count": len(brands),
        "repeated_brand_count": repeated,
        "single_scan_brand_count": len(brands) - repeated,
        "all_mode_changed_brand_count": changed_visible_report,
        "repeated_mode_changed_brand_count": changed_repeated_visible_report,
        "classification_counts": dict(sorted(classifications.items())),
        "brands": brands,
    }


def _canonical_records(report: dict[str, Any]) -> tuple[CanonicalEvidenceRecord, ...]:
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    flow = raw.get("flow") if isinstance(raw.get("flow"), dict) else {}
    candidate = flow.get("candidate") if isinstance(flow.get("candidate"), dict) else {}
    pack = candidate.get("evidence_pack") if isinstance(candidate.get("evidence_pack"), dict) else {}
    rows = pack.get("evidence") if isinstance(pack.get("evidence"), list) else []
    records: dict[str, CanonicalEvidenceRecord] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "unknown").strip().lower()
        evidence_type = str(row.get("evidence_type") or "unknown").strip().lower()
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        source_class = str(metadata.get("source_class") or _infer_source_class(source, evidence_type)).strip().lower()
        url = _normalize_url(str(row.get("url") or ""))
        normalized_content = _normalize_text(row.get("content"))
        if not normalized_content:
            continue
        content_hash = _text_hash(normalized_content)
        locator_basis = url or content_hash
        locator = "|".join((source_class, evidence_type, locator_basis))
        fingerprint = _stable_hash(
            {
                "source_class": source_class,
                "evidence_type": evidence_type,
                "url": url,
                "content_hash": content_hash,
            }
        )
        record = CanonicalEvidenceRecord(
            locator=locator,
            fingerprint=fingerprint,
            source=source,
            source_class=source_class,
            evidence_type=evidence_type,
            url=url,
            content_hash=content_hash,
            normalized_content=normalized_content,
        )
        records[fingerprint] = record
    return tuple(sorted(records.values(), key=lambda item: (item.locator, item.fingerprint)))


def _component_fingerprints(
    report: dict[str, Any],
    records: tuple[CanonicalEvidenceRecord, ...],
) -> dict[str, str]:
    raw_rows = _raw_evidence_rows(report)
    fingerprint_by_ref: dict[str, str] = {}
    canonical_by_identity = {
        (
            record.source_class,
            record.evidence_type,
            record.url,
            record.content_hash,
        ): record.fingerprint
        for record in records
    }
    for row in raw_rows:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        source = str(row.get("source") or "unknown").strip().lower()
        evidence_type = str(row.get("evidence_type") or "unknown").strip().lower()
        source_class = str(metadata.get("source_class") or _infer_source_class(source, evidence_type)).strip().lower()
        identity = (
            source_class,
            evidence_type,
            _normalize_url(str(row.get("url") or "")),
            _text_hash(_normalize_text(row.get("content"))),
        )
        fingerprint = canonical_by_identity.get(identity)
        ref = str(row.get("ref") or "").strip()
        if ref and fingerprint:
            fingerprint_by_ref[ref] = fingerprint

    components: dict[str, str] = {}
    for component in report.get("components") or []:
        if not isinstance(component, dict):
            continue
        key = str(component.get("key") or component.get("component") or "").strip()
        block = component.get("block") if isinstance(component.get("block"), dict) else {}
        refs = []
        for item in block.get("refs") or []:
            ref = str(item.get("ref") or "").strip() if isinstance(item, dict) else str(item or "").strip()
            if ref and ref in fingerprint_by_ref:
                refs.append(fingerprint_by_ref[ref])
        if key:
            components[key] = _stable_hash(sorted(set(refs)))
    return dict(sorted(components.items()))


def _raw_evidence_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    flow = raw.get("flow") if isinstance(raw.get("flow"), dict) else {}
    candidate = flow.get("candidate") if isinstance(flow.get("candidate"), dict) else {}
    pack = candidate.get("evidence_pack") if isinstance(candidate.get("evidence_pack"), dict) else {}
    rows = pack.get("evidence") if isinstance(pack.get("evidence"), list) else []
    return [row for row in rows if isinstance(row, dict)]


def _evaluation_fingerprint(report: dict[str, Any]) -> str:
    rows = []
    for component in report.get("components") or []:
        if not isinstance(component, dict):
            continue
        tiles = [
            {
                "id": str(tile.get("id") or ""),
                "state": str(tile.get("estado") or tile.get("state") or ""),
            }
            for tile in component.get("tile_profile") or []
            if isinstance(tile, dict)
        ]
        tiles.sort(key=lambda tile: tile["id"])
        rows.append(
            {
                "key": str(component.get("key") or component.get("component") or ""),
                "status": str(component.get("status") or ""),
                "score": component.get("score"),
                "tiles": tiles,
            }
        )
    rows.sort(key=lambda item: item["key"])
    return _stable_hash(rows)


def _interpretation_fingerprint(report: dict[str, Any]) -> str:
    rows = []
    for block in report.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        rows.append(
            {
                "name": str(block.get("name") or ""),
                "detected": block.get("detected") is True,
                "content": _normalize_text(block.get("content")),
                "coverage_status": str(block.get("coverage_status") or ""),
            }
        )
    rows.sort(key=lambda item: item["name"])
    return _stable_hash(rows)


def _changed_components(
    baseline_report: dict[str, Any],
    candidate_report: dict[str, Any],
) -> list[dict[str, Any]]:
    baseline = {
        str(item.get("key") or item.get("component") or ""): item
        for item in baseline_report.get("components") or []
        if isinstance(item, dict)
    }
    candidate = {
        str(item.get("key") or item.get("component") or ""): item
        for item in candidate_report.get("components") or []
        if isinstance(item, dict)
    }
    changes: list[dict[str, Any]] = []
    for key in sorted(baseline.keys() | candidate.keys()):
        before = baseline.get(key) or {}
        after = candidate.get(key) or {}
        before_score = before.get("score")
        after_score = after.get("score")
        before_status = str(before.get("status") or "")
        after_status = str(after.get("status") or "")
        before_tiles = _tile_state_map(before)
        after_tiles = _tile_state_map(after)
        if (before_score, before_status, before_tiles) == (after_score, after_status, after_tiles):
            continue
        changes.append(
            {
                "component": key,
                "score_before": before_score,
                "score_after": after_score,
                "status_before": before_status,
                "status_after": after_status,
                "changed_tiles": sorted(
                    tile
                    for tile in before_tiles.keys() | after_tiles.keys()
                    if before_tiles.get(tile) != after_tiles.get(tile)
                ),
            }
        )
    return changes


def _history_entry(
    report: dict[str, Any],
    snapshot: EvidenceSnapshot,
    *,
    classification: str,
    canonical_status: str,
    reasons: list[str],
    baseline_comparison: EvidenceComparison | None,
    previous_comparison: EvidenceComparison | None,
) -> dict[str, Any]:
    return {
        "schema_version": EVIDENCE_COMPARISON_VERSION,
        "policy_version": CANONICAL_POLICY_VERSION,
        "report_id": str(report.get("id") or ""),
        "created_at": report.get("created_at"),
        "score": report.get("score"),
        "reliability_status": str(report.get("reliability_status") or "unknown"),
        "classification": classification,
        "canonical_status": canonical_status,
        "reason_codes": list(dict.fromkeys(reasons)),
        "evidence_snapshot": snapshot.to_dict(),
        "baseline_comparison": baseline_comparison.to_dict() if baseline_comparison else None,
        "previous_comparison": previous_comparison.to_dict() if previous_comparison else None,
    }


def _eligible_for_canonical(report: dict[str, Any], snapshot: EvidenceSnapshot) -> bool:
    return (
        not snapshot.invalid
        and snapshot.reliability_status == "reliable"
        and snapshot.acquisition_state == "pass"
        and bool(snapshot.material_records)
    )


def _acquisition_regression_reasons(
    baseline: EvidenceSnapshot,
    candidate: EvidenceSnapshot,
    *,
    lost_urls: list[str],
) -> list[str]:
    reasons: list[str] = []
    baseline_owned_urls = _source_class_urls(baseline, "owned_copy")
    candidate_owned_urls = _source_class_urls(candidate, "owned_copy")
    baseline_external_urls = _source_class_urls(baseline, "external_proof")
    candidate_external_urls = _source_class_urls(candidate, "external_proof")
    if _gate_rank(candidate.acquisition_state) < _gate_rank(baseline.acquisition_state):
        reasons.append("acquisition_gate_worsened")
    if baseline_owned_urls - candidate_owned_urls:
        reasons.append("owned_evidence_lost")
    if baseline_external_urls - candidate_external_urls:
        reasons.append("external_evidence_lost")
    if candidate.independent_external_cluster_count < baseline.independent_external_cluster_count:
        reasons.append("independent_external_coverage_lost")
    if candidate.visual_evidence_count < baseline.visual_evidence_count:
        reasons.append("visual_evidence_lost")
    if lost_urls and candidate.acquisition_state in {"warning", "blocked", "unknown", ""}:
        reasons.append("previous_evidence_not_reacquired")
    return list(dict.fromkeys(reasons))


def _source_class_urls(snapshot: EvidenceSnapshot, source_class: str) -> set[str]:
    return {
        record.url
        for record in snapshot.material_records
        if record.source_class == source_class and record.url
    }


def _external_clusters(records: list[CanonicalEvidenceRecord]) -> list[list[CanonicalEvidenceRecord]]:
    clusters: list[list[CanonicalEvidenceRecord]] = []
    for record in records:
        tokens = _content_tokens(record.normalized_content)
        target: list[CanonicalEvidenceRecord] | None = None
        for cluster in clusters:
            representative_tokens = _content_tokens(cluster[0].normalized_content)
            if (
                record.content_hash == cluster[0].content_hash
                or (
                    len(tokens) >= 20
                    and len(representative_tokens) >= 20
                    and _jaccard(tokens, representative_tokens) >= 0.82
                )
            ):
                target = cluster
                break
        if target is None:
            clusters.append([record])
        else:
            target.append(record)
    return clusters


def _external_cluster_fingerprints(records: list[CanonicalEvidenceRecord]) -> list[str]:
    fingerprints = []
    for cluster in _external_clusters(records):
        fingerprints.append(
            _stable_hash(
                {
                    "urls": sorted(record.url for record in cluster if record.url),
                    "content": sorted(record.content_hash for record in cluster),
                }
            )
        )
    return sorted(fingerprints)


def _normalize_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = str(parsed.hostname or "").lower().removeprefix("www.")
        if not host:
            return raw.lower().rstrip("/")
        port = f":{parsed.port}" if parsed.port else ""
        path = re.sub(r"/+", "/", parsed.path or "/")
        if path != "/":
            path = path.rstrip("/")
        query = [
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
        ]
        return urlunparse(
            (
                (parsed.scheme or "https").lower(),
                f"{host}{port}",
                path,
                "",
                urlencode(sorted(query)),
                "",
            )
        ).rstrip("/")
    except (TypeError, ValueError):
        return raw.lower().rstrip("/")


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.casefold().split())


def _content_tokens(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(value))


def _infer_source_class(source: str, evidence_type: str) -> str:
    if evidence_type.startswith(("acquisition.", "diagnostic.")):
        return "acquisition_metadata"
    if source in {"web", "owned", "website"}:
        return "owned_copy"
    if source in {"exa", "searchapi"} or evidence_type.startswith("external_proof"):
        return "external_proof"
    if "visual" in source or "visual" in evidence_type:
        return "visual_signal"
    return "other"


def _has_not_evaluated_component(report: dict[str, Any]) -> bool:
    return any(
        isinstance(component, dict) and str(component.get("status") or "") == "not_evaluated"
        for component in report.get("components") or []
    )


def _tile_state_map(component: dict[str, Any]) -> dict[str, str]:
    return {
        str(tile.get("id") or ""): str(tile.get("estado") or tile.get("state") or "")
        for tile in component.get("tile_profile") or []
        if isinstance(tile, dict) and str(tile.get("id") or "")
    }


def _gate_rank(state: str) -> int:
    return {"pass": 3, "warning": 2, "unknown": 1, "": 1, "blocked": 0}.get(state, 1)


def _numeric_delta(left: Any, right: Any) -> float | int | None:
    if isinstance(left, bool) or isinstance(right, bool):
        return None
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    delta = left - right
    return int(delta) if float(delta).is_integer() else round(float(delta), 4)


def _jaccard(left: set[Any], right: set[Any]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
