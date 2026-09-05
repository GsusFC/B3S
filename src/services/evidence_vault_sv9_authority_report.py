"""Pure projection of persisted SV9 authority into Scanner-compatible output."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from src.services.evidence_vault_sv9_authority_projection import (
    validate_persisted_evidence_vault_sv9_authority_projection,
)
from src.services.scanner_report_assessment import (
    assessment_projection_from_envelope,
    assessment_projection_from_report,
)
from src.sv9.assessment_kernel import (
    SV9_ASSESSMENT_OUTPUT_V2_VERSION,
    SV9_ASSESSMENT_OUTPUT_VERSION,
    SV9_SCANNER_ASSESSMENT_V2_VERSION,
    SV9_SCANNER_ASSESSMENT_VERSION,
    build_scanner_sv9_assessment,
    validate_sv9_assessment_output,
)
from src.sv9.rubric import COHERENCIA_REVIEW_THRESHOLD, PRESENTATION_ORDER, STATUS_NOT_DETECTED, STATUS_SCORED

# fmt: off
_APPLICATION_FIELDS = frozenset("status reason_codes evaluation_status candidate signed_delta authority".split())
_COMPACT_CANDIDATE_FIELDS = frozenset("id source_scan_id canonical_plan_fingerprint complete_record_fingerprint assessment_fingerprint score_fingerprint".split())
_SHA = re.compile(r"[0-9a-f]{64}\Z")

# This is the complete public contract emitted by authority_application.  Slot
# markers deliberately describe presence, not the nested object contents.
VALID_APPLICATION_ROWS = frozenset({
    ("authority_established", "candidate_available", "C", None, "A"),
    ("authority_advanced", "candidate_available", "C", None, "A"),
    ("authority_retained", "no_new_score", None, None, "A"),
    ("review_required", "review_required", None, "D", "A"),
    ("review_required", "candidate_available", "C", None, "A"),
    ("review_required", "candidate_available", "C", None, None),
    ("first_run_unresolved", "review_required", None, None, None),
    ("first_run_unresolved", "no_new_score", None, None, None),
    ("authority_conflict", "candidate_available", None, None, None),
    ("authority_conflict", "review_required", None, None, None),
    ("authority_conflict", "review_required", None, None, "A"),
    ("authority_conflict", "no_new_score", None, None, None),
    ("authority_conflict", None, None, None, None),
})


def project_vault_authority_publication(application_result: Mapping[str, Any], current_scan_id: str, accepted_source_report: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Publish current authority, retain a verified report, or expose no score."""
    current = _text(current_scan_id)
    try:
        result = _application(application_result); status = result["status"]
        if current is None: raise ValueError
        if status in {"authority_established", "authority_advanced"}:
            if accepted_source_report is not None: raise ValueError
            wrapper, candidate, assessment = _authority(result["authority"]); expected = "adopt" if status == "authority_established" else "supersede"; compact = _compact_candidate(result["candidate"])
            if wrapper["reopen_review_overlay"] is not None or wrapper["active_authority_event"]["event_type"] != expected or compact["source_scan_id"] != current or any(compact[name] != candidate[name] for name in _COMPACT_CANDIDATE_FIELDS): raise ValueError
            envelope, projection = _scanner(assessment)
            if not _bound(candidate, wrapper, assessment, projection): raise ValueError
            return {"action": "publish_current", "source_report_id": None, "scanner_payload": _payload(current, envelope, projection)}
        if status == "authority_retained":
            # Adoption is durable before report persistence. Exact replay of
            # that same capture may only need to materialize its accepted score.
            wrapper, candidate, assessment = _authority(result["authority"])
            if candidate["source_scan_id"] == current and accepted_source_report is None:
                if wrapper["reopen_review_overlay"] is not None: raise ValueError
                envelope, projection = _scanner(assessment)
                if not _bound(candidate, wrapper, assessment, projection): raise ValueError
                return {"action": "publish_current", "source_report_id": None, "scanner_payload": _payload(current, envelope, projection)}
            return _retain(result, current, accepted_source_report, False)
        if status == "review_required" and result["evaluation_status"] == "candidate_available" and result["authority"] is not None:
            if _compact_candidate(result["candidate"])["source_scan_id"] != current: raise ValueError
            return _retain(result, current, accepted_source_report, False)
        if status == "review_required" and result["evaluation_status"] == "review_required": return _retain(result, current, accepted_source_report, True)
    except Exception:
        pass
    return _none(current)


