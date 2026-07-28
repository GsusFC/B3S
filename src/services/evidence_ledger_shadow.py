"""Longitudinal evidence memory with no scoring or selection authority."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from typing import Any, Iterable
from urllib.parse import urlparse

from src.services.scanner_evidence_comparison import (
    MATERIAL_SOURCE_CLASSES,
    CanonicalEvidenceRecord,
    build_evidence_snapshot,
    canonical_evidence_records,
)


EVIDENCE_LEDGER_SHADOW_VERSION = "evidence-ledger-shadow-v1"
EVIDENCE_LEDGER_SHADOW_POLICY_VERSION = "evidence-ledger-shadow-policy-v1"
EVIDENCE_LEDGER_MODE_ENV = "B3S_EVIDENCE_LEDGER_MODE"
EVIDENCE_LEDGER_MODES = {"disabled", "shadow"}

_VALIDATION_OBSERVATIONS = 2
_TTL_DAYS = {
    "owned_copy": 180,
    "external_proof": 90,
    "visual_signal": 30,
    "derived_strategy": 90,
    "other": 90,
}
_CURRENT_STATES = {"observed", "repeated", "validation_candidate"}


def evidence_ledger_mode(value: str | None = None) -> str:
    raw = str(
        value if value is not None else os.environ.get(EVIDENCE_LEDGER_MODE_ENV, "disabled")
    ).strip().lower()
    return raw if raw in EVIDENCE_LEDGER_MODES else "disabled"


def build_evidence_ledger_shadow(
    reports: Iterable[dict[str, Any]],
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic, recomputable evidence ledger proposal.

    The result deliberately has no ``validated`` or ``retired`` state. It only
    proposes candidates for later policy evaluation.
    """

    ordered = _ordered_reports(reports)
    effective_mode = evidence_ledger_mode(mode)
    latest = ordered[-1] if ordered else {}
    base = {
        "schema_version": EVIDENCE_LEDGER_SHADOW_VERSION,
        "policy_version": EVIDENCE_LEDGER_SHADOW_POLICY_VERSION,
        "mode": effective_mode,
        "runtime_effect": False,
        "brand": {
            "name": str(latest.get("brand_name") or ""),
            "domain": _domain(str(latest.get("url") or "")),
        },
        "report_count": len(ordered),
        "latest_report_id": str(latest.get("id") or "") or None,
        "summary": _empty_summary(),
        "policy": {
            "automatic_validation": False,
            "automatic_retirement": False,
            "automatic_contradiction_detection": False,
            "validation_candidate_min_observations": _VALIDATION_OBSERVATIONS,
            "ttl_days_by_source_class": dict(_TTL_DAYS),
        },
        "warnings": [
            "shadow_only_no_scoring_or_selection_effect",
            "validation_candidates_are_not_validated_facts",
            "single_miss_never_retires_evidence",
            "semantic_contradiction_detection_not_implemented",
            "source_syndication_is_not_automatic_corroboration",
        ],
        "entries": [],
    }
    if effective_mode != "shadow" or not ordered:
        base["state_fingerprint"] = _state_fingerprint(base)
        return base

    latest_id = str(latest.get("id") or "")
    latest_at = _timestamp(latest.get("created_at"))
    brand_domain = str(base["brand"]["domain"])
    observations_by_fingerprint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    record_by_fingerprint: dict[str, CanonicalEvidenceRecord] = {}
    fingerprints_by_locator: dict[str, set[str]] = defaultdict(set)
    latest_fingerprints: set[str] = set()
    latest_fingerprints_by_locator: dict[str, set[str]] = defaultdict(set)

    for report in ordered:
        report_id = str(report.get("id") or "")
        observed_at = _timestamp(report.get("created_at"))
        snapshot = build_evidence_snapshot(report)
        seen_in_report: set[str] = set()
        for record in canonical_evidence_records(report):
            if record.source_class not in MATERIAL_SOURCE_CLASSES:
                continue
            if record.fingerprint in seen_in_report:
                continue
            seen_in_report.add(record.fingerprint)
            record_by_fingerprint[record.fingerprint] = record
            fingerprints_by_locator[record.locator].add(record.fingerprint)
            if report_id == latest_id:
                latest_fingerprints.add(record.fingerprint)
                latest_fingerprints_by_locator[record.locator].add(record.fingerprint)
            observations_by_fingerprint[record.fingerprint].append(
                {
                    "report_id": report_id,
                    "observed_at": observed_at.isoformat(),
                    "identity_match": record.identity_match,
                    "acquisition_state": snapshot.acquisition_state,
                    "invalid": snapshot.invalid,
                }
            )

    entries: list[dict[str, Any]] = []
    for fingerprint in sorted(record_by_fingerprint):
        record = record_by_fingerprint[fingerprint]
        observations = sorted(
            observations_by_fingerprint[fingerprint],
            key=lambda item: (str(item["observed_at"]), str(item["report_id"])),
        )
        first_seen = _timestamp(observations[0]["observed_at"])
        last_seen = _timestamp(observations[-1]["observed_at"])
        present_in_latest = fingerprint in latest_fingerprints
        age_days = max(0, (latest_at - last_seen).days)
        ttl_days = _TTL_DAYS.get(record.source_class, _TTL_DAYS["other"])
        qualified_observations = sum(
            1
            for observation in observations
            if _validation_observation_eligible(
                record,
                observation,
                brand_domain=brand_domain,
            )
        )
        state, reasons = _entry_state(
            record=record,
            observation_count=len(observations),
            qualified_observations=qualified_observations,
            present_in_latest=present_in_latest,
            latest_locator_fingerprints=latest_fingerprints_by_locator.get(
                record.locator,
                set(),
            ),
            age_days=age_days,
            ttl_days=ttl_days,
        )
        entries.append(
            {
                "evidence_fingerprint": fingerprint,
                "locator_hash": _hash(record.locator),
                "source_class": record.source_class,
                "evidence_type": record.evidence_type,
                "url": record.url,
                "source_domain": _domain(record.url),
                "content_hash": record.content_hash,
                "state": state,
                "reason_codes": reasons,
                "present_in_latest": present_in_latest,
                "first_seen_at": first_seen.isoformat(),
                "last_seen_at": last_seen.isoformat(),
                "age_days": age_days,
                "ttl_days": ttl_days,
                "observation_count": len(observations),
                "qualified_observation_count": qualified_observations,
                "report_ids": [str(item["report_id"]) for item in observations],
                "identity_matches": sorted(
                    {
                        str(item["identity_match"])
                        for item in observations
                        if str(item["identity_match"])
                    }
                ),
                "locator_variant_count": len(fingerprints_by_locator[record.locator]),
                "observations": observations,
            }
        )

    state_counts = Counter(str(entry["state"]) for entry in entries)
    current_entries = [entry for entry in entries if entry["state"] in _CURRENT_STATES]
    source_counts = Counter(str(entry["source_class"]) for entry in current_entries)
    repeated_current = sum(
        1 for entry in current_entries if int(entry["observation_count"]) >= _VALIDATION_OBSERVATIONS
    )
    base["entries"] = entries
    base["summary"] = {
        "entry_count": len(entries),
        "current_entry_count": len(current_entries),
        "new_in_latest_count": sum(
            1
            for entry in current_entries
            if int(entry["observation_count"]) == 1
            and latest_id in entry["report_ids"]
        ),
        "repeated_current_count": repeated_current,
        "repeat_rate": round(repeated_current / max(1, len(current_entries)), 4),
        "state_counts": dict(sorted(state_counts.items())),
        "current_source_class_counts": dict(sorted(source_counts.items())),
    }
    base["state_fingerprint"] = _state_fingerprint(base)
    return base


