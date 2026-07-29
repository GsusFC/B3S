"""Additive scoring-memory preview built from immutable scanner reports.

The scanner already accepts a tile as ``ok`` only when the evaluator attaches
an evidence quote. This projection adds two stricter requirements before that
quote may survive a later acquisition dropout:

* the quote must be reproducible inside a persisted evidence record; and
* that source record must have eligible (or explicitly accepted) brand identity.

The projection never overwrites a report and never changes runtime scoring. It
only answers the promotion question: "what would the score be if a later
``sin_evidencia`` tile reused previously accepted, still-eligible evidence?"
An explicit later ``no`` is retained as a conflict and is never auto-recovered.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
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
from src.sv9.rubric import (
    BASE_COMPONENTS,
    COMPONENTS,
    MAGNETISM_CAP_BASE_THRESHOLD,
    MAGNETISM_CAP_VALUE,
    RUBRIC_VERSION,
    STATUS_SCORED,
    component_points,
)


EVIDENCE_SCORING_MEMORY_PREVIEW_VERSION = (
    "evidence-scoring-memory-preview-v1"
)
EVIDENCE_SCORING_MEMORY_PREVIEW_POLICY_VERSION = (
    "evidence-scoring-memory-preview-policy-v1"
)
EVIDENCE_SCORING_MEMORY_VERSION = "evidence-scoring-memory-v1"
EVIDENCE_SCORING_MEMORY_ENTRY_VERSION = (
    "evidence-scoring-memory-entry-v1"
)


class EvidenceScoringMemoryPreviewError(ValueError):
    """The reports cannot form one safe scoring-memory preview."""


def build_evidence_scoring_memory_preview(
    reports: Iterable[dict[str, Any]],
    *,
    mode: str = "shadow",
    evidence_adjudications: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build a deterministic, non-authoritative additive memory preview."""

    ordered = _ordered_reports(reports)
    effective_mode = (
        "shadow"
        if str(mode or "").strip().lower() == "shadow"
        else "disabled"
    )
    result = _empty_result(mode=effective_mode, report_count=len(ordered))
    if effective_mode != "shadow" or not ordered:
        result["state_fingerprint"] = _state_fingerprint(result)
        return result

    domains = {
        _normalize_domain(str(report.get("url") or ""))
        for report in ordered
        if _normalize_domain(str(report.get("url") or ""))
    }
    if len(domains) != 1:
        raise EvidenceScoringMemoryPreviewError(
            "scoring memory requires reports from exactly one canonical domain"
        )
    brand_domain = next(iter(domains))
    latest = ordered[-1]
    latest_report_id = str(latest.get("id") or "")
    latest_rubric_version = _rubric_version(latest)

    identity_projection = build_evidence_memory_identity_v2(
        ordered,
        mode="shadow",
        adjudications=evidence_adjudications,
    )
    identity_by_id = {
        str(entry.get("evidence_id") or ""): entry
        for entry in identity_projection.get("entries") or []
        if isinstance(entry, dict)
        and str(entry.get("evidence_id") or "")
    }

    entry_accumulators: dict[str, dict[str, Any]] = {}
    excluded_reasons: Counter[str] = Counter()
    excluded_candidate_count = 0
    for report in ordered:
        report_id = str(report.get("id") or "")
        observed_at = _timestamp(report.get("created_at")).isoformat()
        rubric_version = _rubric_version(report)
        source_records = _source_records(
            report,
            identity_by_id=identity_by_id,
        )
        for component_key, verdict in _tile_verdicts(report):
            if str(verdict.get("estado") or "") != "ok":
                continue
            quote = normalize_evidence_text(verdict.get("evidencia"))
            if not quote:
                excluded_reasons["scanner_ok_without_quote"] += 1
                excluded_candidate_count += 1
                continue
            matched, rejected_reasons = _match_source_records(
                quote,
                source_records=source_records,
            )
            excluded_reasons.update(rejected_reasons)
            if not matched:
                excluded_reasons["quote_without_eligible_literal_source"] += 1
                excluded_candidate_count += 1
                continue

            tile_id = str(
                verdict.get("id") or verdict.get("tile_id") or ""
            ).strip()
            source_urls = sorted(
                {
                    str(row["url"])
                    for row in matched
                    if str(row.get("url") or "")
                }
            )
            tile_evidence_id = stable_artifact_digest(
                EVIDENCE_SCORING_MEMORY_ENTRY_VERSION,
                {
                    "brand_domain": brand_domain,
                    "component_key": component_key,
                    "tile_id": tile_id,
                    "quote": quote.casefold(),
                    "source_urls": source_urls,
                },
            )
            occurrence = {
                "report_id": report_id,
                "observed_at": observed_at,
                "rubric_version": rubric_version,
                "present_in_latest": report_id == latest_report_id,
                "source_evidence_ids": sorted(
                    {
                        str(row["evidence_id"])
                        for row in matched
                    }
                ),
                "source_refs": sorted(
                    {
                        str(row["ref"])
                        for row in matched
                        if str(row.get("ref") or "")
                    }
                ),
            }
            accumulator = entry_accumulators.setdefault(
                tile_evidence_id,
                {
                    "tile_evidence_id": tile_evidence_id,
                    "component_key": component_key,
                    "tile_id": tile_id,
                    "quote": quote,
                    "source_urls": set(),
                    "source_classes": set(),
                    "source_evidence_ids": set(),
                    "report_ids": set(),
                    "observed_at": [],
                    "rubric_versions": set(),
                    "occurrences": {},
                    "acceptance_basis": set(),
                },
            )
            accumulator["source_urls"].update(source_urls)
            accumulator["source_classes"].update(
                str(row.get("source_class") or "")
                for row in matched
                if str(row.get("source_class") or "")
            )
            accumulator["source_evidence_ids"].update(
                occurrence["source_evidence_ids"]
            )
            accumulator["report_ids"].add(report_id)
            accumulator["observed_at"].append(observed_at)
            accumulator["rubric_versions"].add(rubric_version)
            accumulator["occurrences"][report_id] = occurrence
            accumulator["acceptance_basis"].update(
                {
                    "scanner_tile_ok",
                    "literal_source_match",
                    *(
                        str(row["acceptance_basis"])
                        for row in matched
                    ),
                }
            )

    entries = [
        _finalize_entry(
            accumulator,
            latest_report_id=latest_report_id,
        )
        for accumulator in entry_accumulators.values()
    ]
    entries.sort(
        key=lambda entry: (
            str(entry["component_key"]),
            str(entry["tile_id"]),
            str(entry["tile_evidence_id"]),
        )
    )

    latest_states = _latest_tile_states(latest)
    compatible_prior_by_tile: dict[
        tuple[str, str], list[dict[str, Any]]
    ] = {}
    for entry in entries:
        compatible_occurrences = [
            occurrence
            for occurrence in entry.get("occurrences") or []
            if isinstance(occurrence, dict)
            and str(occurrence.get("report_id") or "")
            != latest_report_id
            and str(occurrence.get("rubric_version") or "")
            == latest_rubric_version
        ]
        if not compatible_occurrences:
            continue
        key = (
            str(entry.get("component_key") or ""),
            str(entry.get("tile_id") or ""),
        )
        compatible_prior_by_tile.setdefault(key, []).append(entry)

    recoveries: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for key, prior_entries in sorted(compatible_prior_by_tile.items()):
        component_key, tile_id = key
        latest_state = latest_states.get(key)
        if latest_state == "sin_evidencia":
            recoveries.append(
                {
                    "component_key": component_key,
                    "tile_id": tile_id,
                    "latest_state": latest_state,
                    "preview_state": "ok",
                    "reason": (
                        "prior_reproducible_accepted_evidence_recovers_"
                        "latest_blind_spot"
                    ),
                    "tile_evidence_ids": sorted(
                        str(entry["tile_evidence_id"])
                        for entry in prior_entries
                    ),
                }
            )
        elif latest_state == "no":
            conflicts.append(
                {
                    "component_key": component_key,
                    "tile_id": tile_id,
                    "latest_state": latest_state,
                    "preview_state": "no",
                    "reason": (
                        "explicit_latest_negative_is_not_overridden_by_"
                        "historical_evidence"
                    ),
                    "tile_evidence_ids": sorted(
                        str(entry["tile_evidence_id"])
                        for entry in prior_entries
                    ),
                }
            )

    scoring = _scoring_preview(
        latest,
        recovered_tiles={
            (
                str(row["component_key"]),
                str(row["tile_id"]),
            )
            for row in recoveries
        },
    )
    memory_version = stable_artifact_digest(
        EVIDENCE_SCORING_MEMORY_VERSION,
        {
            "brand_domain": brand_domain,
            "rubric_version": latest_rubric_version,
            "entries": [
                {
                    "tile_evidence_id": entry["tile_evidence_id"],
                    "component_key": entry["component_key"],
                    "tile_id": entry["tile_id"],
                    "quote_hash": stable_artifact_digest(
                        "evidence-scoring-memory-quote-v1",
                        {"quote": str(entry["quote"]).casefold()},
                    ),
                    "source_evidence_ids": entry[
                        "source_evidence_ids"
                    ],
                }
                for entry in entries
            ],
        },
    )

    result.update(
        {
            "brand": {
                "name": str(latest.get("brand_name") or ""),
                "domain": brand_domain,
            },
            "latest_report_id": latest_report_id,
            "rubric_version": latest_rubric_version,
            "memory_version": memory_version,
            "summary": {
                "accepted_evidence_count": len(entries),
                "accepted_evidence_occurrence_count": sum(
                    int(entry["observation_count"])
                    for entry in entries
                ),
                "recovered_blind_spot_count": len(recoveries),
                "explicit_negative_conflict_count": len(conflicts),
                "excluded_candidate_count": excluded_candidate_count,
                "excluded_reason_counts": dict(
                    sorted(excluded_reasons.items())
                ),
            },
            "accepted_evidence": entries,
            "recoveries": recoveries,
            "conflicts": conflicts,
            "scoring": scoring,
        }
    )
    result["state_fingerprint"] = _state_fingerprint(result)
    return result