def _clone(value: Any) -> Any:
    try: encoded = json.dumps(value, ensure_ascii=False, allow_nan=False); encoded.encode("utf-8"); return json.loads(encoded)
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc: raise ValueError from exc

def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value): raise ValueError
    result = _clone(dict(value))
    if type(result) is not dict: raise ValueError
    return result

def _text(value: Any) -> str | None:
    if type(value) is not str or not value or value != value.strip(): return None
    try: value.encode("utf-8")
    except UnicodeError: return None
    return value

def _same(left: Any, right: Any) -> bool: return json.dumps(left, ensure_ascii=False, allow_nan=False, sort_keys=True) == json.dumps(right, ensure_ascii=False, allow_nan=False, sort_keys=True)
def _number(left: Any, right: Any) -> bool: return type(left) in {int, float} and type(right) in {int, float} and _same(left, right)

def _compact_candidate(value: Any) -> dict[str, str]:
    result = _mapping(value)
    if set(result) != _COMPACT_CANDIDATE_FIELDS or _text(result.get("source_scan_id")) is None: raise ValueError
    try: valid = str(UUID(result["id"])) == result["id"]
    except (AttributeError, TypeError, ValueError): valid = False
    if not valid or any(type(result[name]) is not str or _SHA.fullmatch(result[name]) is None for name in _COMPACT_CANDIDATE_FIELDS - {"id", "source_scan_id"}): raise ValueError
    return result

def _application(value: Any) -> dict[str, Any]:
    result = _mapping(value); evaluation = result.get("evaluation_status")
    if set(result) != _APPLICATION_FIELDS or _text(result.get("status")) is None or type(result["reason_codes"]) is not list or any(_text(code) is None for code in result["reason_codes"]) or evaluation is not None and _text(evaluation) is None: raise ValueError
    if any(result[name] is not None and type(result[name]) is not dict for name in ("candidate", "signed_delta", "authority")): raise ValueError
    if result["candidate"] is not None: result["candidate"] = _compact_candidate(result["candidate"])
    row = (result["status"], evaluation, "C" if result["candidate"] is not None else None, "D" if result["signed_delta"] is not None else None, "A" if result["authority"] is not None else None)
    if row not in VALID_APPLICATION_ROWS: raise ValueError
    if result["authority"] is not None: _authority(result["authority"])
    return result

