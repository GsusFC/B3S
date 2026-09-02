"""Read-only projection of accepted Vault evidence relations into VA1."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import UUID

from src.services.evidence_vault_canonical_core import (
    build_tile_contract_registry,
    canonical_fingerprint,
)
from src.services.evidence_vault_sv9_judgment_delta import (
    build_evidence_identity_set,
    build_authoritative_evidence_tile_relation,
)

# fmt: off

_VERSION = "evidence-vault-sv9-authoritative-relation-projection-v1"
EVIDENCE_VAULT_SV9_EVALUATION_INPUT_VERSION = "evidence-vault-sv9-evaluation-input-v1"
_EVALUATION_INPUT_VERSION = EVIDENCE_VAULT_SV9_EVALUATION_INPUT_VERSION
_WITNESS_VERSION = "evidence-vault-sv9-authoritative-relation-witness-v1"
_POLARITIES = frozenset({"supports", "contradicts", "demonstrates_absence"})
_ASSESSMENT_STATES = frozenset({"ok", "no", "sin_evidencia"})
_SOURCE_IDENTITY_FIELDS = (
    "workspace_id",
    "brand_id",
    "scan_run_id",
    "source_scan_id",
    "workspace_slug",
    "canonical_domain",
    "capture_id",
    "capture_fingerprint",
    "operation_plan_id",
    "operation_fingerprint",
    "operation_status",
)
_EVALUATION_INPUT_FIELDS = frozenset(
    "status reason_codes schema_version source_identity current_evidence "
    "authoritative_relations operational_witness relation_projection_fingerprint "
    "projection_version evaluation_input_fingerprint".split()
)
_WITNESS_FIELDS = frozenset("schema_version source_scan_id operational_witness authoritative_relations projection_fingerprint witness_fingerprint".split())
_OPERATIONAL_WITNESS_FIELDS = frozenset("canonical_memory_version adoption_event_id adoption_sequence candidate_packet_fingerprint request_fingerprint".split())


class EvidenceVaultSv9AuthoritativeRelationWitnessError(ValueError): pass
class EvidenceVaultSv9AuthoritativeRelationStaleWitnessError(EvidenceVaultSv9AuthoritativeRelationWitnessError): pass


def project_evidence_vault_sv9_evaluation_input(
    *, repository: Any, source_scan_id: str, workspace_slug: str = "b3s"
) -> dict[str, Any]:
    """Build one authority-bound, deterministic SV9 evaluation-input bundle."""

    try:
        facts = repository.load_evidence_vault_sv9_authoritative_relation_facts(
            source_scan_id, workspace_slug=workspace_slug
        )
    except Exception:
        return _evaluation_review("source_unavailable")
    if facts is None:
        return _evaluation_review("source_unavailable")

    try:
        source_identity = _source_identity_from_facts(
            facts, source_scan_id=source_scan_id, workspace_slug=workspace_slug
        )
    except ValueError as exc:
        return _evaluation_review(
            _known_reason(exc, "invalid_source_identity", "source_identity_mismatch", "operation_not_immutable")
        )
    except Exception:
        return _evaluation_review("invalid_source_identity")

    try:
        current_evidence = _capture_current_from_facts(
            facts, source_scan_id=source_scan_id, workspace_slug=workspace_slug
        )
    except ValueError as exc:
        return _evaluation_review(
            _known_reason(exc, "invalid_capture_current", "source_identity_mismatch", "operation_not_immutable")
        )
    except Exception:
        return _evaluation_review("invalid_capture_current")

    try:
        projection = _authoritative_relation_projection_from_facts(
            facts, source_scan_id=source_scan_id, workspace_slug=workspace_slug
        )
    except ValueError as exc:
        return _evaluation_review(
            _known_reason(
                exc,
                "invalid_authoritative_facts",
                "source_identity_mismatch",
                "operation_not_immutable",
                "no_operational_authority",
            )
        )
    except Exception:
        return _evaluation_review("invalid_authoritative_facts")
    if projection.get("status") != "available":
        reasons = projection.get("reason_codes")
        return _evaluation_review(
            reasons[0] if isinstance(reasons, list) and reasons and isinstance(reasons[0], str) else "invalid_authoritative_facts"
        )

    try:
        relations = projection["authoritative_relations"]
        witness = projection["operational_witness"]
        relation_fingerprint = _sha(projection["projection_fingerprint"])
        if not isinstance(relations, list) or not isinstance(witness, dict):
            raise ValueError("invalid_authoritative_facts")
        projection_version = _text(_VERSION)
        payload = {
            "source_identity": source_identity,
            "current_evidence": current_evidence,
            "authoritative_relations": relations,
            "operational_witness": witness,
            "relation_projection_fingerprint": relation_fingerprint,
            "projection_version": projection_version,
        }
        result = {
            "status": "available",
            "reason_codes": [],
            "schema_version": _EVALUATION_INPUT_VERSION,
            **payload,
        }
        result["evaluation_input_fingerprint"] = canonical_fingerprint(
            _EVALUATION_INPUT_VERSION, payload
        )
        return result
    except Exception:
        return _evaluation_review("invalid_evaluation_input")


def validate_evidence_vault_sv9_evaluation_input(
    value: Any, *, source_scan_id: str, workspace_slug: str = "b3s"
) -> dict[str, Any]:
    """Validate a projected evaluation input without consulting mutable state.

    The evaluator receives this value across a trust boundary.  Validation is
    deliberately replay-based: every nested signed projection is rebuilt and
    the two envelope fingerprints are recomputed before any Flow call.
    """

    try:
        if type(value) is not dict or set(value) != _EVALUATION_INPUT_FIELDS:
            raise ValueError("evaluation input fields")
        if (
            value["status"] != "available"
            or value["reason_codes"] != []
            or value["schema_version"] != _EVALUATION_INPUT_VERSION
            or value["projection_version"] != _VERSION
        ):
            raise ValueError("evaluation input envelope")
        source = _validate_evaluation_source_identity(
            value["source_identity"],
            source_scan_id=source_scan_id,
            workspace_slug=workspace_slug,
        )
        current = build_evidence_identity_set(value["current_evidence"])
        if value["current_evidence"] != current["evidence"]:
            raise ValueError("evaluation input evidence is not canonical")
        relations = _validate_evaluation_relations(
            value["authoritative_relations"],
            source=source,
            current=current["evidence"],
        )
        witness = _validate_evaluation_operational_witness(
            value["operational_witness"]
        )
        expected_projection_fingerprint = canonical_fingerprint(
            _VERSION,
            {
                "source_scan_id": source["source_scan_id"],
                "capture_origin": {
                    "capture_id": source["capture_id"],
                    "capture_fingerprint": source["capture_fingerprint"],
                },
                "operation_origin": {
                    "operation_id": source["operation_plan_id"],
                    "operation_fingerprint": source["operation_fingerprint"],
                },
                "operational_witness": witness,
                "authoritative_relations": relations,
            },
        )
        if value["relation_projection_fingerprint"] != expected_projection_fingerprint:
            raise ValueError("evaluation input projection fingerprint")
        payload = {
            "source_identity": source,
            "current_evidence": current["evidence"],
            "authoritative_relations": relations,
            "operational_witness": witness,
            "relation_projection_fingerprint": expected_projection_fingerprint,
            "projection_version": _VERSION,
        }
        expected = {
            "status": "available",
            "reason_codes": [],
            "schema_version": _EVALUATION_INPUT_VERSION,
            **payload,
            "evaluation_input_fingerprint": canonical_fingerprint(
                _EVALUATION_INPUT_VERSION, payload
            ),
        }
        if value != expected:
            raise ValueError("evaluation input replay mismatch")
        return expected
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("evaluation input is invalid") from exc


def _validate_evaluation_source_identity(
    value: Any, *, source_scan_id: str, workspace_slug: str
) -> dict[str, str]:
    if type(value) is not dict or set(value) != set(_SOURCE_IDENTITY_FIELDS):
        raise ValueError("invalid_source_identity")
    source = {
        name: _uuid(value[name])
        if name in {"workspace_id", "brand_id", "scan_run_id", "capture_id", "operation_plan_id"}
        else _sha(value[name])
        if name in {"capture_fingerprint", "operation_fingerprint"}
        else _text(value[name])
        for name in _SOURCE_IDENTITY_FIELDS
    }
    if (
        source["source_scan_id"] != _text(source_scan_id)
        or source["workspace_slug"] != _text(workspace_slug)
        or source["operation_status"] not in {"completed", "not_required"}
    ):
        raise ValueError("invalid_source_identity")
    if value != source:
        raise ValueError("invalid_source_identity")
    return source


def _validate_evaluation_operational_witness(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _OPERATIONAL_WITNESS_FIELDS:
        raise ValueError("invalid_authoritative_facts")
    witness = {
        "canonical_memory_version": _sha(value["canonical_memory_version"]),
        "adoption_event_id": _uuid(value["adoption_event_id"]),
        "adoption_sequence": value["adoption_sequence"],
        "candidate_packet_fingerprint": _sha(value["candidate_packet_fingerprint"]),
        "request_fingerprint": _sha(value["request_fingerprint"]),
    }
    if type(witness["adoption_sequence"]) is not int or witness["adoption_sequence"] < 1:
        raise ValueError("invalid_authoritative_facts")
    if value != witness:
        raise ValueError("invalid_authoritative_facts")
    return witness


def _validate_evaluation_relations(
    value: Any, *, source: Mapping[str, Any], current: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    if type(value) is not list:
        raise ValueError("invalid_authoritative_facts")
    rows = [build_authoritative_evidence_tile_relation(_raw=row, _signed=True) for row in value]
    capture = {
        "capture_id": source["capture_id"],
        "capture_fingerprint": source["capture_fingerprint"],
    }
    operation = {
        "operation_id": source["operation_plan_id"],
        "operation_fingerprint": source["operation_fingerprint"],
    }
    current_pairs = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in current}
    registry = {str(row["tile_id"]): index for index, row in enumerate(build_tile_contract_registry()["tiles"])}
    keys = []
    seen = set()
    for row in rows:
        pair = row["evidence_ref"], row["evidence_fingerprint"]
        key = row["tile_id"], *pair
        if (
            pair not in current_pairs
            or key in seen
            or row["capture_origin"] != capture
            or row["operation_origin"] != operation
        ):
            raise ValueError("invalid_authoritative_facts")
        seen.add(key)
        keys.append((registry[row["tile_id"]], *pair))
    if keys != sorted(keys) or len(keys) != len(set(keys)) or value != rows:
        raise ValueError("invalid_authoritative_facts")
    return rows


def project_evidence_vault_sv9_authoritative_relations(
    *, repository: Any, source_scan_id: str, workspace_slug: str = "b3s"
) -> dict[str, Any]:
    """Return VA1 relations only when every authoritative basis row is proven."""

    try:
        facts = repository.load_evidence_vault_sv9_authoritative_relation_facts(
            source_scan_id, workspace_slug=workspace_slug
        )
        return _authoritative_relation_projection_from_facts(
            facts, source_scan_id=source_scan_id, workspace_slug=workspace_slug
        )
    except ValueError as exc:
        return _review(
            _known_reason(
                exc,
                "invalid_authoritative_facts",
                "source_unavailable",
                "source_identity_mismatch",
                "operation_not_immutable",
                "no_operational_authority",
            )
        )
    except Exception:
        return _review("invalid_authoritative_facts")


def _review(reason: str) -> dict[str, Any]:
    return {"status": "review_required", "reason_codes": [reason], "authoritative_relations": [], "operational_witness": None, "projection_fingerprint": None}


def project_evidence_vault_sv9_capture_current(
    *, repository: Any, source_scan_id: str, workspace_slug: str = "b3s"
) -> dict[str, Any]:
    """Project the complete persisted capture evidence identity set."""

    try:
        facts = repository.load_evidence_vault_sv9_authoritative_relation_facts(
            source_scan_id, workspace_slug=workspace_slug
        )
        current = _capture_current_from_facts(
            facts, source_scan_id=source_scan_id, workspace_slug=workspace_slug
        )
        return {"status": "available", "reason_codes": [], "current_evidence": current}
    except ValueError as exc:
        return _capture_review(
            _known_reason(
                exc,
                "invalid_capture_current",
                "source_unavailable",
                "operation_not_immutable",
                "source_identity_mismatch",
            )
        )
    except Exception:
        return _capture_review("invalid_capture_current")


def _capture_review(reason: str) -> dict[str, Any]:
    return {"status": "review_required", "reason_codes": [reason], "current_evidence": []}


def _evaluation_review(reason: str) -> dict[str, Any]:
    return {
        "status": "review_required",
        "reason_codes": [reason],
        "schema_version": _EVALUATION_INPUT_VERSION,
        "source_identity": None,
        "current_evidence": [],
        "authoritative_relations": [],
        "operational_witness": None,
        "relation_projection_fingerprint": None,
        "projection_version": _VERSION,
        "evaluation_input_fingerprint": None,
    }


def _known_reason(exc: ValueError, default: str, *known: str) -> str:
    return str(exc) if str(exc) in known else default


def _source_identity_from_facts(
    facts: Mapping[str, Any], *, source_scan_id: str, workspace_slug: str
) -> dict[str, str]:
    source = facts["source"]
    capture, operation = _validate_source(
        source, source_scan_id=source_scan_id, workspace_slug=workspace_slug
    )
    identity = {
        name: _uuid(source[name])
        if name in {"workspace_id", "brand_id", "scan_run_id"}
        else _text(source[name])
        for name in _SOURCE_IDENTITY_FIELDS
    }
    identity.update(
        capture_id=capture["capture_id"],
        capture_fingerprint=capture["capture_fingerprint"],
        operation_plan_id=operation["operation_id"],
        operation_fingerprint=operation["operation_fingerprint"],
    )
    return identity


def _capture_current_from_facts(
    facts: Mapping[str, Any] | None, *, source_scan_id: str, workspace_slug: str
) -> list[dict[str, str]]:
    if facts is None:
        raise ValueError("source_unavailable")
    source = facts["source"]
    _validate_source(source, source_scan_id=source_scan_id, workspace_slug=workspace_slug)
    rows = _capture_rows(source, facts["evidence"], reject_duplicate_pairs=True)
    current = build_evidence_identity_set(
        [{key: row[key] for key in ("evidence_ref", "evidence_fingerprint")} for row in rows]
    )
    return current["evidence"]


def _authoritative_relation_projection_from_facts(
    facts: Mapping[str, Any] | None, *, source_scan_id: str, workspace_slug: str
) -> dict[str, Any]:
    if facts is None:
        raise ValueError("source_unavailable")
    source, authority = facts["source"], facts["authority"]
    if source["source_scan_id"] != str(source_scan_id).strip() or source["workspace_slug"] != str(workspace_slug).strip():
        raise ValueError("source_identity_mismatch")
    if source["operation_status"] not in {"completed", "not_required"}:
        raise ValueError("operation_not_immutable")
    if authority is None:
        raise ValueError("no_operational_authority")
    return _project(source, facts["evidence"], authority)


def _validate_source(
    source: Mapping[str, Any], *, source_scan_id: str, workspace_slug: str
) -> tuple[dict[str, str], dict[str, str]]:
    expected_scan_id, expected_workspace = _text(source_scan_id), _text(workspace_slug)
    source_scan = _text(source["source_scan_id"])
    source_workspace = _text(source["workspace_slug"])
    if (
        source_scan != expected_scan_id
        or source_workspace != expected_workspace
    ):
        raise ValueError("source_identity_mismatch")
    for name in ("workspace_id", "brand_id", "scan_run_id"):
        _text(source[name])
    if source["operation_status"] not in {"completed", "not_required"}:
        raise ValueError("operation_not_immutable")
    _text(source["canonical_domain"])
    capture = {"capture_id": _uuid(source["capture_id"]), "capture_fingerprint": _sha(source["capture_fingerprint"])}
    operation = {"operation_id": _uuid(source["operation_plan_id"]), "operation_fingerprint": _sha(source["operation_fingerprint"])}
    return capture, operation


def _capture_rows(
    source: Mapping[str, Any], evidence: Any, *, reject_duplicate_pairs: bool
) -> list[dict[str, Any]]:
    if not isinstance(evidence, list):
        raise ValueError("capture evidence")
    rows, pairs = [], set()
    for raw in evidence:
        if not isinstance(raw, Mapping):
            raise ValueError("cross-source evidence")
        if (
            not _same_uuid_or_original(raw.get("workspace_id"), source["workspace_id"])
            or not _same_uuid_or_original(raw.get("brand_id"), source["brand_id"])
            or any(
                raw.get(name) != source[name]
                for name in ("source_scan_id", "canonical_domain", "capture_id")
            )
        ):
            raise ValueError("cross-source evidence")
        record_id = _uuid(raw.get("evidence_record_id"))
        evidence_ref = _text(raw.get("evidence_ref"))
        evidence_fingerprint = _sha(raw.get("evidence_fingerprint"))
        pair = evidence_ref, evidence_fingerprint
        if reject_duplicate_pairs and pair in pairs:
            raise ValueError("duplicate capture pair")
        pairs.add(pair)
        evidence_id, source_identity_id = raw.get("evidence_id"), raw.get("source_identity_id")
        if (evidence_id is None) != (source_identity_id is None):
            raise ValueError("canonical identity")
        identity = None if evidence_id is None else (_sha(evidence_id), _sha(source_identity_id))
        rows.append(
            {
                "evidence_ref": evidence_ref,
                "evidence_fingerprint": evidence_fingerprint,
                "evidence_record_id": record_id,
                "canonical_identity": identity,
            }
        )
    return rows


def _project(source: Mapping[str, Any], evidence: Any, authority: Mapping[str, Any]) -> dict[str, Any]:
    capture, operation = _validate_source(
        source, source_scan_id=source["source_scan_id"], workspace_slug=source["workspace_slug"]
    )
    registry = {str(row["tile_id"]): str(row["component_key"]) for row in build_tile_contract_registry()["tiles"]}
    order = {tile: index for index, tile in enumerate(registry)}
    rows = _capture_rows(source, evidence, reject_duplicate_pairs=False)
    current_by_identity: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        if row["canonical_identity"] is not None:
            current_by_identity.setdefault(row["canonical_identity"], []).append(row)
    witness = authority.get("witness")
    if not isinstance(witness, Mapping) or type(witness.get("adoption_sequence")) is not int or witness["adoption_sequence"] < 1:
        raise ValueError("adoption witness")
    witness = {"canonical_memory_version": _sha(witness.get("canonical_memory_version")), "adoption_event_id": _uuid(witness.get("adoption_event_id")), "adoption_sequence": witness["adoption_sequence"], "candidate_packet_fingerprint": _sha(witness.get("candidate_packet_fingerprint")), "request_fingerprint": _sha(witness.get("request_fingerprint"))}
    accepted, relations, authoritative_basis, seen = authority.get("accepted"), [], set(), set()
    if not isinstance(accepted, list):
        raise ValueError("accepted authority")
    for tile in accepted:
        if not isinstance(tile, Mapping) or tile.get("authority_state") != "accepted" or tile.get("review_state") != "resolved" or tile.get("lifecycle_state") != "active":
            raise ValueError("non-active authority")
        tile_id, component = _text(tile.get("tile_id")), _text(tile.get("component_key"))
        assessment_state = tile.get("assessment_state")
        if assessment_state not in _ASSESSMENT_STATES:
            raise ValueError("invalid assessment state")
        basis = tile.get("basis")
        if registry.get(tile_id) != component or not isinstance(basis, list):
            raise ValueError("unknown tile")
        if assessment_state == "sin_evidencia":
            if basis:
                raise ValueError("sin_evidencia basis")
            continue
        if not basis:
            raise ValueError("unknown tile")
        for basis in basis:
            if not isinstance(basis, Mapping) or basis.get("polarity") not in _POLARITIES:
                raise ValueError("nonprojectable basis")
            key = (_sha(basis.get("evidence_id")), _sha(basis.get("source_identity_id")))
            authoritative_basis.add(key)
            matches = current_by_identity.get(key, [])
            if len(matches) != 1:
                return _review("unmatched_current_evidence")
            row = matches[0]
            relation_key = (tile_id, row["evidence_record_id"])
            if relation_key in seen or not _sha(basis.get("relation_id")):
                raise ValueError("cartesian or duplicate relation")
            seen.add(relation_key)
            relation = build_authoritative_evidence_tile_relation(tile_id=tile_id, component_key=component, disposition="relevant", evidence_ref=row["evidence_ref"], evidence_fingerprint=row["evidence_fingerprint"], capture_origin=capture, operation_origin=operation)
            relations.append(build_authoritative_evidence_tile_relation(_raw=relation, _signed=True))
    if any(len(current_by_identity.get(key, [])) != 1 for key in authoritative_basis):
        return _review("unmatched_current_evidence")
    relations.sort(key=lambda row: (order[row["tile_id"]], row["evidence_ref"], row["evidence_fingerprint"]))
    fingerprint = canonical_fingerprint(_VERSION, {"source_scan_id": source["source_scan_id"], "capture_origin": capture, "operation_origin": operation, "operational_witness": witness, "authoritative_relations": relations})
    return {"status": "available", "reason_codes": [], "authoritative_relations": relations, "operational_witness": witness, "projection_fingerprint": fingerprint}


def build_evidence_vault_sv9_authoritative_relation_witness(*, source_scan_id: str, projection: Mapping[str, Any]) -> dict[str, Any]:
    """Bind exact VA1 relations to the operational adoption that authorized them."""
    try:
        result = _witness_payload(source_scan_id, projection)
        result["witness_fingerprint"] = canonical_fingerprint(_WITNESS_VERSION, result)
        return result
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise EvidenceVaultSv9AuthoritativeRelationWitnessError("authoritative relation witness is invalid") from exc


def validate_evidence_vault_sv9_authoritative_relation_witness(value: Any) -> dict[str, Any]:
    try:
        if type(value) is not dict or set(value) != _WITNESS_FIELDS or value.get("schema_version") != _WITNESS_VERSION: raise ValueError("witness fields")
        operational = value["operational_witness"]
        projection = {"status": "available", "reason_codes": [], "authoritative_relations": value["authoritative_relations"], "operational_witness": operational, "projection_fingerprint": value["projection_fingerprint"]}
        expected = _witness_payload(value["source_scan_id"], projection)
        expected["witness_fingerprint"] = canonical_fingerprint(_WITNESS_VERSION, expected)
        if value != expected: raise ValueError("witness replay")
        return expected
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise EvidenceVaultSv9AuthoritativeRelationWitnessError("authoritative relation witness is invalid") from exc


def _witness_payload(source_scan_id: Any, projection: Any) -> dict[str, Any]:
    source = _text(source_scan_id)
    if type(projection) is not dict or set(projection) != {"status", "reason_codes", "authoritative_relations", "operational_witness", "projection_fingerprint"} or projection["status"] != "available" or projection["reason_codes"] != []: raise ValueError("projection")
    operational = projection["operational_witness"]
    if type(operational) is not dict or set(operational) != _OPERATIONAL_WITNESS_FIELDS: raise ValueError("operational witness")
    operational = {"canonical_memory_version": _sha(operational["canonical_memory_version"]), "adoption_event_id": _uuid(operational["adoption_event_id"]), "adoption_sequence": operational["adoption_sequence"], "candidate_packet_fingerprint": _sha(operational["candidate_packet_fingerprint"]), "request_fingerprint": _sha(operational["request_fingerprint"])}
    if type(operational["adoption_sequence"]) is not int or operational["adoption_sequence"] < 1: raise ValueError("adoption sequence")
    if type(projection["authoritative_relations"]) is not list or not projection["authoritative_relations"]: raise ValueError("relations")
    rows = [build_authoritative_evidence_tile_relation(_raw=row, _signed=True) for row in projection["authoritative_relations"]]
    if any(row["disposition"] != "relevant" for row in rows): raise ValueError("operational relation disposition")
    capture, operation = rows[0]["capture_origin"], rows[0]["operation_origin"]
    if any(row["capture_origin"] != capture or row["operation_origin"] != operation for row in rows): raise ValueError("relation origins")
    order = {str(row["tile_id"]): index for index, row in enumerate(build_tile_contract_registry()["tiles"])}
    keys = [(order[row["tile_id"]], row["evidence_ref"], row["evidence_fingerprint"]) for row in rows]
    if keys != sorted(keys) or len(keys) != len(set(keys)): raise ValueError("relation order")
    fingerprint = canonical_fingerprint(_VERSION, {"source_scan_id": source, "capture_origin": capture, "operation_origin": operation, "operational_witness": operational, "authoritative_relations": rows})
    if projection["projection_fingerprint"] != fingerprint: raise ValueError("projection fingerprint")
    return {"schema_version": _WITNESS_VERSION, "source_scan_id": source, "operational_witness": operational, "authoritative_relations": rows, "projection_fingerprint": fingerprint}


def _text(value: Any) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("text identity")
    return value


def _sha(value: Any) -> str:
    value = _text(value)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("fingerprint")
    return value


def _uuid(value: Any) -> str:
    try:
        if type(value) is not str: raise ValueError
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("uuid identity") from exc


def _same_uuid_or_original(left: Any, right: Any) -> bool:
    try:
        return _uuid(left) == _uuid(right)
    except ValueError:
        return left == right
# fmt: on