def apply_recovery_review_gate(
    preview: dict[str, Any],
    latest_report: dict[str, Any],
    *,
    accepted_tile_evidence_ids: Iterable[str],
) -> dict[str, Any]:
    """Compute a non-authoritative shadow score from reviewed recoveries only."""

    if (
        preview.get("runtime_effect") is not False
        or preview.get("authority") is not False
    ):
        raise EvidenceScoringMemoryPreviewError(
            "review gating requires a non-authoritative preview"
        )
    accepted_ids = {
        str(value)
        for value in accepted_tile_evidence_ids
        if str(value)
    }
    approved_recoveries: list[dict[str, Any]] = []
    gated_recoveries: list[dict[str, Any]] = []
    for recovery in preview.get("recoveries") or []:
        if not isinstance(recovery, dict):
            continue
        evidence_ids = {
            str(value)
            for value in recovery.get("tile_evidence_ids") or []
            if str(value)
        }
        approved_ids = sorted(evidence_ids & accepted_ids)
        approved = bool(approved_ids)
        gated = {
            **dict(recovery),
            "semantic_review_state": (
                "accepted" if approved else "not_accepted"
            ),
            "accepted_tile_evidence_ids": approved_ids,
        }
        gated_recoveries.append(gated)
        if approved:
            approved_recoveries.append(gated)

    reviewed_scoring = _scoring_preview(
        latest_report,
        recovered_tiles={
            (
                str(row.get("component_key") or ""),
                str(row.get("tile_id") or ""),
            )
            for row in approved_recoveries
        },
    )
    result = deepcopy(preview)
    result["reviewed_shadow"] = {
        "schema_version": "evidence-scoring-reviewed-shadow-v1",
        "runtime_effect": False,
        "authority": False,
        "automatic_scoring_effect": False,
        "review_required": True,
        "candidate_recovery_count": len(gated_recoveries),
        "accepted_recovery_count": len(approved_recoveries),
        "recoveries": gated_recoveries,
        "scoring": reviewed_scoring,
        "warnings": [
            "only_explicitly_accepted_semantic_mappings_are_scored",
            "reviewed_score_remains_shadow_only",
        ],
    }
    result["state_fingerprint"] = _state_fingerprint(result)
    return result


