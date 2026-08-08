"""Pure, non-authoritative acquisition replay and reviewed lineage exports.

The historical report replay in this module is deliberately a *derivation*.
A persisted report can prove the acquisition candidate embedded in that report;
it cannot prove that the candidate was the scanner's original raw capture envelope.
Likewise, a reviewed lineage seed/export is content-addressed transport material
only.  Neither contract grants authority or changes any runtime or cutover.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from src.history.capture_observation import (
    CAPTURE_OBSERVATION_SCHEMA_VERSION,
    parse_capture_observation,
)
from src.history.models import ReportImportError
from src.history.report_parser import canonical_json_hash, normalize_domain, parse_report
from src.services.evidence_memory_identity_v2 import (
    project_evidence_memory_row_identity,
)
from src.services.evidence_vault_canonical_core import (
    build_tile_contract_registry,
    canonical_fingerprint,
    canonical_json,
    tile_contract_registry_fingerprint,
)
from src.services.evidence_vault_exact_relation_supplement import (
    EVIDENCE_VAULT_EXACT_RELATION_SOURCE_RESOLUTION_VERSION,
    EVIDENCE_VAULT_EXACT_RELATION_SUPPLEMENT_VERSION,
    EvidenceVaultExactRelationSupplementError,
    validate_exact_relation_supplement_structure,
)
from src.services.scanner_evidence_comparison import (
    canonical_evidence_representatives,
)


HISTORICAL_REPORT_ACQUISITION_REPLAY_POLICY_VERSION = (
    "evidence-vault-historical-report-acquisition-replay-v1"
)
EVIDENCE_PACK_NORMALIZATION_POLICY_VERSION = (
    "evidence-vault-historical-evidence-pack-normalization-v1"
)
EVIDENCE_VAULT_LINEAGE_SEED_EXPORT_VERSION = (
    "evidence-vault-lineage-seed-export-v2"
)
LINEAGE_REPLAY_PROVENANCE = "report_derived_candidate_capture"
REPORT_DERIVED_ACQUISITION_CLASSIFICATION = "report_derived_candidate_capture"
RAW_HASH_VERIFICATION_CLASSIFICATION = (
    "declared_unverified_no_source_bytes"
)

_NORMALIZATION_REMOVED_METADATA_KEYS = frozenset(
    {
        "identity_match_llm",
        "relevant_blocks",
        "semantic_labeling_version",
        "specificity",
        "stance",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STABLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,199}$")
_WORKSPACE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# These limits are intentionally well above the historical field fixtures and
# low enough to prevent this pure validation seam from becoming an unbounded
# JSON retention or hashing primitive.
_MAX_SOURCE_REPORT_BYTES = 32 * 1024 * 1024
_MAX_CANDIDATE_BYTES = 16 * 1024 * 1024
_MAX_PACK_BYTES = 16 * 1024 * 1024
_MAX_RELATION_ARTIFACT_BYTES = 16 * 1024 * 1024
_MAX_EXPORT_BYTES = 96 * 1024 * 1024
_MAX_JSON_DEPTH = 48
_MAX_EVIDENCE_ROWS = 20_000
_MAX_ATTEMPT_ROWS = 2_000
_MAX_ARTIFACT_ROWS = 2_000

# Match credential-shaped *keys*, not vague substrings.  In particular, keys
# such as max_tokens and token_count are not credentials and remain valid.
_CREDENTIAL_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "auth_token",
        "authorization",
        "client_secret",
        "private_key",
        "secret_key",
        "password",
        "passwd",
        "session_cookie",
    }
)
_CREDENTIAL_SUFFIXES = (
    "_api_key",
    "_access_token",
    "_refresh_token",
    "_auth_token",
    "_client_secret",
    "_private_key",
    "_secret_key",
    "_password",
)


class EvidenceVaultLineageReplayError(ValueError):
    """Historical replay or lineage binding cannot be proven safely."""


def evidence_pack_normalization_policy() -> dict[str, Any]:
    """Return the frozen normalization policy used by field replay fixtures."""

    return {
        "schema_version": EVIDENCE_PACK_NORMALIZATION_POLICY_VERSION,
        "source_path": "raw.flow.candidate.evidence_pack",
        "removed_metadata_keys": sorted(_NORMALIZATION_REMOVED_METADATA_KEYS),
        "canonical_json": "utf8,sort_keys=true,separators=(comma,colon)",
        "semantic_effect": "metadata_removal_only",
    }


def derive_normalized_evidence_pack(
    raw_evidence_pack: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove only the frozen semantic-label metadata keys from a raw pack."""

    if not isinstance(raw_evidence_pack, Mapping):
        raise EvidenceVaultLineageReplayError(
            "raw evidence pack must be an object"
        )
    pack = deepcopy(dict(raw_evidence_pack))
    rows = pack.get("evidence")
    if not isinstance(rows, list) or not rows:
        raise EvidenceVaultLineageReplayError(
            "raw evidence pack must contain evidence rows"
        )
    if len(rows) > _MAX_EVIDENCE_ROWS:
        raise EvidenceVaultLineageReplayError("evidence row limit exceeded")
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise EvidenceVaultLineageReplayError(
                f"raw evidence pack evidence[{index}] must be an object"
            )
        metadata = raw.get("metadata")
        if metadata is None:
            continue
        if not isinstance(metadata, dict):
            raise EvidenceVaultLineageReplayError(
                f"raw evidence pack evidence[{index}].metadata must be an object"
            )
        for key in _NORMALIZATION_REMOVED_METADATA_KEYS:
            metadata.pop(key, None)
    _bounded_json(
        pack,
        field="normalized_evidence_pack",
        maximum_bytes=_MAX_PACK_BYTES,
        reject_credentials=True,
    )
    return pack


