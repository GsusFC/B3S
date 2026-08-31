"""Read-only projection of accepted Vault evidence relations into VA1."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import UUID

from src.services.evidence_vault_canonical_core import (
    build_tile_contract_registry,
    canonical_fingerprint,
)
from src.services.evidence_vault_sv9_judgment_delta import (
    build_authoritative_evidence_tile_relation,
)

# fmt: off

_VERSION = "evidence-vault-sv9-authoritative-relation-projection-v1"
_POLARITIES = frozenset({"supports", "contradicts", "demonstrates_absence"})


def project_evidence_vault_sv9_authoritative_relations(
    *, repository: Any, source_scan_id: str, workspace_slug: str = "b3s"
) -> dict[str, Any]:
    """Return VA1 relations only when every current persisted fact is proven."""

    try:
        facts = repository.load_evidence_vault_sv9_authoritative_relation_facts(
            source_scan_id, workspace_slug=workspace_slug
        )
        if facts is None:
            return _review("source_unavailable")
        source, authority = facts["source"], facts["authority"]
        if source["source_scan_id"] != str(source_scan_id).strip() or source["workspace_slug"] != str(workspace_slug).strip():
            return _review("source_identity_mismatch")
        if source["operation_status"] not in {"completed", "not_required"}:
            return _review("operation_not_immutable")
        if authority is None:
            return _review("no_operational_authority")
        return _project(source, facts["evidence"], authority)
    except Exception:
        return _review("invalid_authoritative_facts")


def _review(reason: str) -> dict[str, Any]:
    return {"status": "review_required", "reason_codes": [reason], "authoritative_relations": [], "operational_witness": None, "projection_fingerprint": None}


def _project(source: Mapping[str, Any], evidence: Any, authority: Mapping[str, Any]) -> dict[str, Any]:
    _text(source["canonical_domain"])
    capture = {"capture_id": _uuid(source["capture_id"]), "capture_fingerprint": _sha(source["capture_fingerprint"])}
    operation = {"operation_id": _uuid(source["operation_plan_id"]), "operation_fingerprint": _sha(source["operation_fingerprint"])}
    registry = {str(row["tile_id"]): str(row["component_key"]) for row in build_tile_contract_registry()["tiles"]}
    order = {tile: index for index, tile in enumerate(registry)}
    current: dict[tuple[str, str], Mapping[str, Any]] = {}
    if not isinstance(evidence, list) or not evidence:
        return _review("unmatched_current_evidence")
    for row in evidence:
        if not isinstance(row, Mapping) or any(row.get(name) != source[name] for name in ("workspace_id", "brand_id", "source_scan_id", "canonical_domain", "capture_id")):
            raise ValueError("cross-source evidence")
        key = (_sha(row.get("evidence_id")), _sha(row.get("source_identity_id")))
        if key in current:
            raise ValueError("ambiguous evidence identity")
        current[key] = {"evidence_ref": _text(row.get("evidence_ref")), "evidence_fingerprint": _sha(row.get("evidence_fingerprint")), "evidence_record_id": _uuid(row.get("evidence_record_id"))}
    witness = authority.get("witness")
    if not isinstance(witness, Mapping) or not isinstance(witness.get("adoption_sequence"), int) or witness["adoption_sequence"] < 1:
        raise ValueError("adoption witness")
    witness = {"canonical_memory_version": _sha(witness.get("canonical_memory_version")), "adoption_event_id": _uuid(witness.get("adoption_event_id")), "adoption_sequence": witness["adoption_sequence"], "candidate_packet_fingerprint": _sha(witness.get("candidate_packet_fingerprint")), "request_fingerprint": _sha(witness.get("request_fingerprint"))}
    accepted, relations, covered, seen = authority.get("accepted"), [], set(), set()
    if not isinstance(accepted, list):
        raise ValueError("accepted authority")
    for tile in accepted:
        if not isinstance(tile, Mapping) or tile.get("authority_state") != "accepted" or tile.get("review_state") != "resolved" or tile.get("lifecycle_state") != "active":
            raise ValueError("non-active authority")
        tile_id, component = _text(tile.get("tile_id")), _text(tile.get("component_key"))
        if registry.get(tile_id) != component or not isinstance(tile.get("basis"), list) or not tile["basis"]:
            raise ValueError("unknown tile")
        for basis in tile["basis"]:
            if not isinstance(basis, Mapping) or basis.get("polarity") not in _POLARITIES:
                raise ValueError("nonprojectable basis")
            key = (_sha(basis.get("evidence_id")), _sha(basis.get("source_identity_id")))
            row = current.get(key)
            if row is None:
                return _review("unmatched_current_evidence")
            relation_key = (tile_id, row["evidence_record_id"])
            if relation_key in seen or not _sha(basis.get("relation_id")):
                raise ValueError("cartesian or duplicate relation")
            seen.add(relation_key); covered.add(key)
            relation = build_authoritative_evidence_tile_relation(tile_id=tile_id, component_key=component, disposition="relevant", evidence_ref=row["evidence_ref"], evidence_fingerprint=row["evidence_fingerprint"], capture_origin=capture, operation_origin=operation)
            relations.append(build_authoritative_evidence_tile_relation(_raw=relation, _signed=True))
    if set(current) != covered:
        return _review("unmatched_current_evidence")
    relations.sort(key=lambda row: (order[row["tile_id"]], row["evidence_ref"], row["evidence_fingerprint"]))
    fingerprint = canonical_fingerprint(_VERSION, {"source_scan_id": source["source_scan_id"], "capture_origin": capture, "operation_origin": operation, "operational_witness": witness, "authoritative_relations": relations})
    return {"status": "available", "reason_codes": [], "authoritative_relations": relations, "operational_witness": witness, "projection_fingerprint": fingerprint}


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("text identity")
    return value


def _sha(value: Any) -> str:
    value = _text(value)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("fingerprint")
    return value


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("uuid identity") from exc
# fmt: on
