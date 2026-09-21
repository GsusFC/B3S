"""Publish the deterministic report produced by an approved SV9 review."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable, Mapping
from typing import Any

from src.services.evidence_vault_sv9_authority_report import (
    project_vault_authority_publication,
)
from web.report_store import save_report
from web.scan_runner import _authority_scanner_payload, _compose_report


class Sv9ReviewPublicationError(RuntimeError):
    """The approved candidate cannot be projected into a safe report."""


def publish_approved_sv9_review(
    *,
    resolution_result: Mapping[str, Any],
    domain: str,
    repository: Any,
    publish: Callable[[dict[str, Any]], None] = save_report,
) -> dict[str, Any]:
    """Persist one immutable report only for an approved resolution.

    The candidate report is rebuilt from the persisted candidate assessment and
    its exact capture observation.  The prior report is never edited.
    """

    resolution = resolution_result.get("resolution")
    if isinstance(resolution, Mapping) and resolution.get("decision") == "reject":
        return {"state": "not_published", "reason": "review_rejected"}
    authority = resolution_result.get("authority")
    if not isinstance(resolution, Mapping) or not isinstance(authority, Mapping):
        raise Sv9ReviewPublicationError("approved SV9 resolution has no authority projection")
    if resolution.get("decision") != "approve":
        raise Sv9ReviewPublicationError("SV9 resolution decision is invalid")
    candidate = authority.get("accepted_candidate")
    if not isinstance(candidate, Mapping):
        raise Sv9ReviewPublicationError("approved SV9 resolution has no candidate")
    source_scan_id = str(resolution.get("source_scan_id") or "").strip()
    resolution_id = str(resolution.get("id") or "").strip()
    if not source_scan_id or not resolution_id:
        raise Sv9ReviewPublicationError("approved SV9 resolution identity is invalid")
    report_id = f"sv9-review-{resolution_id}"

    operation = repository.get_capture_operation_plan(source_scan_id)
    if not isinstance(operation, Mapping):
        raise Sv9ReviewPublicationError("approved SV9 capture operation is unavailable")
    observation = operation.get("raw_observation")
    if not isinstance(observation, Mapping):
        raise Sv9ReviewPublicationError("approved SV9 capture observation is unavailable")

    compact_candidate = {
        key: candidate[key]
        for key in (
            "id",
            "canonical_plan_fingerprint",
            "complete_record_fingerprint",
            "assessment_fingerprint",
            "score_fingerprint",
            "source_scan_id",
        )
    }
    application_result = {
        "status": "authority_advanced",
        "reason_codes": ["human_review_approved"],
        "evaluation_status": "candidate_available",
        "candidate": compact_candidate,
        "signed_delta": None,
        "authority": deepcopy(dict(authority)),
    }
    publication = project_vault_authority_publication(
        application_result,
        source_scan_id,
    )
    if publication.get("action") != "publish_current":
        raise Sv9ReviewPublicationError("approved SV9 candidate did not produce a publishable authority")

    payload = _authority_scanner_payload(
        publication=publication,
        canonical_snapshot={"acquisition_gate": {"state": "approved_review"}},
        canonical_source_capture=None,
        gate={"state": "approved_review"},
        report_observation=observation,
    )
    report = _compose_report(
        report_id,
        str(observation.get("canonical_url") or domain),
        str(observation.get("brand_name") or domain),
        payload,
    )
    # Keep retries byte-stable so a lost HTTP response cannot create a second
    # logical report or turn an idempotent retry into a conflict.
    if isinstance(resolution.get("created_at"), str):
        report["created_at"] = resolution["created_at"]
    report["review_publication"] = {
        "resolution_id": str(resolution["id"]),
        "decision": "approve",
        "source_scan_id": source_scan_id,
        "authority_event_id": str(resolution["successor_authority_event_id"]),
        "authority": True,
    }
    publish(report)
    return {
        "state": "published",
        "report_id": report_id,
        "score": report.get("score"),
        "score_fingerprint": report.get("score_fingerprint"),
    }
