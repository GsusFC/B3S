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

EVIDENCE_VAULT_SV9_AUTHORITATIVE_RELATION_PROJECTION_VERSION = "evidence-vault-sv9-authoritative-relation-projection-v2"; _LEGACY_PROJECTION_VERSION = "evidence-vault-sv9-authoritative-relation-projection-v1"
_VERSION = EVIDENCE_VAULT_SV9_AUTHORITATIVE_RELATION_PROJECTION_VERSION
EVIDENCE_VAULT_SV9_EVALUATION_INPUT_VERSION = "evidence-vault-sv9-evaluation-input-v2"
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
    "current_identity_bindings authoritative_relations authority_continuity "
    "authority_coverage_loss reopen_tile_ids operational_witness "
    "relation_projection_fingerprint projection_version non_authoritative_hints "
    "evaluation_input_fingerprint".split()
)
_EVALUATION_HINT_SEED_FIELDS = frozenset(
    "hint_id tile_id component_key evidence_record_id provenance_fingerprint".split()
)
_EVALUATION_HINT_FIELDS = _EVALUATION_HINT_SEED_FIELDS | frozenset(
    "capture_id capture_fingerprint authority runtime_effect hint_fingerprint".split()
)
_EVALUATION_HINT_FINGERPRINT_VERSION = "evidence-vault-sv9-evaluation-hint-fingerprint-v1"
_IDENTITY_BINDING_FIELDS = frozenset("evidence_record_id evidence_ref evidence_fingerprint evidence_id source_identity_id".split())
_BASIS_MATCH_FIELDS = frozenset("relation_id evidence_id source_identity_id continuity_state matched_current_record_ids".split())
_CONTINUITY_FIELDS = frozenset("tile_id component_key continuity_state basis_matches".split())
_COVERAGE_LOSS_FIELDS = frozenset("tile_id component_key reason basis_facts".split())
_CONTINUITY_STATES = frozenset({"stable", "missing", "changed", "ambiguous_duplicate"})
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
        source = facts["source"]
        rows = _capture_rows(source, facts["evidence"], reject_duplicate_pairs=False)
        try:
            current_evidence = build_evidence_identity_set(
                [{key: row[key] for key in ("evidence_ref", "evidence_fingerprint")} for row in rows]
            )["evidence"]
        except ValueError:
            identities = [row["canonical_identity"] for row in rows if row["canonical_identity"] is not None]
            if len(identities) == len(set(identities)): raise
            current_evidence = []
    except ValueError as exc:
        return _evaluation_review(
            _known_reason(exc, "invalid_capture_current", "source_identity_mismatch", "operation_not_immutable")
        )
    except Exception:
        return _evaluation_review("invalid_capture_current")

    try:
        projection = _authoritative_relation_projection_from_facts(
            facts, source_scan_id=source_scan_id, workspace_slug=workspace_slug, rows=rows
        )
    except ValueError as exc:
        return _evaluation_review(
            _known_reason(
                exc,
                "invalid_authoritative_facts",
                "source_identity_mismatch",
                "operation_not_immutable",
                "no_operational_authority",
                "ambiguous_current_evidence",
            )
        )
    except Exception:
        return _evaluation_review("invalid_authoritative_facts")
    if projection.get("status") != "available":
        reasons = projection.get("reason_codes")
        return _evaluation_review(reasons[0] if isinstance(reasons, list) and reasons and isinstance(reasons[0], str) else "invalid_authoritative_facts", projection)

    try:
        relations = projection["authoritative_relations"]
        witness = projection["operational_witness"]
        relation_fingerprint = _sha(projection["projection_fingerprint"])
        if not isinstance(relations, list) or not isinstance(witness, dict):
            raise ValueError("invalid_authoritative_facts")
        non_authoritative_hints = _build_evaluation_hints(
            facts["evaluation_hint_seeds"],
            source=source_identity,
            bindings=projection["current_identity_bindings"],
        )
        projection_version = _text(_VERSION)
        payload = {
            "source_identity": source_identity,
            "current_evidence": current_evidence,
            "current_identity_bindings": projection["current_identity_bindings"],
            "authoritative_relations": relations,
            "authority_continuity": projection["authority_continuity"],
            "authority_coverage_loss": projection["authority_coverage_loss"],
            "reopen_tile_ids": projection["reopen_tile_ids"],
            "operational_witness": witness,
            "relation_projection_fingerprint": relation_fingerprint,
            "projection_version": projection_version,
            "non_authoritative_hints": non_authoritative_hints,
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
        bindings = _validate_current_identity_bindings(value["current_identity_bindings"])
        if sorted((row["evidence_ref"], row["evidence_fingerprint"]) for row in bindings) != [
            (row["evidence_ref"], row["evidence_fingerprint"]) for row in current["evidence"]
        ]:
            raise ValueError("evaluation input evidence bindings")
        non_authoritative_hints = _validate_evaluation_hints(
            value["non_authoritative_hints"], source=source, bindings=bindings
        )
        relations = _validate_evaluation_relations(
            value["authoritative_relations"],
            source=source,
            current=current["evidence"],
        )
        witness = _validate_evaluation_operational_witness(
            value["operational_witness"]
        )
        continuity = _validate_authority_continuity(value["authority_continuity"], bindings)
        coverage_loss = _validate_authority_coverage_loss(value["authority_coverage_loss"], continuity)
        reopen_tile_ids = _validate_reopen_tile_ids(value["reopen_tile_ids"], continuity)
        _validate_relation_partition(relations, continuity, coverage_loss, reopen_tile_ids, bindings)
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
                "current_identity_bindings": bindings,
                "authority_continuity": continuity,
                "authority_coverage_loss": coverage_loss,
                "reopen_tile_ids": reopen_tile_ids,
                "authoritative_relations": relations,
            },
        )
        if value["relation_projection_fingerprint"] != expected_projection_fingerprint:
            raise ValueError("evaluation input projection fingerprint")
        payload = {
            "source_identity": source,
            "current_evidence": current["evidence"],
            "current_identity_bindings": bindings,
            "authoritative_relations": relations,
            "authority_continuity": continuity,
            "authority_coverage_loss": coverage_loss,
            "reopen_tile_ids": reopen_tile_ids,
            "operational_witness": witness,
            "relation_projection_fingerprint": expected_projection_fingerprint,
            "projection_version": _VERSION,
            "non_authoritative_hints": non_authoritative_hints,
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


def _binding_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple((value is None, "" if value is None else value) for value in (
        row["source_identity_id"], row["evidence_id"], row["evidence_ref"],
        row["evidence_fingerprint"], row["evidence_record_id"],
    ))


def _validate_current_identity_bindings(value: Any) -> list[dict[str, Any]]:
    if type(value) is not list: raise ValueError("evaluation input evidence bindings")
    rows, records, pairs, identities = [], set(), set(), set()
    for raw in value:
        if type(raw) is not dict or set(raw) != _IDENTITY_BINDING_FIELDS: raise ValueError("evaluation input evidence bindings")
        row = {
            "evidence_record_id": _uuid(raw["evidence_record_id"]),
            "evidence_ref": _text(raw["evidence_ref"]),
            "evidence_fingerprint": _sha(raw["evidence_fingerprint"]),
            "evidence_id": None if raw["evidence_id"] is None else _sha(raw["evidence_id"]),
            "source_identity_id": None if raw["source_identity_id"] is None else _sha(raw["source_identity_id"]),
        }
        if (row["evidence_id"] is None) != (row["source_identity_id"] is None): raise ValueError("canonical identity")
        pair = row["evidence_ref"], row["evidence_fingerprint"]
        identity = row["evidence_id"], row["source_identity_id"]
        if row["evidence_record_id"] in records or pair in pairs or (identity != (None, None) and identity in identities): raise ValueError("ambiguous_current_evidence")
        records.add(row["evidence_record_id"]); pairs.add(pair)
        if row["evidence_id"] is not None: identities.add(identity)
        rows.append(row)
    if rows != sorted(rows, key=_binding_key): raise ValueError("evaluation input evidence bindings")
    return rows


def _build_evaluation_hints(
    value: Any, *, source: Mapping[str, Any], bindings: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    if type(value) is not list:
        raise ValueError("evaluation hint seeds")
    registry, components = _registry_maps()
    records = {row["evidence_record_id"] for row in bindings}
    rows, seen_hint_ids, seen_provenance, seen_pairs, seen_fingerprints = [], set(), set(), set(), set()
    for raw in value:
        if type(raw) is not dict or set(raw) != _EVALUATION_HINT_SEED_FIELDS:
            raise ValueError("evaluation hint seeds")
        hint_id = _uuid(raw["hint_id"])
        tile_id, component_key = _text(raw["tile_id"]), _text(raw["component_key"])
        evidence_record_id = _uuid(raw["evidence_record_id"])
        provenance_fingerprint = _sha(raw["provenance_fingerprint"])
        if (
            components.get(tile_id) != component_key
            or evidence_record_id not in records
            or hint_id in seen_hint_ids
            or provenance_fingerprint in seen_provenance
            or (tile_id, evidence_record_id) in seen_pairs
        ):
            raise ValueError("evaluation hint seeds")
        unsigned = {
            "hint_id": hint_id,
            "tile_id": tile_id,
            "component_key": component_key,
            "evidence_record_id": evidence_record_id,
            "provenance_fingerprint": provenance_fingerprint,
            "capture_id": source["capture_id"],
            "capture_fingerprint": source["capture_fingerprint"],
            "authority": False,
            "runtime_effect": "evaluation_routing_only",
        }
        hint_fingerprint = canonical_fingerprint(_EVALUATION_HINT_FINGERPRINT_VERSION, unsigned)
        if hint_fingerprint in seen_fingerprints:
            raise ValueError("evaluation hint seeds")
        rows.append(unsigned | {"hint_fingerprint": hint_fingerprint})
        seen_hint_ids.add(hint_id); seen_provenance.add(provenance_fingerprint)
        seen_pairs.add((tile_id, evidence_record_id)); seen_fingerprints.add(hint_fingerprint)
    return sorted(rows, key=lambda row: (registry[row["tile_id"]], row["evidence_record_id"], row["provenance_fingerprint"]))


def _validate_evaluation_hints(
    value: Any, *, source: Mapping[str, Any], bindings: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    if type(value) is not list:
        raise ValueError("evaluation hints")
    registry, components = _registry_maps()
    records = {row["evidence_record_id"] for row in bindings}
    rows, seen_hint_ids, seen_provenance, seen_pairs, seen_fingerprints = [], set(), set(), set(), set()
    for raw in value:
        if type(raw) is not dict or set(raw) != _EVALUATION_HINT_FIELDS:
            raise ValueError("evaluation hints")
        hint_id = _uuid(raw["hint_id"])
        tile_id, component_key = _text(raw["tile_id"]), _text(raw["component_key"])
        evidence_record_id = _uuid(raw["evidence_record_id"])
        provenance_fingerprint = _sha(raw["provenance_fingerprint"])
        capture_id, capture_fingerprint = _uuid(raw["capture_id"]), _sha(raw["capture_fingerprint"])
        if (
            components.get(tile_id) != component_key
            or evidence_record_id not in records
            or capture_id != source["capture_id"]
            or capture_fingerprint != source["capture_fingerprint"]
            or raw["authority"] is not False
            or raw["runtime_effect"] != "evaluation_routing_only"
            or hint_id in seen_hint_ids
            or provenance_fingerprint in seen_provenance
            or (tile_id, evidence_record_id) in seen_pairs
        ):
            raise ValueError("evaluation hints")
        unsigned = {
            "hint_id": hint_id,
            "tile_id": tile_id,
            "component_key": component_key,
            "evidence_record_id": evidence_record_id,
            "provenance_fingerprint": provenance_fingerprint,
            "capture_id": capture_id,
            "capture_fingerprint": capture_fingerprint,
            "authority": False,
            "runtime_effect": "evaluation_routing_only",
        }
        hint_fingerprint = canonical_fingerprint(_EVALUATION_HINT_FINGERPRINT_VERSION, unsigned)
        if raw["hint_fingerprint"] != hint_fingerprint or hint_fingerprint in seen_fingerprints:
            raise ValueError("evaluation hints")
        rows.append(unsigned | {"hint_fingerprint": hint_fingerprint})
        seen_hint_ids.add(hint_id); seen_provenance.add(provenance_fingerprint)
        seen_pairs.add((tile_id, evidence_record_id)); seen_fingerprints.add(hint_fingerprint)
    canonical = sorted(rows, key=lambda row: (registry[row["tile_id"]], row["evidence_record_id"], row["provenance_fingerprint"]))
    if value != canonical:
        raise ValueError("evaluation hints")
    return canonical


def _registry_maps() -> tuple[dict[str, int], dict[str, str]]:
    rows = build_tile_contract_registry()["tiles"]
    return ({str(row["tile_id"]): i for i, row in enumerate(rows)},
            {str(row["tile_id"]): str(row["component_key"]) for row in rows})


def _tile_state(states: list[str]) -> str:
    if not states or all(state == "stable" for state in states): return "stable"
    if "ambiguous_duplicate" in states: return "ambiguous_duplicate"
    if "changed" in states: return "changed"
    return "missing"


def _validate_authority_continuity(value: Any, bindings: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if type(value) is not list: raise ValueError("authority continuity")
    registry, components = _registry_maps(); by_identity, by_source, order = {}, {}, {row["evidence_record_id"]: i for i, row in enumerate(bindings)}
    for row in bindings:
        if row["evidence_id"] is not None:
            by_identity.setdefault((row["evidence_id"], row["source_identity_id"]), []).append(row)
            by_source.setdefault(row["source_identity_id"], []).append(row)
    result, seen_tiles, seen_relations = [], set(), set()
    for raw in value:
        if type(raw) is not dict or set(raw) != _CONTINUITY_FIELDS: raise ValueError("authority continuity")
        tile, component, state = _text(raw["tile_id"]), _text(raw["component_key"]), _text(raw["continuity_state"])
        if tile in seen_tiles or components.get(tile) != component or state not in _CONTINUITY_STATES: raise ValueError("authority continuity")
        matches, states = [], []
        if type(raw["basis_matches"]) is not list: raise ValueError("authority continuity")
        for match in raw["basis_matches"]:
            if type(match) is not dict or set(match) != _BASIS_MATCH_FIELDS: raise ValueError("authority continuity")
            relation, eid, sid = _sha(match["relation_id"]), _sha(match["evidence_id"]), _sha(match["source_identity_id"])
            exact, same = by_identity.get((eid, sid), []), by_source.get(sid, [])
            expected = "ambiguous_duplicate" if len(exact) > 1 else "stable" if exact else "changed" if same else "missing"
            ids = sorted((row["evidence_record_id"] for row in (exact if expected != "changed" else same)), key=order.__getitem__)
            if relation in seen_relations or match["continuity_state"] != expected or match["matched_current_record_ids"] != ids: raise ValueError("authority continuity")
            matches.append({"relation_id": relation, "evidence_id": eid, "source_identity_id": sid, "continuity_state": expected, "matched_current_record_ids": ids})
            states.append(expected); seen_relations.add(relation)
        if matches != sorted(matches, key=lambda row: row["relation_id"]) or state != _tile_state(states): raise ValueError("authority continuity")
        result.append({"tile_id": tile, "component_key": component, "continuity_state": state, "basis_matches": matches}); seen_tiles.add(tile)
    if [row["tile_id"] for row in result] != sorted((row["tile_id"] for row in result), key=registry.__getitem__): raise ValueError("authority continuity")
    return result


def _validate_authority_coverage_loss(value: Any, continuity: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if type(value) is not list: raise ValueError("authority coverage loss")
    registry, components = _registry_maps(); expected = {row["tile_id"]: row for row in continuity}; result, seen = [], set()
    for raw in value:
        if type(raw) is not dict or set(raw) != _COVERAGE_LOSS_FIELDS: raise ValueError("authority coverage loss")
        tile, component, reason = _text(raw["tile_id"]), _text(raw["component_key"]), _text(raw["reason"])
        row = expected.get(tile)
        if row is None or tile in seen or components.get(tile) != component: raise ValueError("authority coverage loss")
        facts = [dict(match) for match in row["basis_matches"] if match["continuity_state"] in {"missing", "changed"}]
        if not facts or raw["basis_facts"] != facts or reason != ("historical_basis_changed" if any(f["continuity_state"] == "changed" for f in facts) else "historical_basis_missing"): raise ValueError("authority coverage loss")
        result.append({"tile_id": tile, "component_key": component, "reason": reason, "basis_facts": facts}); seen.add(tile)
    if [row["tile_id"] for row in result] != sorted((row["tile_id"] for row in result), key=registry.__getitem__): raise ValueError("authority coverage loss")
    return result


def _validate_reopen_tile_ids(value: Any, continuity: list[Mapping[str, Any]]) -> list[str]:
    if type(value) is not list or any(type(tile) is not str for tile in value): raise ValueError("reopen tile ids")
    registry, _ = _registry_maps(); expected = [row["tile_id"] for row in continuity if row["continuity_state"] in {"missing", "changed"}]
    if value != sorted(expected, key=registry.__getitem__) or len(value) != len(set(value)): raise ValueError("reopen tile ids")
    return list(value)


def _validate_relation_partition(relations, continuity, coverage, reopen, bindings) -> None:
    by_record = {row["evidence_record_id"]: row for row in bindings}; grouped = {}
    for row in relations: grouped.setdefault(row["tile_id"], []).append(row)
    if set(row["tile_id"] for row in coverage) != set(reopen) or set(grouped) - {row["tile_id"] for row in continuity}: raise ValueError("authority coverage loss")
    for tile in continuity:
        actual = grouped.get(tile["tile_id"], [])
        if tile["continuity_state"] != "stable":
            if actual: raise ValueError("authority continuity relations")
            continue
        ids = [record for match in tile["basis_matches"] for record in match["matched_current_record_ids"]]
        pairs = [(by_record[record]["evidence_ref"], by_record[record]["evidence_fingerprint"]) for record in ids]
        actual_pairs = [(row["evidence_ref"], row["evidence_fingerprint"]) for row in actual]
        if len(actual) != len(ids) or sorted(actual_pairs) != sorted(pairs): raise ValueError("authority continuity relations")


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
                "ambiguous_current_evidence",
            )
        )
    except Exception:
        return _review("invalid_authoritative_facts")


def _review(reason: str, bindings=None, continuity=None, coverage=None, reopen=None) -> dict[str, Any]:
    return {"status": "review_required", "reason_codes": [reason], "authoritative_relations": [], "current_identity_bindings": list(bindings or []), "authority_continuity": list(continuity or []), "authority_coverage_loss": list(coverage or []), "reopen_tile_ids": list(reopen or []), "operational_witness": None, "projection_fingerprint": None}


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


def _evaluation_review(reason: str, projection: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result = {
        "status": "review_required",
        "reason_codes": [reason],
        "schema_version": _EVALUATION_INPUT_VERSION,
        "source_identity": None,
        "current_evidence": [],
        "current_identity_bindings": [],
        "authoritative_relations": [],
        "authority_continuity": [],
        "authority_coverage_loss": [],
        "reopen_tile_ids": [],
        "operational_witness": None,
        "relation_projection_fingerprint": None,
        "projection_version": _VERSION,
        "non_authoritative_hints": [],
        "evaluation_input_fingerprint": None,
    }
    if projection:
        for key in ("current_identity_bindings", "authority_continuity", "authority_coverage_loss", "reopen_tile_ids"):
            if key in projection: result[key] = projection[key]
    return result


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
    facts: Mapping[str, Any] | None, *, source_scan_id: str, workspace_slug: str,
    rows: list[Mapping[str, Any]] | None = None,
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
    return _project(source, facts["evidence"], authority, rows=rows)


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
    rows, pairs, record_ids = [], set(), set()
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
        if record_id in record_ids: raise ValueError("duplicate evidence record")
        record_ids.add(record_id)
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
                "evidence_id": None if identity is None else identity[0],
                "source_identity_id": None if identity is None else identity[1],
                "canonical_identity": identity,
            }
        )
    return rows


def _identity_binding(row: Mapping[str, Any]) -> dict[str, Any]:
    identity = row["canonical_identity"]
    return {"evidence_record_id": row["evidence_record_id"], "evidence_ref": row["evidence_ref"], "evidence_fingerprint": row["evidence_fingerprint"], "evidence_id": None if identity is None else identity[0], "source_identity_id": None if identity is None else identity[1]}


def _project(source: Mapping[str, Any], evidence: Any, authority: Mapping[str, Any], *, rows=None) -> dict[str, Any]:
    capture, operation = _validate_source(source, source_scan_id=source["source_scan_id"], workspace_slug=source["workspace_slug"])
    order, components = _registry_maps(); rows = list(rows) if rows is not None else _capture_rows(source, evidence, reject_duplicate_pairs=False)
    bindings = sorted((_identity_binding(row) for row in rows), key=_binding_key)
    binding_order = {row["evidence_record_id"]: i for i, row in enumerate(bindings)}
    current_by_identity, current_by_source, pair_identities = {}, {}, {}
    for row in rows:
        pair = row["evidence_ref"], row["evidence_fingerprint"]
        if pair in pair_identities and pair_identities[pair] != row["canonical_identity"]: raise ValueError("conflicting_current_identity")
        pair_identities[pair] = row["canonical_identity"]
        if row["canonical_identity"] is not None:
            current_by_identity.setdefault(row["canonical_identity"], []).append(row)
            current_by_source.setdefault(row["source_identity_id"], []).append(row)
    witness = authority.get("witness") if isinstance(authority, Mapping) else None
    if not isinstance(witness, Mapping) or type(witness.get("adoption_sequence")) is not int or witness["adoption_sequence"] < 1: raise ValueError("adoption witness")
    witness = {"canonical_memory_version": _sha(witness.get("canonical_memory_version")), "adoption_event_id": _uuid(witness.get("adoption_event_id")), "adoption_sequence": witness["adoption_sequence"], "candidate_packet_fingerprint": _sha(witness.get("candidate_packet_fingerprint")), "request_fingerprint": _sha(witness.get("request_fingerprint"))}
    accepted = authority.get("accepted") if isinstance(authority, Mapping) else None
    if not isinstance(accepted, list): raise ValueError("accepted authority")
    tiles, seen_tiles, seen_relation_ids = [], set(), set()
    for raw in accepted:
        if not isinstance(raw, Mapping) or raw.get("authority_state") != "accepted" or raw.get("review_state") != "resolved" or raw.get("lifecycle_state") != "active": raise ValueError("non-active authority")
        tile, component, state, basis = _text(raw.get("tile_id")), _text(raw.get("component_key")), raw.get("assessment_state"), raw.get("basis")
        if tile in seen_tiles or components.get(tile) != component or state not in _ASSESSMENT_STATES or not isinstance(basis, list): raise ValueError("unknown tile")
        if state == "sin_evidencia":
            if basis: raise ValueError("sin_evidencia basis")
            normalized = []
        else:
            if not basis: raise ValueError("unknown tile")
            normalized = []
            for raw_basis in basis:
                if not isinstance(raw_basis, Mapping) or raw_basis.get("polarity") not in _POLARITIES: raise ValueError("nonprojectable basis")
                relation_id, eid, sid = _sha(raw_basis.get("relation_id")), _sha(raw_basis.get("evidence_id")), _sha(raw_basis.get("source_identity_id"))
                if relation_id in seen_relation_ids: raise ValueError("cartesian or duplicate relation")
                normalized.append({"relation_id": relation_id, "evidence_id": eid, "source_identity_id": sid}); seen_relation_ids.add(relation_id)
            normalized.sort(key=lambda row: row["relation_id"])
        tiles.append({"tile_id": tile, "component_key": component, "basis": normalized}); seen_tiles.add(tile)
    tiles.sort(key=lambda row: order[row["tile_id"]])
    identities = [row["canonical_identity"] for row in rows if row["canonical_identity"] is not None]
    continuity, coverage, reopen, relations, seen_relations = [], [], [], [], set(); ambiguous = len(pair_identities) != len(rows) or len(identities) != len(set(identities))
    for tile in tiles:
        matches, states = [], []
        for basis in tile["basis"]:
            key = basis["evidence_id"], basis["source_identity_id"]; exact, same = current_by_identity.get(key, []), current_by_source.get(key[1], [])
            state = "ambiguous_duplicate" if len(exact) > 1 else "stable" if len(exact) == 1 else "changed" if same else "missing"
            if state == "ambiguous_duplicate": ambiguous = True
            matched = exact if state != "changed" else same
            ids = sorted((row["evidence_record_id"] for row in matched), key=binding_order.__getitem__)
            matches.append({"relation_id": basis["relation_id"], "evidence_id": key[0], "source_identity_id": key[1], "continuity_state": state, "matched_current_record_ids": ids}); states.append(state)
        tile_state = _tile_state(states); row = {"tile_id": tile["tile_id"], "component_key": tile["component_key"], "continuity_state": tile_state, "basis_matches": matches}; continuity.append(row)
        if tile_state in {"missing", "changed"}:
            reopen.append(tile["tile_id"]); lost = [match for match in matches if match["continuity_state"] in {"missing", "changed"}]
            coverage.append({"tile_id": tile["tile_id"], "component_key": tile["component_key"], "reason": "historical_basis_changed" if any(match["continuity_state"] == "changed" for match in lost) else "historical_basis_missing", "basis_facts": lost}); continue
        if tile_state == "ambiguous_duplicate": continue
        for match in matches:
            current = current_by_identity[(match["evidence_id"], match["source_identity_id"])]
            if len(current) != 1: raise ValueError("ambiguous_current_evidence")
            row = current[0]; key = tile["tile_id"], row["evidence_record_id"]
            if key in seen_relations: raise ValueError("cartesian or duplicate relation")
            seen_relations.add(key); relation = build_authoritative_evidence_tile_relation(tile_id=tile["tile_id"], component_key=tile["component_key"], disposition="relevant", evidence_ref=row["evidence_ref"], evidence_fingerprint=row["evidence_fingerprint"], capture_origin=capture, operation_origin=operation)
            relations.append(build_authoritative_evidence_tile_relation(_raw=relation, _signed=True))
    reopen.sort(key=order.__getitem__); coverage.sort(key=lambda row: order[row["tile_id"]]); relations.sort(key=lambda row: (order[row["tile_id"]], row["evidence_ref"], row["evidence_fingerprint"]))
    if ambiguous: return _review("ambiguous_current_evidence", bindings, continuity, coverage, reopen)
    payload = {"source_scan_id": source["source_scan_id"], "capture_origin": capture, "operation_origin": operation, "operational_witness": witness, "current_identity_bindings": bindings, "authority_continuity": continuity, "authority_coverage_loss": coverage, "reopen_tile_ids": reopen, "authoritative_relations": relations}
    return {"status": "available", "reason_codes": [], "authoritative_relations": relations, "current_identity_bindings": bindings, "authority_continuity": continuity, "authority_coverage_loss": coverage, "reopen_tile_ids": reopen, "operational_witness": witness, "projection_fingerprint": canonical_fingerprint(_VERSION, payload)}


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
    fields = {"status", "reason_codes", "authoritative_relations", "operational_witness", "projection_fingerprint"}; v2_fields = {"current_identity_bindings", "authority_continuity", "authority_coverage_loss", "reopen_tile_ids"}
    if type(projection) is not dict or set(projection) not in (fields, fields | v2_fields) or projection["status"] != "available" or projection["reason_codes"] != []: raise ValueError("projection")
    is_v2 = set(projection) == fields | v2_fields
    operational = projection["operational_witness"]
    if type(operational) is not dict or set(operational) != _OPERATIONAL_WITNESS_FIELDS: raise ValueError("operational witness")
    operational = {"canonical_memory_version": _sha(operational["canonical_memory_version"]), "adoption_event_id": _uuid(operational["adoption_event_id"]), "adoption_sequence": operational["adoption_sequence"], "candidate_packet_fingerprint": _sha(operational["candidate_packet_fingerprint"]), "request_fingerprint": _sha(operational["request_fingerprint"])}
    if type(operational["adoption_sequence"]) is not int or operational["adoption_sequence"] < 1: raise ValueError("adoption sequence")
    if type(projection["authoritative_relations"]) is not list: raise ValueError("relations")
    rows = [build_authoritative_evidence_tile_relation(_raw=row, _signed=True) for row in projection["authoritative_relations"]]
    if is_v2:
        bindings = _validate_current_identity_bindings(projection["current_identity_bindings"]); continuity = _validate_authority_continuity(projection["authority_continuity"], bindings); coverage = _validate_authority_coverage_loss(projection["authority_coverage_loss"], continuity); reopen = _validate_reopen_tile_ids(projection["reopen_tile_ids"], continuity); _validate_relation_partition(rows, continuity, coverage, reopen, bindings)
        if coverage or reopen or any(row["continuity_state"] != "stable" for row in continuity): raise ValueError("relations")
    if not rows: raise ValueError("relations")
    if any(row["disposition"] != "relevant" for row in rows): raise ValueError("operational relation disposition")
    capture, operation = rows[0]["capture_origin"], rows[0]["operation_origin"]
    if any(row["capture_origin"] != capture or row["operation_origin"] != operation for row in rows): raise ValueError("relation origins")
    order = {str(row["tile_id"]): index for index, row in enumerate(build_tile_contract_registry()["tiles"])}
    keys = [(order[row["tile_id"]], row["evidence_ref"], row["evidence_fingerprint"]) for row in rows]
    if keys != sorted(keys) or len(keys) != len(set(keys)): raise ValueError("relation order")
    payload = {"source_scan_id": source, "capture_origin": capture, "operation_origin": operation, "operational_witness": operational, "authoritative_relations": rows}
    if is_v2:
        payload.update(current_identity_bindings=bindings, authority_continuity=continuity, authority_coverage_loss=coverage, reopen_tile_ids=reopen)
        if projection["projection_fingerprint"] != canonical_fingerprint(_VERSION, payload): raise ValueError("projection fingerprint")
    fingerprint = canonical_fingerprint(_LEGACY_PROJECTION_VERSION, {"source_scan_id": source, "capture_origin": capture, "operation_origin": operation, "operational_witness": operational, "authoritative_relations": rows})
    if not is_v2 and projection["projection_fingerprint"] != fingerprint: raise ValueError("projection fingerprint")
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