def _empty_result(*, mode: str, report_count: int) -> dict[str, Any]:
    return {
        "schema_version": EVIDENCE_SCORING_MEMORY_PREVIEW_VERSION,
        "policy_version": (
            EVIDENCE_SCORING_MEMORY_PREVIEW_POLICY_VERSION
        ),
        "mode": mode,
        "runtime_effect": False,
        "authority": False,
        "automatic_scoring_effect": False,
        "brand": {"name": "", "domain": ""},
        "report_count": report_count,
        "latest_report_id": None,
        "rubric_version": None,
        "memory_version": None,
        "summary": {
            "accepted_evidence_count": 0,
            "accepted_evidence_occurrence_count": 0,
            "recovered_blind_spot_count": 0,
            "explicit_negative_conflict_count": 0,
            "excluded_candidate_count": 0,
            "excluded_reason_counts": {},
        },
        "accepted_evidence": [],
        "recoveries": [],
        "conflicts": [],
        "scoring": {
            "status": "unavailable",
            "current_score": None,
            "recomputed_current_score": None,
            "preview_score": None,
            "score_delta": None,
            "current_score_reproducible": False,
            "magnetism_capped": None,
        },
        "warnings": [
            "preview_only_no_runtime_scoring_effect",
            "reports_and_source_evidence_remain_immutable",
            "only_literal_identity_eligible_scanner_ok_evidence_is_reused",
            "only_latest_blind_spots_can_be_recovered",
            "explicit_latest_negative_is_never_overridden",
        ],
    }