def build_historical_report_capture_observation(
    *,
    historical_report: dict[str, Any],
    source_raw_bytes_sha256: str,
    source_artifact_name: str,
) -> dict[str, Any]:
    """Derive one strict capture observation from an embedded report candidate.

    ``source_raw_bytes_sha256`` is retained as a declared raw-file digest.  The
    caller does not provide source bytes to this API, so the output says
    explicitly that the byte digest was not reverified here.  The full report
    is instead content-addressed canonically and is required again by the seed
    export validator.
    """

    if not isinstance(historical_report, dict):
        raise EvidenceVaultLineageReplayError(
            "historical report must be an exact JSON object"
        )
    raw_sha = _sha256(
        source_raw_bytes_sha256,
        field="source_raw_bytes_sha256",
    )
    artifact_name = _artifact_name(source_artifact_name)
    _bounded_json(
        historical_report,
        field="source_historical_report",
        maximum_bytes=_MAX_SOURCE_REPORT_BYTES,
        reject_credentials=True,
    )
    try:
        parsed = parse_report(historical_report)
    except ReportImportError as exc:
        raise EvidenceVaultLineageReplayError(
            f"historical report cannot be parsed: {exc}"
        ) from exc

    raw = historical_report.get("raw")
    flow = raw.get("flow") if isinstance(raw, dict) else None
    candidate = flow.get("candidate") if isinstance(flow, dict) else None
    if not isinstance(candidate, dict) or not candidate:
        raise EvidenceVaultLineageReplayError(
            "historical report has no embedded acquisition candidate"
        )
    pack = candidate.get("evidence_pack")
    rows = pack.get("evidence") if isinstance(pack, dict) else None
    if not isinstance(rows, list) or not rows:
        raise EvidenceVaultLineageReplayError(
            "historical report candidate has no embedded evidence"
        )
    if (
        len(rows) != len(parsed.evidence_records)
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise EvidenceVaultLineageReplayError(
            "historical report candidate evidence is malformed"
        )
    if len(rows) > _MAX_EVIDENCE_ROWS:
        raise EvidenceVaultLineageReplayError("evidence row limit exceeded")
    _bounded_json(
        candidate,
        field="historical_report_embedded_candidate",
        maximum_bytes=_MAX_CANDIDATE_BYTES,
        reject_credentials=True,
    )

    attempts = _exact_object_rows(
        historical_report.get("attempts"),
        field="attempts",
        maximum_count=_MAX_ATTEMPT_ROWS,
    )
    artifacts = _exact_object_rows(
        historical_report.get("acquisition_artifacts"),
        field="acquisition_artifacts",
        maximum_count=_MAX_ARTIFACT_ROWS,
    )
    _bounded_json(
        attempts,
        field="historical_report_attempts",
        maximum_bytes=_MAX_CANDIDATE_BYTES,
        reject_credentials=True,
    )
    _bounded_json(
        artifacts,
        field="historical_report_artifacts",
        maximum_bytes=_MAX_CANDIDATE_BYTES,
        reject_credentials=True,
    )
    if attempts != list(parsed.acquisition_attempts):
        raise EvidenceVaultLineageReplayError(
            "historical report acquisition attempts were not parsed exactly"
        )
    if artifacts != list(parsed.artifacts):
        raise EvidenceVaultLineageReplayError(
            "historical report acquisition artifacts were not parsed exactly"
        )

    report_sha = parsed.report_hash
    capture_sha = parsed.capture_hash
    source_scan_fingerprint = canonical_fingerprint(
        HISTORICAL_REPORT_ACQUISITION_REPLAY_POLICY_VERSION,
        {
            "provenance": LINEAGE_REPLAY_PROVENANCE,
            "source_artifact_name": artifact_name,
            "source_raw_bytes_sha256": raw_sha,
            "source_report_id": parsed.source_report_id,
            "source_report_canonical_sha256": report_sha,
            "source_capture_payload_canonical_sha256": capture_sha,
        },
    )
    limitations = _dedupe_text(
        [
            *parsed.limitations,
            "historical_report_embedded_candidate_only",
            LINEAGE_REPLAY_PROVENANCE,
            "not_an_original_raw_acquisition_envelope",
            "raw_file_sha256_is_declared_not_reverified_without_source_bytes",
            "authority_runtime_scanner_and_cutover_not_attested",
        ]
    )
    evidence_records = [deepcopy(row) for row in parsed.evidence_records]
    observation = {
        "schema_version": CAPTURE_OBSERVATION_SCHEMA_VERSION,
        "source_scan_id": f"lineage-replay:{source_scan_fingerprint}",
        "source_run_id": parsed.source_run_id,
        "brand_name": parsed.brand_name,
        "url": parsed.canonical_url,
        "observed_at": parsed.observed_at.isoformat(),
        "recorded_at": parsed.recorded_at.isoformat(),
        "pipeline_version": HISTORICAL_REPORT_ACQUISITION_REPLAY_POLICY_VERSION,
        "acquisition_state": parsed.acquisition_state,
        "acquisition_summary": {
            "evidence_record_count": len(evidence_records),
            "source_count": len(
                {
                    str(row.get("source") or "")
                    for row in evidence_records
                    if str(row.get("source") or "")
                }
            ),
            "acquisition_step_count": len(attempts),
            "artifact_count": len(artifacts),
        },
        "limitations": limitations,
        "capture_payload": deepcopy(candidate),
        "evidence_records": evidence_records,
        "acquisition_attempts": deepcopy(attempts),
        "artifacts": deepcopy(artifacts),
        "metadata": {
            "provenance": LINEAGE_REPLAY_PROVENANCE,
            "acquisition_classification": (
                REPORT_DERIVED_ACQUISITION_CLASSIFICATION
            ),
            "replay_policy_version": (
                HISTORICAL_REPORT_ACQUISITION_REPLAY_POLICY_VERSION
            ),
            "source_artifact_name": artifact_name,
            "source_raw_bytes_sha256": raw_sha,
            "source_raw_hash_verification": (
                RAW_HASH_VERIFICATION_CLASSIFICATION
            ),
            "source_report_id": parsed.source_report_id,
            "source_report_canonical_sha256": report_sha,
            "source_capture_payload_canonical_sha256": capture_sha,
            "original_capture_envelope": False,
            "authority": False,
            "runtime_effect": False,
            "production_runtime_effect": False,
            "scanner_runtime_effect": False,
            "cutover_authorized": False,
        },
    }
    try:
        validated = parse_capture_observation(observation)
    except ReportImportError as exc:
        raise EvidenceVaultLineageReplayError(
            f"derived capture observation is invalid: {exc}"
        ) from exc
    if validated.raw_observation != observation:
        raise EvidenceVaultLineageReplayError(
            "derived capture observation did not round-trip exactly"
        )
    return observation


def validate_historical_report_capture_observation(
    observation: Mapping[str, Any],
    *,
    historical_report: dict[str, Any],
    source_raw_bytes_sha256: str,
    source_artifact_name: str,
) -> None:
    """Rebuild a report-derived observation and reject every alteration."""

    if not isinstance(observation, Mapping):
        raise EvidenceVaultLineageReplayError(
            "capture observation must be an object"
        )
    rebuilt = build_historical_report_capture_observation(
        historical_report=historical_report,
        source_raw_bytes_sha256=source_raw_bytes_sha256,
        source_artifact_name=source_artifact_name,
    )
    if dict(observation) != rebuilt:
        raise EvidenceVaultLineageReplayError(
            "capture observation differs from its exact historical report"
        )


def build_lineage_seed_export_v2(
    *,
    workspace_slug: str,
    seed_id: str,
    lineage_export_identity: str,
    lineage_export_ordinal: int,
    expected_predecessor_event_fingerprint: str | None,
    replay_origin_sequence: int | None = None,
    source_historical_report: dict[str, Any],
    source_raw_bytes_sha256: str,
    source_artifact_name: str,
    capture_observation: Mapping[str, Any],
    normalized_evidence_pack: Mapping[str, Any],
    exact_relation_supplement: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one chainable, reviewed lineage seed/export v2 artifact.

    The export binds a complete exact-relation supplement to one exact
    report-derived capture. Later checkpoints increment
    ``lineage_export_ordinal``, name the predecessor event, and carry the first
    checkpoint's ``replay_origin_sequence``. Ordering is commit/event ordering,
    never observation time.
    """

    workspace = _workspace_slug(workspace_slug)
    seed = _stable_id(seed_id, field="seed_id")
    export_identity = _stable_id(
        lineage_export_identity,
        field="lineage_export_identity",
    )
    ordinal = _positive_int(
        lineage_export_ordinal,
        field="lineage_export_ordinal",
    )
    origin = (
        ordinal
        if replay_origin_sequence is None
        else _positive_int(
            replay_origin_sequence,
            field="replay_origin_sequence",
        )
    )
    if origin > ordinal:
        raise EvidenceVaultLineageReplayError(
            "replay_origin_sequence cannot exceed lineage_export_ordinal"
        )
    predecessor = _predecessor_fingerprint(
        expected_predecessor_event_fingerprint,
        ordinal=ordinal,
    )
    raw_sha = _sha256(
        source_raw_bytes_sha256,
        field="source_raw_bytes_sha256",
    )
    artifact_name = _artifact_name(source_artifact_name)

    if not isinstance(capture_observation, Mapping):
        raise EvidenceVaultLineageReplayError(
            "capture_observation must be an object"
        )
    capture = deepcopy(dict(capture_observation))
    validate_historical_report_capture_observation(
        capture,
        historical_report=source_historical_report,
        source_raw_bytes_sha256=raw_sha,
        source_artifact_name=artifact_name,
    )
    try:
        parsed_capture = parse_capture_observation(capture)
    except ReportImportError as exc:
        raise EvidenceVaultLineageReplayError(
            f"capture observation is invalid: {exc}"
        ) from exc

    raw_pack = parsed_capture.capture_payload.get("evidence_pack")
    if not isinstance(raw_pack, dict):
        raise EvidenceVaultLineageReplayError(
            "capture payload has no exact embedded evidence pack"
        )
    raw_pack_rows = raw_pack.get("evidence")
    if raw_pack_rows != list(parsed_capture.evidence_records):
        raise EvidenceVaultLineageReplayError(
            "capture evidence records differ from the embedded raw pack"
        )
    derived_normalized_pack = derive_normalized_evidence_pack(raw_pack)
    if not isinstance(normalized_evidence_pack, Mapping):
        raise EvidenceVaultLineageReplayError(
            "normalized_evidence_pack must be an object"
        )
    declared_normalized_pack = deepcopy(dict(normalized_evidence_pack))
    _bounded_json(
        declared_normalized_pack,
        field="normalized_evidence_pack",
        maximum_bytes=_MAX_PACK_BYTES,
        reject_credentials=True,
    )
    if declared_normalized_pack != derived_normalized_pack:
        raise EvidenceVaultLineageReplayError(
            "declared normalized evidence pack differs from policy rederivation"
        )

    raw_pack_sha = _canonical_sha(raw_pack)
    normalized_pack_sha = _canonical_sha(declared_normalized_pack)
    source = _validated_exact_relation_source(
        exact_relation_supplement,
        maximum_bytes=_MAX_RELATION_ARTIFACT_BYTES,
    )
    if source.get("source_evidence_pack_canonical_sha256") != normalized_pack_sha:
        raise EvidenceVaultLineageReplayError(
            "exact relation supplement evidence pack hash mismatch"
        )
    if source.get("brand_identity") != parsed_capture.canonical_domain:
        raise EvidenceVaultLineageReplayError(
            "exact relation supplement brand differs from capture"
        )
    if source.get("subject_url") != parsed_capture.canonical_url:
        raise EvidenceVaultLineageReplayError(
            "exact relation supplement subject URL differs from capture"
        )

    bindings = _validate_and_bind_groups(
        source,
        normalized_pack=declared_normalized_pack,
        raw_capture_rows=list(parsed_capture.evidence_records),
        brand_identity=parsed_capture.canonical_domain,
        subject_url=parsed_capture.canonical_url,
    )
    source_report_sha = canonical_json_hash(source_historical_report)
    metadata = parsed_capture.metadata
    if (
        metadata.get("source_raw_bytes_sha256") != raw_sha
        or metadata.get("source_report_canonical_sha256") != source_report_sha
        or metadata.get("source_artifact_name") != artifact_name
    ):
        raise EvidenceVaultLineageReplayError(
            "capture source hash metadata differs from the exact source report"
        )

    payload = {
        "schema_version": EVIDENCE_VAULT_LINEAGE_SEED_EXPORT_VERSION,
        "workspace_slug": workspace,
        "seed_id": seed,
        "lineage_export_identity": export_identity,
        "lineage_export_ordinal": ordinal,
        "capture_sequence": ordinal,
        "replay_origin_sequence": origin,
        "expected_predecessor_event_fingerprint": predecessor,
        "lineage_kind": LINEAGE_REPLAY_PROVENANCE,
        "acquisition_classification": (
            REPORT_DERIVED_ACQUISITION_CLASSIFICATION
        ),
        "provenance_policy_versions": {
            "historical_report_replay": (
                HISTORICAL_REPORT_ACQUISITION_REPLAY_POLICY_VERSION
            ),
            "evidence_pack_normalization": (
                EVIDENCE_PACK_NORMALIZATION_POLICY_VERSION
            ),
            "exact_relation_source": (
                EVIDENCE_VAULT_EXACT_RELATION_SUPPLEMENT_VERSION
            ),
            "exact_relation_resolution": (
                EVIDENCE_VAULT_EXACT_RELATION_SOURCE_RESOLUTION_VERSION
            ),
            "lineage_binding": (
                EVIDENCE_VAULT_LINEAGE_SEED_EXPORT_VERSION
            ),
        },
        "brand_identity": parsed_capture.canonical_domain,
        "subject_url": parsed_capture.canonical_url,
        "source_artifact_name": artifact_name,
        "source_raw_bytes_sha256": raw_sha,
        "source_raw_hash_verification": RAW_HASH_VERIFICATION_CLASSIFICATION,
        "source_report_canonical_sha256": source_report_sha,
        "source_relation_artifact_fingerprint": source[
            "artifact_fingerprint"
        ],
        "capture_observation_hash": parsed_capture.observation_hash,
        "capture_hash": parsed_capture.capture_hash,
        "raw_evidence_pack_canonical_sha256": raw_pack_sha,
        "normalized_evidence_pack_canonical_sha256": normalized_pack_sha,
        "normalization_policy": evidence_pack_normalization_policy(),
        "source_historical_report": deepcopy(source_historical_report),
        "source_normalized_evidence_pack": declared_normalized_pack,
        "source_exact_relation_supplement": source,
        "capture_observation": capture,
        "evidence_bindings": bindings,
        "group_count": len(source["groups"]),
        "relation_count": len(bindings),
        "adoption_eligible": False,
        "authority": False,
        "runtime_effect": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "cutover_authorized": False,
    }
    lineage_export_fingerprint = canonical_fingerprint(
        f"{EVIDENCE_VAULT_LINEAGE_SEED_EXPORT_VERSION}-manifest",
        payload,
    )
    unsigned = {
        **payload,
        "lineage_export_fingerprint": lineage_export_fingerprint,
    }
    artifact = {
        **unsigned,
        "artifact_fingerprint": canonical_fingerprint(
            EVIDENCE_VAULT_LINEAGE_SEED_EXPORT_VERSION,
            unsigned,
        ),
    }
    _bounded_json(
        artifact,
        field="lineage_seed_export_v2",
        maximum_bytes=_MAX_EXPORT_BYTES,
        reject_credentials=True,
    )
    return artifact


def validate_lineage_seed_export_v2(artifact: Mapping[str, Any]) -> None:
    """Rebuild a self-contained seed/export and reject unknown or altered data."""

    fields = {
        "schema_version",
        "workspace_slug",
        "seed_id",
        "lineage_export_identity",
        "lineage_export_ordinal",
        "capture_sequence",
        "replay_origin_sequence",
        "expected_predecessor_event_fingerprint",
        "lineage_kind",
        "acquisition_classification",
        "provenance_policy_versions",
        "brand_identity",
        "subject_url",
        "source_artifact_name",
        "source_raw_bytes_sha256",
        "source_raw_hash_verification",
        "source_report_canonical_sha256",
        "source_relation_artifact_fingerprint",
        "capture_observation_hash",
        "capture_hash",
        "raw_evidence_pack_canonical_sha256",
        "normalized_evidence_pack_canonical_sha256",
        "normalization_policy",
        "source_historical_report",
        "source_normalized_evidence_pack",
        "source_exact_relation_supplement",
        "capture_observation",
        "evidence_bindings",
        "group_count",
        "relation_count",
        "adoption_eligible",
        "authority",
        "runtime_effect",
        "production_runtime_effect",
        "scanner_runtime_effect",
        "cutover_authorized",
        "lineage_export_fingerprint",
        "artifact_fingerprint",
    }
    if not isinstance(artifact, Mapping) or set(artifact) != fields:
        raise EvidenceVaultLineageReplayError(
            "lineage seed/export fields are invalid"
        )
    if artifact.get("schema_version") != (
        EVIDENCE_VAULT_LINEAGE_SEED_EXPORT_VERSION
    ):
        raise EvidenceVaultLineageReplayError(
            "lineage seed/export schema is unsupported"
        )
    if any(
        artifact.get(field) is not False
        for field in (
            "adoption_eligible",
            "authority",
            "runtime_effect",
            "production_runtime_effect",
            "scanner_runtime_effect",
            "cutover_authorized",
        )
    ):
        raise EvidenceVaultLineageReplayError(
            "lineage seed/export cannot claim authority or runtime effect"
        )
    source_report = artifact.get("source_historical_report")
    source_pack = artifact.get("source_normalized_evidence_pack")
    source_relation = artifact.get("source_exact_relation_supplement")
    capture = artifact.get("capture_observation")
    if not isinstance(source_report, dict):
        raise EvidenceVaultLineageReplayError(
            "source_historical_report must be an object"
        )
    if not all(
        isinstance(value, Mapping)
        for value in (source_pack, source_relation, capture)
    ):
        raise EvidenceVaultLineageReplayError(
            "lineage seed/export nested sources must be objects"
        )
    rebuilt = build_lineage_seed_export_v2(
        workspace_slug=artifact.get("workspace_slug"),
        seed_id=artifact.get("seed_id"),
        lineage_export_identity=artifact.get("lineage_export_identity"),
        lineage_export_ordinal=artifact.get("lineage_export_ordinal"),
        expected_predecessor_event_fingerprint=artifact.get(
            "expected_predecessor_event_fingerprint"
        ),
        replay_origin_sequence=artifact.get("replay_origin_sequence"),
        source_historical_report=source_report,
        source_raw_bytes_sha256=artifact.get("source_raw_bytes_sha256"),
        source_artifact_name=artifact.get("source_artifact_name"),
        capture_observation=capture,
        normalized_evidence_pack=source_pack,
        exact_relation_supplement=source_relation,
    )
    if rebuilt != dict(artifact):
        raise EvidenceVaultLineageReplayError(
            "lineage seed/export differs from its exact embedded sources"
        )


def _validated_exact_relation_source(
    artifact: Mapping[str, Any],
    *,
    maximum_bytes: int,
) -> dict[str, Any]:
    if not isinstance(artifact, Mapping):
        raise EvidenceVaultLineageReplayError(
            "exact_relation_supplement must be an object"
        )
    source = deepcopy(dict(artifact))
    _bounded_json(
        source,
        field="exact_relation_supplement",
        maximum_bytes=maximum_bytes,
        reject_credentials=True,
    )
    try:
        validate_exact_relation_supplement_structure(source)
    except EvidenceVaultExactRelationSupplementError as exc:
        raise EvidenceVaultLineageReplayError(
            f"exact relation supplement is invalid: {exc}"
        ) from exc
    return source


def _validate_and_bind_groups(
    source: dict[str, Any],
    *,
    normalized_pack: dict[str, Any],
    raw_capture_rows: list[dict[str, Any]],
    brand_identity: str,
    subject_url: str,
) -> list[dict[str, Any]]:
    normalized_rows = normalized_pack.get("evidence")
    if not isinstance(normalized_rows, list) or not normalized_rows:
        raise EvidenceVaultLineageReplayError(
            "normalized pack has no evidence rows"
        )
    representatives = canonical_evidence_representatives(
        normalized_rows,
        subject_url=subject_url,
    )
    if not representatives:
        raise EvidenceVaultLineageReplayError(
            "normalized pack has no canonical evidence"
        )
    raw_indices_by_ref: dict[str, list[int]] = {}
    for index, row in enumerate(raw_capture_rows):
        if not isinstance(row, dict):
            raise EvidenceVaultLineageReplayError(
                "raw capture evidence contains a non-object row"
            )
        ref = str(row.get("ref") or "")
        raw_indices_by_ref.setdefault(ref, []).append(index)
    registry = {
        str(row["tile_id"]): row
        for row in build_tile_contract_registry()["tiles"]
    }
    groups = source.get("groups")
    if not isinstance(groups, list) or not groups:
        raise EvidenceVaultLineageReplayError(
            "exact relation supplement has no groups"
        )
    if groups != sorted(groups, key=lambda row: str(row.get("tile_id") or "")):
        raise EvidenceVaultLineageReplayError(
            "exact relation supplement groups are not canonically ordered"
        )

    bindings: list[dict[str, Any]] = []
    seen_group_ids: set[str] = set()
    seen_relation_ids: set[str] = set()
    for group in groups:
        tile_id = str(group.get("tile_id") or "")
        contract = registry.get(tile_id)
        if contract is None:
            raise EvidenceVaultLineageReplayError(
                f"exact relation group has unknown tile: {tile_id}"
            )
        if (
            group.get("tile_key") != contract["tile_key"]
            or group.get("tile_condition") != contract["condition"]
            or group.get("evidence_contract_ok")
            != contract["evidence_contract"]["ok"]
            or group.get("evidence_contract_reject")
            != contract["evidence_contract"]["reject"]
        ):
            raise EvidenceVaultLineageReplayError(
                f"exact relation group {tile_id} changed its tile contract"
            )
        decision_rule = str(group.get("decision_rule") or "")
        relations = group.get("relations")
        if not isinstance(relations, list):
            raise EvidenceVaultLineageReplayError(
                f"exact relation group {tile_id} has invalid relations"
            )
        members: list[dict[str, Any]] = []
        pending_bindings: list[dict[str, Any]] = []
        for relation in relations:
            fingerprint = _sha256(
                relation.get("evidence_fingerprint"),
                field="evidence_fingerprint",
            )
            representative = representatives.get(fingerprint)
            if representative is None:
                raise EvidenceVaultLineageReplayError(
                    f"relation {relation.get('relation_id')} evidence is absent"
                )
            identity = project_evidence_memory_row_identity(
                {
                    **representative,
                    "evidence_fingerprint": fingerprint,
                },
                brand_domain=brand_identity,
            )
            quote = relation.get("literal_quote")
            expected_channel = _channel_role(
                representative,
                subject_url=subject_url,
            )
            if (
                identity is None
                or relation.get("evidence_id") != identity["evidence_id"]
                or relation.get("source_identity_id")
                != identity["document_id"]
                or relation.get("ref") != representative.get("ref")
                or relation.get("url")
                != (representative.get("url") or "")
                or relation.get("source_class")
                != (
                    representative.get("metadata", {}).get("source_class")
                    or ""
                )
                or relation.get("channel_role") != expected_channel
                or not isinstance(quote, str)
                or not quote
                or quote not in str(representative.get("content") or "")
            ):
                raise EvidenceVaultLineageReplayError(
                    f"relation {relation.get('relation_id')} evidence identity changed"
                )
            matching_indices = raw_indices_by_ref.get(str(relation.get("ref")), [])
            matching_indices = [
                index
                for index in matching_indices
                if _row_fingerprint(
                    raw_capture_rows[index],
                    subject_url=subject_url,
                )
                == fingerprint
            ]
            if len(matching_indices) != 1:
                raise EvidenceVaultLineageReplayError(
                    f"relation {relation.get('relation_id')} evidence is absent or ambiguous"
                )
            raw_index = matching_indices[0]
            raw_row = raw_capture_rows[raw_index]
            if quote not in str(raw_row.get("content") or ""):
                raise EvidenceVaultLineageReplayError(
                    f"relation {relation.get('relation_id')} quote is not literal"
                )
            member = {
                "evidence_fingerprint": fingerprint,
                "evidence_id": identity["evidence_id"],
                "source_identity_id": identity["document_id"],
                "ref": str(representative["ref"]),
                "url": str(representative.get("url") or ""),
                "source_class": str(
                    representative.get("metadata", {}).get("source_class")
                    or ""
                ),
                "channel_role": expected_channel,
                "literal_quote": quote,
            }
            members.append(member)
            pending_bindings.append(
                {
                    "group_id": group.get("group_id"),
                    "relation_id": relation.get("relation_id"),
                    "tile_id": tile_id,
                    "decision_rule": decision_rule,
                    "evidence_fingerprint": fingerprint,
                    "evidence_id": identity["evidence_id"],
                    "source_identity_id": identity["document_id"],
                    "source_ref": member["ref"],
                    "source_url": member["url"],
                    "source_class": member["source_class"],
                    "channel_role": expected_channel,
                    "evidence_quote": quote,
                    "capture_evidence_index": raw_index,
                }
            )
        canonical_members = sorted(
            members,
            key=lambda row: (
                row["source_identity_id"],
                row["evidence_fingerprint"],
            ),
        )
        if members != canonical_members:
            raise EvidenceVaultLineageReplayError(
                f"exact relation group {tile_id} members are not canonically ordered"
            )
        expected_group_id = canonical_fingerprint(
            (
                "evidence-vault-exact-relation-composite-claim-v1"
                if decision_rule == "all_of"
                else "evidence-vault-exact-relation-atomic-group-v1"
            ),
            {
                "request_fingerprint": source["request_fingerprint"],
                "tile_contract_registry_fingerprint": (
                    tile_contract_registry_fingerprint()
                ),
                "assessment_candidate_id": group[
                    "assessment_candidate_id"
                ],
                "tile_id": tile_id,
                "decision_rule": decision_rule,
                "members": canonical_members,
            },
        )
        group_id = _sha256(group.get("group_id"), field="group_id")
        if group_id != expected_group_id or group_id in seen_group_ids:
            raise EvidenceVaultLineageReplayError(
                f"exact relation group {tile_id} identity was forged or repeated"
            )
        seen_group_ids.add(group_id)

        source_ids = {row["source_identity_id"] for row in canonical_members}
        channel_roles = {row["channel_role"] for row in canonical_members}
        expected_group_contract = {
            "decision_rule": decision_rule,
            "minimum_member_count": len(canonical_members),
            "required_distinct_source_identity_count": len(
                canonical_members
            ),
            "required_channel_roles": (
                sorted(channel_roles) if decision_rule == "all_of" else []
            ),
            "member_decisions_must_match": decision_rule == "all_of",
            "member_change_requires_review": True,
        }
        if group.get("group_contract") != expected_group_contract:
            raise EvidenceVaultLineageReplayError(
                f"exact relation group {tile_id} contract changed"
            )
        if decision_rule == "all_of" and len(source_ids) != len(canonical_members):
            raise EvidenceVaultLineageReplayError(
                f"all_of group {tile_id} lacks distinct source identities"
            )
        if tile_id == "C7" and (
            decision_rule != "all_of"
            or len(canonical_members) != 2
            or channel_roles
            != {"owned_web", "external_social_profile"}
        ):
            raise EvidenceVaultLineageReplayError(
                "C7 requires exact all_of owned-web/external-social semantics"
            )

        for relation, member, binding in zip(
            relations,
            canonical_members,
            pending_bindings,
            strict=True,
        ):
            expected_claim_id = group_id if decision_rule == "all_of" else None
            expected_relation_id = canonical_fingerprint(
                "evidence-vault-exact-relation-v1",
                {
                    "request_fingerprint": source["request_fingerprint"],
                    "assessment_candidate_id": group[
                        "assessment_candidate_id"
                    ],
                    "group_id": group_id,
                    "tile_id": tile_id,
                    "evidence_id": member["evidence_id"],
                    "source_identity_id": member["source_identity_id"],
                    "claim_id": expected_claim_id,
                    "literal_quote": member["literal_quote"],
                    "polarity": "supports",
                },
            )
            relation_id = _sha256(
                relation.get("relation_id"),
                field="relation_id",
            )
            if (
                relation_id != expected_relation_id
                or relation_id in seen_relation_ids
                or relation.get("claim_id") != expected_claim_id
                or relation.get("polarity") != "supports"
                or relation.get("review_status") != "unreviewed"
                or relation.get("decision_event_id") is not None
            ):
                raise EvidenceVaultLineageReplayError(
                    f"exact relation {relation_id} identity or state was forged"
                )
            seen_relation_ids.add(relation_id)
            if binding["group_id"] != group_id or binding["relation_id"] != relation_id:
                raise EvidenceVaultLineageReplayError(
                    "relation binding identity changed during validation"
                )
            bindings.append(binding)
    if len(bindings) != source.get("relation_count"):
        raise EvidenceVaultLineageReplayError(
            "exact relation member count mismatch"
        )
    return bindings


def _row_fingerprint(row: dict[str, Any], *, subject_url: str) -> str | None:
    representatives = canonical_evidence_representatives(
        [row],
        subject_url=subject_url,
    )
    if len(representatives) != 1:
        return None
    return next(iter(representatives))


def _channel_role(row: Mapping[str, Any], *, subject_url: str) -> str:
    url = str(row.get("url") or "")
    subject_host = (urlparse(subject_url).hostname or "").lower()
    host = (urlparse(url).hostname or "").lower()
    metadata = row.get("metadata")
    source_class = str(
        metadata.get("source_class")
        if isinstance(metadata, Mapping)
        else ""
    )
    if source_class == "owned_copy" and host in {
        subject_host,
        f"www.{subject_host}",
    }:
        return "owned_web"
    if (
        source_class == "external_proof"
        and host in {"linkedin.com", "www.linkedin.com"}
        and urlparse(url).path.startswith("/company/")
    ):
        return "external_social_profile"
    return "other"


def _bounded_json(
    value: Any,
    *,
    field: str,
    maximum_bytes: int,
    reject_credentials: bool,
) -> None:
    try:
        rendered = canonical_json(value).encode("utf-8")
    except Exception as exc:
        raise EvidenceVaultLineageReplayError(
            f"{field} must be canonical JSON"
        ) from exc
    if len(rendered) > maximum_bytes:
        raise EvidenceVaultLineageReplayError(
            f"{field} exceeds its byte limit"
        )
    stack: list[tuple[Any, int, str]] = [(value, 1, field)]
    while stack:
        current, depth, path = stack.pop()
        if depth > _MAX_JSON_DEPTH:
            raise EvidenceVaultLineageReplayError(
                f"{field} exceeds its depth limit"
            )
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise EvidenceVaultLineageReplayError(
                        f"{path} contains a non-string key"
                    )
                if reject_credentials and _credential_shaped_key(key):
                    raise EvidenceVaultLineageReplayError(
                        f"{path} contains credential-shaped key {key!r}"
                    )
                stack.append((nested, depth + 1, f"{path}.{key}"))
        elif isinstance(current, (list, tuple)):
            for index, nested in enumerate(current):
                stack.append((nested, depth + 1, f"{path}[{index}]"))


def _credential_shaped_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
    return normalized in _CREDENTIAL_KEYS or normalized.endswith(
        _CREDENTIAL_SUFFIXES
    )


def _canonical_sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _exact_object_rows(
    value: Any,
    *,
    field: str,
    maximum_count: int,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise EvidenceVaultLineageReplayError(f"{field} must be an array")
    if len(value) > maximum_count:
        raise EvidenceVaultLineageReplayError(f"{field} row limit exceeded")
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise EvidenceVaultLineageReplayError(
                f"{field}[{index}] must be an object"
            )
        rows.append(deepcopy(row))
    return rows


def _dedupe_text(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise EvidenceVaultLineageReplayError(
            f"{field} must be a lowercase SHA-256 digest"
        )
    return value


def _artifact_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > 1024
        or "\x00" in value
        or value in {".", ".."}
    ):
        raise EvidenceVaultLineageReplayError(
            "source_artifact_name is invalid"
        )
    return value


def _workspace_slug(value: Any) -> str:
    if not isinstance(value, str) or not _WORKSPACE_SLUG_RE.fullmatch(value):
        raise EvidenceVaultLineageReplayError("workspace_slug is invalid")
    return value


def _stable_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _STABLE_ID_RE.fullmatch(value):
        raise EvidenceVaultLineageReplayError(f"{field} is invalid")
    return value


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EvidenceVaultLineageReplayError(
            f"{field} must be a positive integer"
        )
    return value


def _predecessor_fingerprint(value: Any, *, ordinal: int) -> str | None:
    if ordinal == 1:
        if value is not None:
            raise EvidenceVaultLineageReplayError(
                "ordinal 1 cannot declare a predecessor event"
            )
        return None
    if value is None:
        raise EvidenceVaultLineageReplayError(
            "later lineage exports require a predecessor event fingerprint"
        )
    return _sha256(value, field="expected_predecessor_event_fingerprint")


__all__ = [
    "EVIDENCE_PACK_NORMALIZATION_POLICY_VERSION",
    "EVIDENCE_VAULT_LINEAGE_SEED_EXPORT_VERSION",
    "EvidenceVaultLineageReplayError",
    "HISTORICAL_REPORT_ACQUISITION_REPLAY_POLICY_VERSION",
    "LINEAGE_REPLAY_PROVENANCE",
    "REPORT_DERIVED_ACQUISITION_CLASSIFICATION",
    "build_historical_report_capture_observation",
    "build_lineage_seed_export_v2",
    "derive_normalized_evidence_pack",
    "evidence_pack_normalization_policy",
    "validate_historical_report_capture_observation",
    "validate_lineage_seed_export_v2",
]