def _entry_state(
    *,
    record: CanonicalEvidenceRecord,
    observation_count: int,
    qualified_observations: int,
    present_in_latest: bool,
    latest_locator_fingerprints: set[str],
    age_days: int,
    ttl_days: int,
) -> tuple[str, list[str]]:
    if present_in_latest:
        if (
            observation_count >= _VALIDATION_OBSERVATIONS
            and qualified_observations >= _VALIDATION_OBSERVATIONS
        ):
            return "validation_candidate", [
                "exact_evidence_repeated",
                "identity_and_acquisition_eligible",
                "shadow_candidate_only",
            ]
        if observation_count >= _VALIDATION_OBSERVATIONS:
            return "repeated", [
                "exact_evidence_repeated",
                "validation_eligibility_incomplete",
            ]
        return "observed", ["first_exact_observation"]
    if latest_locator_fingerprints:
        return "changed_candidate", [
            "same_locator_new_content_observed",
            "semantic_contradiction_not_assumed",
        ]
    if age_days >= ttl_days:
        return "stale_candidate", [
            "not_present_in_latest_capture",
            "shadow_ttl_elapsed",
            "automatic_retirement_disabled",
        ]
    return "not_reacquired", [
        "not_present_in_latest_capture",
        "single_miss_never_retires_evidence",
    ]


def _validation_observation_eligible(
    record: CanonicalEvidenceRecord,
    observation: dict[str, Any],
    *,
    brand_domain: str,
) -> bool:
    if observation.get("invalid") is True:
        return False
    if str(observation.get("acquisition_state") or "") not in {"pass", "warning"}:
        return False
    if record.source_class == "owned_copy":
        source_domain = _domain(record.url)
        return bool(
            source_domain
            and brand_domain
            and (
                source_domain == brand_domain
                or source_domain.endswith(f".{brand_domain}")
            )
        )
    if record.source_class == "external_proof":
        return str(observation.get("identity_match") or "") in {"domain", "brand_name"}
    return False


def _ordered_reports(reports: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for report in reports:
        if not isinstance(report, dict):
            continue
        report_id = str(report.get("id") or "").strip()
        if not report_id:
            continue
        by_id[report_id] = dict(report)
    return sorted(
        by_id.values(),
        key=lambda report: (
            _timestamp(report.get("created_at")),
            str(report.get("id") or ""),
        ),
    )


def _empty_summary() -> dict[str, Any]:
    return {
        "entry_count": 0,
        "current_entry_count": 0,
        "new_in_latest_count": 0,
        "repeated_current_count": 0,
        "repeat_rate": 0.0,
        "state_counts": {},
        "current_source_class_counts": {},
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


def _domain(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return str(parsed.hostname or "").strip(".").lower().removeprefix("www.")


def _state_fingerprint(payload: dict[str, Any]) -> str:
    return _hash(
        {
            "schema_version": payload["schema_version"],
            "policy_version": payload["policy_version"],
            "mode": payload["mode"],
            "brand": payload["brand"],
            "latest_report_id": payload["latest_report_id"],
            "summary": payload["summary"],
            "entries": payload["entries"],
        }
    )


def _hash(value: Any) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