def _authority(value: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    wrapper = validate_persisted_evidence_vault_sv9_authority_projection(_mapping(value)); candidate = wrapper["accepted_candidate"]; assessment = _clone(candidate["assessment"])
    validate_sv9_assessment_output(assessment)
    if not _same(wrapper["assessment"], assessment) or any(candidate.get(name) != assessment.get(name) for name in ("assessment_fingerprint", "score_fingerprint")) or not _number(wrapper["score"], assessment["sv9_score"]): raise ValueError
    return wrapper, candidate, assessment

def _scanner(assessment: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    output = _mapping(assessment); validate_sv9_assessment_output(output); version = output["schema_version"]
    schema = {SV9_ASSESSMENT_OUTPUT_VERSION: SV9_SCANNER_ASSESSMENT_VERSION, SV9_ASSESSMENT_OUTPUT_V2_VERSION: SV9_SCANNER_ASSESSMENT_V2_VERSION}.get(version)
    if schema is None or output["tile_count"] + sum(row["scale"] for row in output.get("component_sentinels", [])) != 80: raise ValueError
    envelope = output | {"schema_version": schema, "assessment_schema_version": version, "expected_tile_count": 80, "availability": "available", "reason_codes": []}; projection = assessment_projection_from_envelope(envelope)
    if projection["availability"] != "available": raise ValueError
    return envelope, projection

def _bound(candidate: Mapping[str, Any], wrapper: Mapping[str, Any], assessment: Mapping[str, Any], projection: Mapping[str, Any]) -> bool:
    return all(_same(candidate.get(name), assessment[name]) for name in ("assessment_fingerprint", "score_fingerprint")) and _number(projection["sv9_score"], assessment["sv9_score"]) and _number(wrapper["score"], projection["sv9_score"])

def _retain(result: Mapping[str, Any], current: str, report: Any, pending: bool) -> dict[str, Any]:
    wrapper, candidate, assessment = _authority(result["authority"]); overlay = wrapper["reopen_review_overlay"]
    if pending:
        if overlay is None or wrapper["current_head"]["event_type"] != "reopen" or wrapper["current_head"]["request"]["source_scan_id"] != current or not _same(result["signed_delta"], overlay["signed_delta"]): raise ValueError
    elif overlay is not None: raise ValueError
    envelope, projection = _scanner(assessment)
    if not _bound(candidate, wrapper, assessment, projection): raise ValueError
    source = _mapping(report); projected = assessment_projection_from_report(source, required=True); raw, report_id = source.get("raw"), source.get("id")
    if type(raw) is not dict or raw.get("schema_version") != "sv9-flow-sv9-shadow-eval-v1" or raw.get("source_run_id") != candidate["source_scan_id"] or projected["availability"] != "available" or not _same(projected["assessment"], envelope) or not _number(projected["sv9_score"], wrapper["score"]) or any(not _same(projected.get(name), candidate[name]) for name in ("assessment_fingerprint", "score_fingerprint")): raise ValueError
    if report_id != candidate["source_scan_id"]:
        # A resume successor changes report identity, never capture provenance.
        capture = raw.get("source_capture")
        if type(report_id) is not str or re.fullmatch(r"exact-resume-[0-9a-f]{64}", report_id) is None or type(capture) is not dict or set(capture) != {"source_scan_id", "observation_hash", "capture_hash"} or capture["source_scan_id"] != candidate["source_scan_id"] or any(_text(capture[key]) is None for key in ("observation_hash", "capture_hash")): raise ValueError
    return {"action": "retain_source", "source_report_id": report_id, "scanner_payload": None}

def _payload(source: str | None, assessment: Mapping[str, Any] | None = None, projection: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if assessment is None:
        assessment = build_scanner_sv9_assessment({}); aliases = {"brand3_score": None, "base_average": None, "magnetism_capped": None, "reliability_status": "broken", "not_detected": [], "not_evaluated": list(PRESENTATION_ORDER), "components": {}, "assessment": assessment, "assessment_fingerprint": None, "score_fingerprint": None}; extras = {"needs_review": False, "total_blind_spots": 0}
    else:
        if projection is None: raise ValueError
        rows, components, missing, blind = projection["component_projections"], {}, [], 0
        for key in PRESENTATION_ORDER:
            row, profile = rows[key], _clone(rows[key]["tile_profile"])
            if row["status"] not in {STATUS_SCORED, STATUS_NOT_DETECTED}: raise ValueError
            states = {state: [tile["id"] for tile in profile if tile["estado"] == state] for state in ("ok", "no", "sin_evidencia")}
            components[key] = {"component": key, "status": row["status"], "score": row["score"], "points": row["points"], "scale": row["scale"], "tile_profile": profile, "blind_spot_count": row["blind"], "lit_tiles": states["ok"], "off_tiles": states["no"], "blind_spot_tiles": states["sin_evidencia"]}; missing += [key] if row["status"] == STATUS_NOT_DETECTED else []; blind += row["blind"]
        review = components["coherencia"]["status"] == STATUS_SCORED and components["coherencia"]["score"] <= COHERENCIA_REVIEW_THRESHOLD; reliability = "shadow" if review or missing or blind > 2 else "reliable" if not blind else "usable"
        aliases = {"brand3_score": projection["sv9_score"], "base_average": projection["base_average"], "magnetism_capped": projection["magnetism_capped"], "reliability_status": reliability, "not_detected": missing, "not_evaluated": [], "components": components, "assessment": assessment, "assessment_fingerprint": projection["assessment_fingerprint"], "score_fingerprint": projection["score_fingerprint"]}; extras = {"needs_review": review, "total_blind_spots": blind}
    result = _clone(aliases) | extras
    return {"schema_version": "sv9-flow-sv9-shadow-eval-v1", "source_run_id": source, "sv9": _clone(aliases) | {"result": result}}

def _none(current: str | None) -> dict[str, Any]: return {"action": "record_no_score", "source_report_id": None, "scanner_payload": _payload(current)}
# fmt: on