def _source_records(
    report: dict[str, Any],
    *,
    identity_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
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
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        metadata = (
            row.get("metadata")
            if isinstance(row.get("metadata"), dict)
            else {}
        )
        source = str(row.get("source") or "unknown").strip().lower()
        evidence_type = (
            str(row.get("evidence_type") or "unknown").strip().lower()
        )
        source_class = str(
            metadata.get("source_class")
            or _infer_source_class(source, evidence_type)
        ).strip().lower()
        if source_class not in MATERIAL_SOURCE_CLASSES:
            continue
        content = normalize_evidence_text(row.get("content"))
        if not content:
            continue
        url = normalize_evidence_url(row.get("url"))
        evidence_id = canonical_evidence_digest(
            source_class=source_class,
            evidence_type=evidence_type,
            url=url,
            content=content,
        )
        identity = identity_by_id.get(evidence_id) or {}
        result.append(
            {
                "evidence_id": evidence_id,
                "ref": str(row.get("ref") or ""),
                "url": url,
                "source_class": source_class,
                "evidence_type": evidence_type,
                "content": content,
                "identity_status": str(
                    identity.get("identity_status") or "unverified"
                ),
                "identity_state": str(
                    identity.get("state") or "unknown"
                ),
                "adjudication_state": str(
                    identity.get("adjudication_state") or "proposed"
                ),
            }
        )
    return result


def _match_source_records(
    quote: str,
    *,
    source_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    normalized_quote = normalize_evidence_text(quote).casefold()
    accepted: list[dict[str, Any]] = []
    rejected_reasons: Counter[str] = Counter()
    for row in source_records:
        if normalized_quote not in str(row.get("content") or "").casefold():
            continue
        adjudication_state = str(
            row.get("adjudication_state") or "proposed"
        )
        identity_status = str(
            row.get("identity_status") or "unverified"
        )
        identity_state = str(row.get("identity_state") or "unknown")
        if identity_state == "stale_candidate":
            rejected_reasons["literal_source_is_stale"] += 1
            continue
        if adjudication_state == "accepted":
            acceptance_basis = "human_identity_accepted"
        elif (
            adjudication_state in {"proposed", "revoked"}
            and identity_status == "eligible"
        ):
            acceptance_basis = "deterministic_identity_eligible"
        else:
            rejected_reasons[
                f"literal_source_identity_{adjudication_state}_"
                f"{identity_status}"
            ] += 1
            continue
        accepted.append(
            {
                **row,
                "acceptance_basis": acceptance_basis,
            }
        )
    accepted.sort(
        key=lambda row: (
            len(str(row.get("content") or "")),
            str(row.get("url") or ""),
            str(row.get("ref") or ""),
            str(row.get("evidence_id") or ""),
        )
    )
    return accepted, rejected_reasons


def _tile_verdicts(
    report: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    result = _evaluation_result(report)
    components = (
        result.get("components")
        if isinstance(result.get("components"), dict)
        else {}
    )
    verdicts: list[tuple[str, dict[str, Any]]] = []
    for component_key, component in components.items():
        if component_key not in COMPONENTS or not isinstance(component, dict):
            continue
        profile = (
            component.get("tile_profile")
            if isinstance(component.get("tile_profile"), list)
            else []
        )
        for verdict in profile:
            if isinstance(verdict, dict):
                verdicts.append((str(component_key), verdict))
    return verdicts


def _latest_tile_states(
    report: dict[str, Any],
) -> dict[tuple[str, str], str]:
    return {
        (
            component_key,
            str(
                verdict.get("id")
                or verdict.get("tile_id")
                or ""
            ).strip(),
        ): str(verdict.get("estado") or "")
        for component_key, verdict in _tile_verdicts(report)
    }


def _finalize_entry(
    accumulator: dict[str, Any],
    *,
    latest_report_id: str,
) -> dict[str, Any]:
    occurrences = sorted(
        accumulator["occurrences"].values(),
        key=lambda row: (
            str(row["observed_at"]),
            str(row["report_id"]),
        ),
    )
    observed_at = sorted(accumulator["observed_at"])
    report_ids = sorted(accumulator["report_ids"])
    return {
        "tile_evidence_id": str(accumulator["tile_evidence_id"]),
        "component_key": str(accumulator["component_key"]),
        "tile_id": str(accumulator["tile_id"]),
        "quote": str(accumulator["quote"]),
        "source_urls": sorted(accumulator["source_urls"]),
        "source_classes": sorted(accumulator["source_classes"]),
        "source_evidence_ids": sorted(
            accumulator["source_evidence_ids"]
        ),
        "acceptance_basis": sorted(accumulator["acceptance_basis"]),
        "first_seen_at": observed_at[0],
        "last_seen_at": observed_at[-1],
        "observation_count": len(occurrences),
        "report_ids": report_ids,
        "rubric_versions": sorted(accumulator["rubric_versions"]),
        "present_in_latest": latest_report_id in report_ids,
        "occurrences": occurrences,
    }


def _scoring_preview(
    latest: dict[str, Any],
    *,
    recovered_tiles: set[tuple[str, str]],
) -> dict[str, Any]:
    result = _evaluation_result(latest)
    components = (
        result.get("components")
        if isinstance(result.get("components"), dict)
        else {}
    )
    if set(components) != set(COMPONENTS):
        return {
            "status": "unavailable_incomplete_component_set",
            "current_score": _number(
                latest.get("score", result.get("brand3_score"))
            ),
            "recomputed_current_score": None,
            "preview_score": None,
            "score_delta": None,
            "current_score_reproducible": False,
            "magnetism_capped": None,
        }

    current_scores: dict[str, int] = {}
    preview_scores: dict[str, int] = {}
    for component_key in COMPONENTS:
        component = components.get(component_key)
        if not isinstance(component, dict):
            return _unavailable_score(latest, result)
        if str(component.get("status") or "") != STATUS_SCORED:
            current_scores[component_key] = 0
            preview_scores[component_key] = 0
            continue
        profile = (
            component.get("tile_profile")
            if isinstance(component.get("tile_profile"), list)
            else []
        )
        if len(profile) != int(COMPONENTS[component_key]["scale"]):
            return _unavailable_score(latest, result)
        current_lit = 0
        preview_lit = 0
        for verdict in profile:
            if not isinstance(verdict, dict):
                return _unavailable_score(latest, result)
            tile_id = str(
                verdict.get("id") or verdict.get("tile_id") or ""
            ).strip()
            state = str(verdict.get("estado") or "")
            if state == "ok":
                current_lit += 1
                preview_lit += 1
            elif (
                state == "sin_evidencia"
                and (component_key, tile_id) in recovered_tiles
            ):
                preview_lit += 1
        current_scores[component_key] = current_lit
        preview_scores[component_key] = preview_lit

    current_total, current_capped = _aggregate_scores(current_scores)
    preview_total, preview_capped = _aggregate_scores(preview_scores)
    persisted_current = _number(
        latest.get("score", result.get("brand3_score"))
    )
    reproducible = (
        persisted_current is not None
        and float(current_total) == float(persisted_current)
    )
    if not reproducible:
        return {
            "status": "blocked_current_score_not_reproducible",
            "current_score": persisted_current,
            "recomputed_current_score": current_total,
            "preview_score": None,
            "score_delta": None,
            "current_score_reproducible": False,
            "magnetism_capped": current_capped,
        }
    return {
        "status": "preview_available",
        "current_score": persisted_current,
        "recomputed_current_score": current_total,
        "preview_score": preview_total,
        "score_delta": preview_total - current_total,
        "current_score_reproducible": True,
        "magnetism_capped": preview_capped,
    }


def _unavailable_score(
    latest: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "unavailable_incomplete_tile_profile",
        "current_score": _number(
            latest.get("score", result.get("brand3_score"))
        ),
        "recomputed_current_score": None,
        "preview_score": None,
        "score_delta": None,
        "current_score_reproducible": False,
        "magnetism_capped": None,
    }


def _aggregate_scores(scores: dict[str, int]) -> tuple[int, bool]:
    normalized_base = [
        scores[key] * (10 / int(COMPONENTS[key]["scale"]))
        for key in BASE_COMPONENTS
    ]
    base_average = sum(normalized_base) / len(normalized_base)
    effective_scores = dict(scores)
    magnetism_capped = (
        base_average < MAGNETISM_CAP_BASE_THRESHOLD
        and effective_scores["magnetism"] > MAGNETISM_CAP_VALUE
    )
    if magnetism_capped:
        effective_scores["magnetism"] = MAGNETISM_CAP_VALUE
    return (
        sum(
            component_points(key, effective_scores[key])
            for key in COMPONENTS
        ),
        magnetism_capped,
    )


def _evaluation_result(report: dict[str, Any]) -> dict[str, Any]:
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    sv9 = raw.get("sv9") if isinstance(raw.get("sv9"), dict) else {}
    return (
        sv9.get("result")
        if isinstance(sv9.get("result"), dict)
        else {}
    )


def _rubric_version(report: dict[str, Any]) -> str:
    return str(
        _evaluation_result(report).get("rubric_version")
        or RUBRIC_VERSION
    ).strip()


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


def _timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def _normalize_domain(value: str) -> str:
    parsed = urlparse(
        value if "://" in value else f"https://{value}"
    )
    return (
        str(parsed.hostname or "")
        .strip(".")
        .lower()
        .removeprefix("www.")
    )


def _number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _state_fingerprint(payload: dict[str, Any]) -> str:
    return stable_artifact_digest(
        EVIDENCE_SCORING_MEMORY_PREVIEW_VERSION,
        {
            key: value
            for key, value in payload.items()
            if key != "state_fingerprint"
        },
    )
