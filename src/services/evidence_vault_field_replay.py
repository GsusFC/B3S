"""Offline real-brand replay gate for normalized historical evidence packs.

This gate validates planning and delta scoping only. The fixtures deliberately
are not represented as raw acquisition envelopes and cannot authorize cutover.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlparse

from src.services.evidence_vault_canonical_core import canonical_fingerprint
from src.services.evidence_vault_incremental_refresh import (
    build_vault_scan_plan,
    validate_vault_scan_plan,
)


EVIDENCE_VAULT_FIELD_REPLAY_MANIFEST_VERSION = (
    "evidence-vault-normalized-field-replay-manifest-v1"
)
EVIDENCE_VAULT_FIELD_REPLAY_RESULT_VERSION = (
    "evidence-vault-normalized-field-replay-result-v1"
)


def postgres_database_name(dsn: str) -> str:
    """Return the exact URI database token used by the shadow safety check."""

    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        return ""
    name = unquote(parsed.path.removeprefix("/"))
    return name if name and "/" not in name else ""


def has_libpq_connection_override_environment(
    environment: Mapping[str, str],
) -> bool:
    """Reject ambient libpq routing that could override the checked URI."""

    return any(
        str(environment.get(key) or "").strip()
        for key in (
            "PGHOST",
            "PGHOSTADDR",
            "PGDATABASE",
            "PGSERVICE",
            "PGSERVICEFILE",
        )
    )


def is_local_postgres_dsn(dsn: str) -> bool:
    """Accept only exact loopback hosts or the fixed disposable socket dir."""

    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query).intersection({"hostaddr", "service", "servicefile", "dbname"}):
        return False
    query_hosts = query.get("host", [])
    if query_hosts:
        return (
            parsed.hostname is None
            and len(query_hosts) == 1
            and Path(query_hosts[0]).resolve() == Path("/private/tmp").resolve()
        )
    return parsed.hostname is not None and parsed.hostname.lower() in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


class EvidenceVaultFieldReplayError(ValueError):
    """A frozen normalized field replay violates its declared contract."""


def validate_evidence_vault_field_replay(
    manifest_path: str | Path,
) -> dict[str, Any]:
    """Validate two-brand baseline/no-op/material planning without network or LLM."""

    path = Path(manifest_path).resolve()
    manifest = _json_object(path)
    _validate_manifest_shape(manifest)
    cases: list[dict[str, Any]] = []
    for raw_case in manifest["cases"]:
        case = dict(raw_case)
        observations = [
            _load_observation(path.parent, row)
            for row in case["observations"]
        ]
        baseline = build_vault_scan_plan(
            brand_identity=case["brand_identity"],
            subject_url=case["subject_url"],
            mode="baseline",
            current_evidence_records=observations[0]["pack"]["evidence"],
        )
        _assert_plan_boundary(baseline)
        expected_baseline = case["expected_baseline"]
        if (
            len(baseline["semantic_context"]["evidence_fingerprints"])
            != expected_baseline["context_count"]
            or len(baseline["operations"]["classify_evidence_fingerprints"])
            != expected_baseline["classify_count"]
        ):
            raise EvidenceVaultFieldReplayError(
                f"{case['case_id']} baseline counts changed"
            )

        identical: list[dict[str, Any]] = []
        for observation in observations:
            parent = canonical_fingerprint(
                "evidence-vault-field-replay-parent-v1",
                {
                    "case_id": case["case_id"],
                    "report_id": observation["report_id"],
                },
            )
            plan = build_vault_scan_plan(
                brand_identity=case["brand_identity"],
                subject_url=case["subject_url"],
                mode="incremental_refresh",
                current_evidence_records=observation["pack"]["evidence"],
                previous_capture_evidence_records=observation["pack"]["evidence"],
                known_evidence_records=observation["pack"]["evidence"],
                canonical_memory_version=parent,
            )
            _assert_plan_boundary(plan)
            if (
                plan["canonical_impact"] != "none"
                or plan["operations"]["llm_required"] is not False
                or plan["operations"]["create_candidate_packet"] is not False
                or any(plan["operations"]["reevaluate_tile_ids"])
            ):
                raise EvidenceVaultFieldReplayError(
                    f"{case['case_id']} identical replay requested work"
                )
            identical.append(
                {
                    "report_id": observation["report_id"],
                    "operation_plan_fingerprint": plan[
                        "operation_plan_fingerprint"
                    ],
                    "delta_fingerprint": plan["delta"]["delta_fingerprint"],
                    "llm_required": False,
                }
            )

        expected_by_pair = {
            (row["from_report_id"], row["to_report_id"]): row
            for row in case["expected_transitions"]
        }
        known: list[dict[str, Any]] = []
        transitions: list[dict[str, Any]] = []
        for previous, current in zip(
            observations[:-1],
            observations[1:],
            strict=True,
        ):
            known.extend(previous["pack"]["evidence"])
            pair = (previous["report_id"], current["report_id"])
            expected = expected_by_pair.get(pair)
            if expected is None:
                raise EvidenceVaultFieldReplayError(
                    f"{case['case_id']} transition is not declared: {pair}"
                )
            parent = canonical_fingerprint(
                "evidence-vault-field-replay-parent-v1",
                {"case_id": case["case_id"], "transition": list(pair)},
            )
            plan = build_vault_scan_plan(
                brand_identity=case["brand_identity"],
                subject_url=case["subject_url"],
                mode="incremental_refresh",
                current_evidence_records=current["pack"]["evidence"],
                previous_capture_evidence_records=previous["pack"]["evidence"],
                known_evidence_records=known,
                canonical_memory_version=parent,
            )
            _assert_plan_boundary(plan)
            summary = plan["delta"]["summary"]
            if (
                plan["canonical_impact"] != expected["canonical_impact"]
                or len(plan["operations"]["classify_evidence_fingerprints"])
                != expected["classify_count"]
                or any(
                    summary.get(key) != value
                    for key, value in expected["summary"].items()
                )
            ):
                raise EvidenceVaultFieldReplayError(
                    f"{case['case_id']} transition changed: {pair}"
                )
            transitions.append(
                {
                    "from_report_id": pair[0],
                    "to_report_id": pair[1],
                    "historical_diagnostic_score_change": [
                        previous["diagnostic_score"],
                        current["diagnostic_score"],
                    ],
                    "canonical_impact": plan["canonical_impact"],
                    "classify_count": len(
                        plan["operations"]["classify_evidence_fingerprints"]
                    ),
                    "reevaluate_tile_count": len(
                        plan["operations"]["reevaluate_tile_ids"]
                    ),
                    "delta_summary": summary,
                    "semantic_context_fingerprint": plan["semantic_context"][
                        "context_fingerprint"
                    ],
                    "operation_plan_fingerprint": plan[
                        "operation_plan_fingerprint"
                    ],
                    "delta_fingerprint": plan["delta"]["delta_fingerprint"],
                }
            )
        if len(transitions) != len(expected_by_pair):
            raise EvidenceVaultFieldReplayError(
                f"{case['case_id']} declared transition count mismatch"
            )
        cases.append(
            {
                "case_id": case["case_id"],
                "brand_identity": case["brand_identity"],
                "observation_count": len(observations),
                "baseline": {
                    "context_count": len(
                        baseline["semantic_context"]["evidence_fingerprints"]
                    ),
                    "classify_count": len(
                        baseline["operations"]["classify_evidence_fingerprints"]
                    ),
                    "operation_plan_fingerprint": baseline[
                        "operation_plan_fingerprint"
                    ],
                },
                "identical_replays": identical,
                "transitions": transitions,
            }
        )

    downstream_reference = _validate_downstream_reference(
        path.parent,
        manifest["downstream_reference"],
    )
    result = {
        "schema_version": EVIDENCE_VAULT_FIELD_REPLAY_RESULT_VERSION,
        "source_manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "replay_level": manifest["replay_level"],
        "gate_status": "pass",
        "authority": False,
        "runtime_effect": False,
        "cutover_authorized": False,
        "validated_claims": [
            "normalized_evidence_fixture_integrity",
            "two_brand_plan_contract",
            "identical_refresh_zero_llm_plan",
            "scoped_material_delta_plan",
            "legacy_downstream_reference_integrity",
        ],
        "not_validated": [
            "raw_acquisition_replay",
            "real_llm_semantic_quality",
            "human_review_quality",
            "legacy_review_import_as_v2_authority",
            "authorized_memory_or_score",
            "production_cutover",
        ],
        "cases": cases,
        "downstream_reference": downstream_reference,
    }
    result["result_fingerprint"] = canonical_fingerprint(
        EVIDENCE_VAULT_FIELD_REPLAY_RESULT_VERSION,
        result,
    )
    return result


def _validate_downstream_reference(
    base: Path,
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(raw, Mapping)
        or raw.get("classification")
        != "derived_non_authoritative_legacy_reference"
        or raw.get("authority") is not False
        or raw.get("runtime_effect") is not False
        or not isinstance(raw.get("files"), list)
        or not isinstance(raw.get("expected"), Mapping)
    ):
        raise EvidenceVaultFieldReplayError(
            "downstream reference declaration is invalid"
        )
    files: dict[str, Path] = {}
    for entry in raw["files"]:
        if not isinstance(entry, Mapping):
            raise EvidenceVaultFieldReplayError(
                "downstream reference file entry is invalid"
            )
        relative = str(entry.get("file") or "")
        target = (base / relative).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError as exc:
            raise EvidenceVaultFieldReplayError(
                "downstream reference escapes fixture directory"
            ) from exc
        if (
            not target.is_file()
            or hashlib.sha256(target.read_bytes()).hexdigest()
            != entry.get("sha256")
        ):
            raise EvidenceVaultFieldReplayError(
                f"downstream reference integrity failed: {relative}"
            )
        files[target.name] = target
    required = {
        "preview.json",
        "identity-review-manifest.json",
        "identity-candidates.jsonl",
        "identity-reviews.jsonl",
    }
    if set(files) != required:
        raise EvidenceVaultFieldReplayError(
            "downstream reference file set is invalid"
        )
    preview = _json_object(files["preview.json"])
    identity_manifest = _json_object(files["identity-review-manifest.json"])
    candidates = _json_lines(files["identity-candidates.jsonl"])
    reviews = _json_lines(files["identity-reviews.jsonl"])
    expected = raw["expected"]
    occurrence_report_ids = {
        str(occurrence.get("report_id") or "")
        for evidence in preview.get("accepted_evidence") or []
        for occurrence in evidence.get("occurrences") or []
    }
    chronology = list(expected.get("report_chronology") or [])
    scoring = preview.get("scoring") or {}
    trajectory = (preview.get("tile_evolution") or {}).get(
        "score_trajectory"
    ) or {}
    summary = preview.get("summary") or {}
    tiles = (preview.get("tile_evolution") or {}).get("tiles") or []
    if (
        preview.get("domain") != raw.get("brand_identity")
        or preview.get("authority") is not False
        or preview.get("runtime_effect") is not False
        or preview.get("automatic_scoring_effect") is not False
        or preview.get("report_count") != len(chronology)
        or occurrence_report_ids != set(chronology)
        or preview.get("latest_report_id") != chronology[-1]
        or summary.get("accepted_evidence_count")
        != expected.get("accepted_evidence_count")
        or summary.get("accepted_evidence_occurrence_count")
        != expected.get("accepted_evidence_occurrence_count")
        or len(preview.get("accepted_evidence") or [])
        != expected.get("accepted_evidence_count")
        or sum(
            len(row.get("occurrences") or [])
            for row in preview.get("accepted_evidence") or []
        )
        != expected.get("accepted_evidence_occurrence_count")
        or len(tiles) != expected.get("tile_count")
        or scoring.get("current_score") != expected.get("current_score")
        or scoring.get("recomputed_current_score")
        != expected.get("recomputed_current_score")
        or scoring.get("preview_score") != expected.get("preview_score")
        or scoring.get("current_score_reproducible") is not True
        or trajectory.get("previous_score")
        != expected.get("stable_previous_score")
        or trajectory.get("latest_score")
        != expected.get("stable_latest_score")
    ):
        raise EvidenceVaultFieldReplayError(
            "downstream preview golden invariants changed"
        )
    candidate_ids = {str(row.get("case_id") or "") for row in candidates}
    review_ids = {str(row.get("case_id") or "") for row in reviews}
    decision_counts: dict[str, int] = {}
    for review in reviews:
        decision = str(review.get("decision") or "")
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
    if (
        identity_manifest.get("authority") is not False
        or identity_manifest.get("runtime_effect") is not False
        or identity_manifest.get("brand_identity") != raw.get("brand_identity")
        or identity_manifest.get("review_case_count")
        != expected.get("identity_review_case_count")
        or len(candidates) != expected.get("identity_review_case_count")
        or len(reviews) != expected.get("identity_review_case_count")
        or candidate_ids != review_ids
        or decision_counts != expected.get("identity_review_decisions")
        or any(
            review.get("event_id") is not None
            or review.get("reviewer_id") is not None
            for review in reviews
        )
        or any(
            row.get("candidate_packet_fingerprint")
            != identity_manifest.get("candidate_packet_fingerprint")
            for row in candidates
        )
        or any(
            row.get("review_packet_fingerprint")
            != identity_manifest.get("review_packet_fingerprint")
            for row in reviews
        )
    ):
        raise EvidenceVaultFieldReplayError(
            "downstream identity review golden invariants changed"
        )
    return {
        "reference_id": raw["reference_id"],
        "classification": raw["classification"],
        "brand_identity": raw["brand_identity"],
        "report_chronology": chronology,
        "accepted_evidence_count": summary["accepted_evidence_count"],
        "accepted_evidence_occurrence_count": summary[
            "accepted_evidence_occurrence_count"
        ],
        "tile_count": len(tiles),
        "current_score": scoring["current_score"],
        "recomputed_current_score": scoring["recomputed_current_score"],
        "preview_score": scoring["preview_score"],
        "identity_review_case_count": len(reviews),
        "identity_review_decisions": decision_counts,
        "authority": False,
        "importable_as_v2_review": False,
    }


def _json_lines(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceVaultFieldReplayError(
            f"cannot read downstream JSONL: {path}"
        ) from exc
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise EvidenceVaultFieldReplayError(
                f"invalid downstream JSONL row {index}: {path}"
            ) from exc
        if not isinstance(value, dict):
            raise EvidenceVaultFieldReplayError(
                f"downstream JSONL row {index} must be an object"
            )
        rows.append(value)
    return rows


def _load_observation(
    base: Path,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise EvidenceVaultFieldReplayError("observation entry must be an object")
    pack_path = (base / str(row.get("evidence_pack_file") or "")).resolve()
    if pack_path.parent != base.resolve():
        raise EvidenceVaultFieldReplayError("evidence pack escapes fixture directory")
    pack = _json_object(pack_path)
    rendered = json.dumps(
        pack,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if (
        hashlib.sha256(rendered).hexdigest()
        != row.get("evidence_pack_canonical_sha256")
        or not isinstance(pack.get("evidence"), list)
        or len(pack["evidence"]) != row.get("evidence_record_count")
        or pack.get("schema_version") != "brand-evidence-pack-v1"
    ):
        raise EvidenceVaultFieldReplayError(
            f"normalized evidence fixture is invalid: {pack_path.name}"
        )
    for evidence in pack["evidence"]:
        if not isinstance(evidence, Mapping):
            raise EvidenceVaultFieldReplayError("evidence row must be an object")
        metadata = evidence.get("metadata")
        if isinstance(metadata, Mapping) and any(
            key in metadata
            for key in {
                "relevant_blocks",
                "stance",
                "identity_match_llm",
                "specificity",
                "semantic_labeling_version",
            }
        ):
            raise EvidenceVaultFieldReplayError(
                "fixture contains derived semantic label metadata"
            )
    return {
        **dict(row),
        "pack": pack,
    }


def _assert_plan_boundary(plan: Mapping[str, Any]) -> None:
    validate_vault_scan_plan(plan)
    operations = plan["operations"]
    if (
        plan["authority"] is not False
        or plan["runtime_effect"] is not False
        or operations["recalculate_canonical_score"] is not False
        or operations["create_canonical_report"] is not False
        or operations["create_diagnostic_report"] is not False
    ):
        raise EvidenceVaultFieldReplayError(
            "field replay plan crossed the non-authoritative boundary"
        )


def _validate_manifest_shape(manifest: Mapping[str, Any]) -> None:
    if (
        manifest.get("schema_version")
        != EVIDENCE_VAULT_FIELD_REPLAY_MANIFEST_VERSION
        or manifest.get("replay_level") != "normalized_evidence_pack_only"
        or manifest.get("authority") is not False
        or manifest.get("runtime_effect") is not False
        or not isinstance(manifest.get("cases"), list)
        or not isinstance(manifest.get("downstream_reference"), Mapping)
        or len(manifest["cases"]) != 2
        or len(
            {
                row.get("case_id")
                for row in manifest["cases"]
                if isinstance(row, Mapping) and row.get("case_id")
            }
        )
        != 2
        or len(
            {
                row.get("brand_identity")
                for row in manifest["cases"]
                if isinstance(row, Mapping) and row.get("brand_identity")
            }
        )
        != 2
    ):
        raise EvidenceVaultFieldReplayError("field replay manifest is invalid")
    for case in manifest["cases"]:
        if (
            not isinstance(case, Mapping)
            or not isinstance(case.get("observations"), list)
            or len(case["observations"]) < 2
            or not isinstance(case.get("expected_transitions"), list)
            or not isinstance(case.get("expected_baseline"), Mapping)
        ):
            raise EvidenceVaultFieldReplayError("field replay case is invalid")


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise EvidenceVaultFieldReplayError(
            f"cannot read field replay JSON: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise EvidenceVaultFieldReplayError("field replay JSON must be an object")
    return value


__all__ = [
    "EVIDENCE_VAULT_FIELD_REPLAY_MANIFEST_VERSION",
    "EVIDENCE_VAULT_FIELD_REPLAY_RESULT_VERSION",
    "EvidenceVaultFieldReplayError",
    "has_libpq_connection_override_environment",
    "is_local_postgres_dsn",
    "postgres_database_name",
    "validate_evidence_vault_field_replay",
]
