"""Leased Vault-only executor for frozen incremental operation plans.

It performs semantic work only for planned evidence, persists model output
before any packet write, and can never adopt memory or calculate a score.
"""

from __future__ import annotations

from copy import deepcopy
from threading import Event, Thread
from typing import Any, Mapping, Protocol

from src.services.evidence_memory_identity_v2 import (
    project_evidence_memory_row_identity,
)
from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    TileState,
    build_candidate_packet,
    build_candidate_tile,
    build_incremental_candidate_tiles,
    build_tile_contract_registry,
    canonical_fingerprint,
    validate_candidate_packet,
)
from src.services.evidence_vault_incremental_refresh import (
    EvidenceVaultOperationPlanError,
    require_current_semantic_analysis_contract,
    validate_vault_scan_plan,
)
from src.services.evidence_vault_operational_authority import (
    EvidenceVaultOperationalAdoptionConflictError,
    validate_operational_memory_packet,
)
from src.services.evidence_vault_operational_memory import (
    build_operational_memory_packet,
)
from src.services.scanner_evidence_comparison import (
    canonical_evidence_representatives,
)
from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord
from src.sv9_flow.evidence_labeling_worker import (
    EVIDENCE_LABELING_VERSION,
    is_evidence_record_labelable,
    label_evidence_pack,
)
from src.sv9_flow.evidence_tile_relation_worker import (
    EVIDENCE_TILE_RELATION_LEGACY_PROPOSAL_VERSION,
    EVIDENCE_TILE_RELATION_PROPOSAL_VERSION,
    evidence_tile_relation_call_count,
    propose_evidence_tile_relations,
)


EVIDENCE_VAULT_INCREMENTAL_EXECUTOR_VERSION = (
    "evidence-vault-incremental-executor-v1"
)
EVIDENCE_VAULT_OPERATION_RESULT_VERSION = (
    "evidence-vault-operation-result-v1"
)
_MAX_TILES_PER_EVIDENCE = 24
_MAX_TOTAL_RELATION_PAIRS = 120
_SUPPORTED_RELATION_PROPOSAL_VERSIONS = {
    "evidence-tile-relation-proposal-v2",
    "evidence-tile-relation-proposal-v3",
    EVIDENCE_TILE_RELATION_PROPOSAL_VERSION,
}


class EvidenceVaultIncrementalExecutorError(RuntimeError):
    """A frozen Vault plan cannot execute without crossing its contract."""


class VaultIncrementalExecutorRepository(Protocol):
    def get_capture_operation_plan(
        self,
        source_scan_id: str,
        *,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any] | None: ...

    def claim_capture_operation_plan(
        self,
        source_scan_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 300,
        workspace_slug: str = "b3s",
    ) -> dict[str, Any]: ...

    def mark_capture_operation_running(self, source_scan_id: str, **kwargs: Any) -> dict[str, Any]: ...
    def heartbeat_capture_operation_plan(self, source_scan_id: str, **kwargs: Any) -> dict[str, Any]: ...
    def persist_capture_operation_result(self, source_scan_id: str, **kwargs: Any) -> dict[str, Any]: ...
    def fail_capture_operation_plan(self, source_scan_id: str, **kwargs: Any) -> dict[str, Any]: ...
    def finalize_capture_operation_plan(self, source_scan_id: str, **kwargs: Any) -> dict[str, Any]: ...
    def get_evidence_vault_operational_memory(self, domain_or_url: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def register_evidence_vault_operational_source_packet(self, domain_or_url: str, packet: Mapping[str, Any], **kwargs: Any) -> tuple[dict[str, Any], bool]: ...
    def register_evidence_vault_operational_memory_packet(self, domain_or_url: str, packet: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], bool]: ...


class _LeaseKeeper:
    def __init__(
        self,
        *,
        repository: VaultIncrementalExecutorRepository,
        source_scan_id: str,
        worker_id: str,
        lease_token: str,
        lease_generation: int,
        lease_seconds: int,
        workspace_slug: str,
    ) -> None:
        self._repository = repository
        self._kwargs = {
            "source_scan_id": source_scan_id,
            "worker_id": worker_id,
            "lease_token": lease_token,
            "lease_generation": lease_generation,
            "lease_seconds": lease_seconds,
            "workspace_slug": workspace_slug,
        }
        self._interval = max(0.25, lease_seconds / 3)
        self._stop = Event()
        self._error: BaseException | None = None
        self._thread = Thread(
            target=self._run,
            name=f"vault-lease-{source_scan_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self._interval + 0.5))

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise EvidenceVaultIncrementalExecutorError(
                "operation lease heartbeat failed"
            ) from self._error

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._repository.heartbeat_capture_operation_plan(
                    **self._kwargs
                )
            except BaseException as exc:
                self._error = exc
                self._stop.set()
                return


def execute_vault_operation_plan(
    *,
    repository: VaultIncrementalExecutorRepository,
    source_scan_id: str,
    worker_id: str,
    llm: Any | None,
    workspace_slug: str = "b3s",
    lease_seconds: int = 300,
) -> dict[str, Any]:
    """Claim, execute, persist, materialize and finalize one exact plan."""

    initial_context = repository.get_capture_operation_plan(
        source_scan_id,
        workspace_slug=workspace_slug,
    )
    if initial_context is None:
        raise EvidenceVaultIncrementalExecutorError(
            "capture operation plan does not exist"
        )
    initial_plan = dict(initial_context["plan"])
    validate_vault_scan_plan(initial_plan)

    claim = repository.claim_capture_operation_plan(
        source_scan_id,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        workspace_slug=workspace_slug,
    )
    status = str(claim.get("status") or "")
    if status == "completed":
        return {"execution_status": "completed", "operation": claim, "work_performed": False}
    if status == "superseded":
        return {"execution_status": "superseded", "operation": claim, "work_performed": False}
    if status == "result_persisted":
        return _materialize_and_finalize_result(
            repository=repository,
            source_scan_id=source_scan_id,
            operation=claim,
            workspace_slug=workspace_slug,
        )
    if claim.get("claim_status") == "busy" or claim.get("claimed") is not True:
        execution_status = status if status in {"claimed", "running"} else "busy"
        return {
            "execution_status": execution_status,
            "operation": claim,
            "work_performed": False,
        }
    if status == "running":
        return {"execution_status": "running", "operation": claim, "work_performed": False}

    lease_token = str(claim["lease_token"])
    lease_generation = int(claim["lease_generation"])
    running = repository.mark_capture_operation_running(
        source_scan_id,
        worker_id=worker_id,
        lease_token=lease_token,
        lease_generation=lease_generation,
        lease_seconds=lease_seconds,
        workspace_slug=workspace_slug,
    )
    lease_keeper = _LeaseKeeper(
        repository=repository,
        source_scan_id=source_scan_id,
        worker_id=worker_id,
        lease_token=lease_token,
        lease_generation=lease_generation,
        lease_seconds=lease_seconds,
        workspace_slug=workspace_slug,
    )
    lease_keeper.start()
    try:
        context = repository.get_capture_operation_plan(
            source_scan_id,
            workspace_slug=workspace_slug,
        )
        if context is None:
            raise EvidenceVaultIncrementalExecutorError(
                "claimed operation context disappeared"
            )
        plan = dict(context["plan"])
        validate_vault_scan_plan(plan)
        if plan["operation_plan_fingerprint"] != running[
            "operation_plan_fingerprint"
        ]:
            raise EvidenceVaultIncrementalExecutorError(
                "claimed operation plan identity changed"
            )
        if plan["mode"] == "diagnostic_full":
            raise EvidenceVaultIncrementalExecutorError(
                "diagnostic_full remains owned by the explicit full scanner"
            )
        if plan["operations"]["llm_required"]:
            try:
                require_current_semantic_analysis_contract(plan)
            except EvidenceVaultOperationPlanError as exc:
                raise EvidenceVaultIncrementalExecutorError(
                    "operation semantic analyzer is unavailable"
                ) from exc
        if _is_no_delta_plan(plan):
            result = (
                _material_delta_only_result(context)
                if plan["delta"].get("requires_incremental_analysis") is True
                else _no_delta_result(context)
            )
        else:
            if llm is None and plan["operations"]["llm_required"]:
                raise EvidenceVaultIncrementalExecutorError(
                    "material incremental plan requires an LLM"
                )
            current = repository.get_evidence_vault_operational_memory(
                context["brand_identity"],
                workspace_slug=workspace_slug,
            )
            if (
                (current["canonical_memory_version"] if current else None)
                != plan["canonical_memory_version"]
            ):
                raise EvidenceVaultIncrementalExecutorError(
                    "canonical parent changed after plan claim"
                )
            result = _build_candidate_result(
                context=context,
                current_memory=current,
                llm=llm,
            )
        validate_vault_operation_result(result)
        lease_keeper.stop()
        lease_keeper.raise_if_failed()
        repository.heartbeat_capture_operation_plan(
            source_scan_id,
            worker_id=worker_id,
            lease_token=lease_token,
            lease_generation=lease_generation,
            lease_seconds=lease_seconds,
            workspace_slug=workspace_slug,
        )
        persisted = repository.persist_capture_operation_result(
            source_scan_id,
            worker_id=worker_id,
            lease_token=lease_token,
            lease_generation=lease_generation,
            result_payload=result,
            workspace_slug=workspace_slug,
        )
    except Exception as exc:
        lease_keeper.stop()
        try:
            repository.fail_capture_operation_plan(
                source_scan_id,
                worker_id=worker_id,
                lease_token=lease_token,
                lease_generation=lease_generation,
                error=f"{type(exc).__name__}: {exc}",
                workspace_slug=workspace_slug,
            )
        except Exception:
            pass
        raise

    if persisted["status"] == "superseded":
        return {
            "execution_status": "superseded",
            "operation": persisted,
            "work_performed": True,
        }
    return _materialize_and_finalize_result(
        repository=repository,
        source_scan_id=source_scan_id,
        operation=persisted,
        workspace_slug=workspace_slug,
        work_performed=True,
    )


def validate_vault_operation_result(result: Mapping[str, Any]) -> None:
    if not isinstance(result, Mapping):
        raise EvidenceVaultIncrementalExecutorError("operation result must be an object")
    common = {
        "schema_version",
        "output_kind",
        "operation_plan_fingerprint",
        "observation_hash",
        "canonical_memory_version",
        "authority",
        "authority_scope",
        "production_runtime_effect",
        "scanner_runtime_effect",
    }
    if (
        result.get("schema_version") != EVIDENCE_VAULT_OPERATION_RESULT_VERSION
        or result.get("authority") is not False
        or result.get("authority_scope") != "b3s-vault"
        or result.get("production_runtime_effect") is not False
        or result.get("scanner_runtime_effect") is not False
    ):
        raise EvidenceVaultIncrementalExecutorError("operation result authority is invalid")
    for field in ("operation_plan_fingerprint", "observation_hash"):
        _sha256(result.get(field), field=field)
    if result.get("canonical_memory_version") is not None:
        _sha256(result["canonical_memory_version"], field="canonical_memory_version")
    output_kind = result.get("output_kind")
    if output_kind in {"no_delta", "material_delta_only"}:
        expected = common | {"delta_fingerprint", "delta_summary"}
        if set(result) != expected:
            raise EvidenceVaultIncrementalExecutorError(
                f"{output_kind} result fields mismatch"
            )
        if result.get("delta_fingerprint") is not None:
            _sha256(result["delta_fingerprint"], field="delta_fingerprint")
        if not isinstance(result.get("delta_summary"), Mapping):
            raise EvidenceVaultIncrementalExecutorError("delta summary is invalid")
        return
    if output_kind != "candidate_overlay":
        raise EvidenceVaultIncrementalExecutorError("unsupported operation output kind")
    expected = common | {
        "selected_evidence_fingerprints",
        "selected_evidence_ids",
        "selected_document_ids",
        "evidence_work_dispositions",
        "semantic_labels",
        "shortlisted_tile_ids",
        "tile_shortlists",
        "shortlist_truncations",
        "labeling_debug",
        "relation_proposal",
        "relation_proposal_call_count",
        "basis_relations",
        "source_candidate_packet_fingerprint",
        "source_candidate_packet",
        "candidate_packet_fingerprint",
        "operational_candidate_packet",
    }
    if set(result) != expected:
        raise EvidenceVaultIncrementalExecutorError("candidate result fields mismatch")
    for field in (
        "source_candidate_packet_fingerprint",
        "candidate_packet_fingerprint",
    ):
        _sha256(result.get(field), field=field)
    for field in (
        "selected_evidence_fingerprints",
        "selected_evidence_ids",
        "selected_document_ids",
    ):
        rows = result.get(field)
        if not isinstance(rows, list) or rows != sorted(set(rows)):
            raise EvidenceVaultIncrementalExecutorError(f"{field} is invalid")
        for value in rows:
            _sha256(value, field=field)
    dispositions = result.get("evidence_work_dispositions")
    if (
        not isinstance(dispositions, Mapping)
        or set(dispositions) != set(result["selected_evidence_fingerprints"])
        or not set(dispositions.values()).issubset(
            {
                "semantic_candidate",
                "ineligible_label_type",
                "non_material_identity",
                "deterministic_identity_mismatch",
            }
        )
    ):
        raise EvidenceVaultIncrementalExecutorError(
            "evidence work dispositions are invalid"
        )
    semantic_fingerprints = {
        fingerprint
        for fingerprint, disposition in dispositions.items()
        if disposition == "semantic_candidate"
    }
    semantic_labels = result.get("semantic_labels")
    component_keys = {
        str(row["component_key"])
        for row in build_tile_contract_registry()["tiles"]
    }
    if (
        not isinstance(semantic_labels, Mapping)
        or set(semantic_labels) != semantic_fingerprints
    ):
        raise EvidenceVaultIncrementalExecutorError(
            "semantic label audit is invalid"
        )
    for labels in semantic_labels.values():
        if (
            not isinstance(labels, Mapping)
            or set(labels)
            != {
                "relevant_blocks",
                "stance",
                "identity_match_llm",
                "specificity",
            }
            or not isinstance(labels["relevant_blocks"], list)
            or labels["relevant_blocks"]
            != sorted(set(labels["relevant_blocks"]))
            or not set(labels["relevant_blocks"]).issubset(component_keys)
            or labels["stance"]
            not in {"supports", "contradicts", "neutral"}
            or labels["identity_match_llm"]
            not in {"domain", "brand_name", "none", "unverified"}
            or labels["specificity"]
            not in {"explicit", "implied", "incidental"}
        ):
            raise EvidenceVaultIncrementalExecutorError(
                "semantic label values are invalid"
            )
    tile_ids = result.get("shortlisted_tile_ids")
    registry_ids = {str(row["tile_id"]) for row in build_tile_contract_registry()["tiles"]}
    if not isinstance(tile_ids, list) or tile_ids != sorted(set(tile_ids)) or not set(tile_ids).issubset(registry_ids):
        raise EvidenceVaultIncrementalExecutorError("shortlisted tile ids are invalid")
    tile_shortlists = result.get("tile_shortlists")
    if not isinstance(tile_shortlists, Mapping) or set(tile_shortlists) != semantic_fingerprints:
        raise EvidenceVaultIncrementalExecutorError(
            "tile shortlist evidence workset is invalid"
        )
    shortlist_union: set[str] = set()
    for fingerprint, rows in tile_shortlists.items():
        _sha256(fingerprint, field="tile shortlist evidence fingerprint")
        if not isinstance(rows, list) or rows != sorted(set(rows)):
            raise EvidenceVaultIncrementalExecutorError("tile shortlist is invalid")
        if len(rows) > _MAX_TILES_PER_EVIDENCE or not set(rows).issubset(registry_ids):
            raise EvidenceVaultIncrementalExecutorError("tile shortlist exceeds bounds")
        shortlist_union.update(rows)
    truncations = result.get("shortlist_truncations")
    if not isinstance(truncations, Mapping) or not set(truncations).issubset(
        semantic_fingerprints
    ):
        raise EvidenceVaultIncrementalExecutorError(
            "shortlist truncation audit is invalid"
        )
    for fingerprint, audit in truncations.items():
        selected_rows = tile_shortlists[fingerprint]
        if not isinstance(audit, Mapping) or set(audit) != {
            "eligible_count",
            "selected_count",
            "omitted_tile_ids",
        }:
            raise EvidenceVaultIncrementalExecutorError(
                "shortlist truncation fields are invalid"
            )
        omitted = audit["omitted_tile_ids"]
        if (
            not isinstance(omitted, list)
            or not omitted
            or omitted != sorted(set(omitted))
            or not set(omitted).issubset(registry_ids)
            or set(omitted).intersection(selected_rows)
            or audit["selected_count"] != len(selected_rows)
            or audit["selected_count"] != _MAX_TILES_PER_EVIDENCE
            or audit["eligible_count"] != len(selected_rows) + len(omitted)
        ):
            raise EvidenceVaultIncrementalExecutorError(
                "shortlist truncation audit is inconsistent"
            )
    minimum_call_count = _relation_chunk_count(tile_shortlists)
    relation_call_count = result.get("relation_proposal_call_count")
    proposal = result.get("relation_proposal")
    legacy_proposal = (
        isinstance(proposal, Mapping)
        and proposal.get("schema_version")
        == EVIDENCE_TILE_RELATION_LEGACY_PROPOSAL_VERSION
    )
    if (
        sorted(shortlist_union) != tile_ids
        or not isinstance(relation_call_count, int)
        or isinstance(relation_call_count, bool)
        or relation_call_count < minimum_call_count
        or (legacy_proposal and relation_call_count != minimum_call_count)
        or relation_call_count > 1_000_000
    ):
        raise EvidenceVaultIncrementalExecutorError("tile shortlist union is invalid")
    discarded_reasons = {
        "fields_mismatch",
        "field_type_invalid",
        "evidence_outside_plan",
        "tile_outside_shortlist",
        "polarity_invalid",
        "quote_not_literal",
        "rationale_invalid",
        "duplicate_relation",
    }
    if (
        not isinstance(proposal, Mapping)
        or proposal.get("schema_version")
        not in _SUPPORTED_RELATION_PROPOSAL_VERSIONS
        or not isinstance(proposal.get("relations"), list)
        or not isinstance(proposal.get("discarded_relations"), list)
        or len(proposal["relations"]) + len(proposal["discarded_relations"])
        > relation_call_count * _MAX_TOTAL_RELATION_PAIRS
    ):
        raise EvidenceVaultIncrementalExecutorError(
            "relation proposal audit is invalid"
        )
    for relation in proposal["relations"]:
        quote = (
            str(relation.get("literal_quote") or "").strip()
            if isinstance(relation, Mapping)
            else ""
        )
        if not 8 <= len(quote) <= 320:
            raise EvidenceVaultIncrementalExecutorError(
                "relation proposal crosses durable evidence bounds"
            )
    for discarded in proposal["discarded_relations"]:
        if (
            not isinstance(discarded, Mapping)
            or set(discarded)
            != {"reason", "submitted_relation_fingerprint"}
            or discarded.get("reason") not in discarded_reasons
        ):
            raise EvidenceVaultIncrementalExecutorError(
                "discarded relation audit is invalid"
            )
        _sha256(
            discarded.get("submitted_relation_fingerprint"),
            field="discarded relation fingerprint",
        )
    source = result.get("source_candidate_packet")
    operational = result.get("operational_candidate_packet")
    if not isinstance(source, Mapping) or not isinstance(operational, Mapping):
        raise EvidenceVaultIncrementalExecutorError("candidate packets are required")
    validate_candidate_packet(dict(source))
    if source.get("candidate_packet_fingerprint") != result[
        "source_candidate_packet_fingerprint"
    ]:
        raise EvidenceVaultIncrementalExecutorError("source packet fingerprint mismatch")
    if operational.get("candidate_packet_fingerprint") != result[
        "candidate_packet_fingerprint"
    ]:
        raise EvidenceVaultIncrementalExecutorError("operational packet fingerprint mismatch")
    try:
        validate_operational_memory_packet(dict(operational))
    except Exception as exc:
        raise EvidenceVaultIncrementalExecutorError(
            "operational candidate packet is invalid"
        ) from exc
    if operational.get("has_accepted_change") is not False:
        raise EvidenceVaultIncrementalExecutorError(
            "executor result cannot contain an accepted memory change"
        )
    if not isinstance(result.get("basis_relations"), list):
        raise EvidenceVaultIncrementalExecutorError("basis relations are invalid")


def _build_candidate_result(
    *,
    context: Mapping[str, Any],
    current_memory: Mapping[str, Any] | None,
    llm: Any,
) -> dict[str, Any]:
    plan = dict(context["plan"])
    selected = _selected_evidence_rows(
        context["evidence_records"],
        subject_url=str(plan["subject_url"]),
        fingerprints=plan["operations"]["classify_evidence_fingerprints"],
    )
    pack = _evidence_pack(
        selected_rows=context["evidence_records"],
        brand_name=str(plan["brand_identity"]),
        url=str(plan["subject_url"]),
    )
    records_by_ref = {record.ref: record for record in pack.evidence}
    labelable = [
        row
        for row in selected
        if is_evidence_record_labelable(
            records_by_ref[row["row"]["ref"]]
        )
    ]
    if labelable:
        labeling_debug = label_evidence_pack(
            pack,
            llm=llm,
            max_records=len(labelable),
            selected_refs=[row["row"]["ref"] for row in labelable],
        )
        if (
            labeling_debug.get("status") != "labeled"
            or labeling_debug.get("records_labeled") != len(labelable)
        ):
            raise EvidenceVaultIncrementalExecutorError(
                "selected evidence labeling did not cover the eligible workset: "
                f"{labeling_debug.get('reason')}"
            )
    else:
        labeling_debug = {
            "version": EVIDENCE_LABELING_VERSION,
            "status": "not_required",
            "reason": "no_labelable_records",
            "records_considered": 0,
            "records_labeled": 0,
            "artifact_cache_hits": 0,
            "artifact_cache_misses": 0,
            "provider_records": 0,
            "provider_call_count": 0,
            "provider_record_refs": [],
            "semantic_passage_count": 0,
            "semantic_batch_count": 0,
            "identity_divergences": [],
        }
    enriched: list[dict[str, Any]] = []
    identities: dict[str, dict[str, Any]] = {}
    dispositions: dict[str, str] = {
        row["fingerprint"]: "ineligible_label_type" for row in selected
    }
    for selected_row in labelable:
        record = records_by_ref[selected_row["row"]["ref"]]
        identity = project_evidence_memory_row_identity(
            record.to_dict(),
            brand_domain=str(plan["brand_identity"]),
        )
        fingerprint = selected_row["fingerprint"]
        if identity is None:
            dispositions[fingerprint] = "non_material_identity"
            continue
        deterministic_identity_match = str(
            record.metadata.get("identity_match") or ""
        ).strip().lower()
        if deterministic_identity_match == "none":
            dispositions[fingerprint] = "deterministic_identity_mismatch"
            continue
        dispositions[fingerprint] = "semantic_candidate"
        identities[fingerprint] = identity
        enriched.append(
            {
                "evidence_fingerprint": fingerprint,
                **record.to_dict(),
                "labels": {
                    "relevant_blocks": sorted(
                        {
                            str(value)
                            for value in record.metadata.get(
                                "relevant_blocks", []
                            )
                        }
                    ),
                    "stance": str(record.metadata.get("stance") or "neutral"),
                    "identity_match_llm": str(
                        record.metadata.get("identity_match_llm")
                        or "unverified"
                    ),
                    "specificity": str(
                        record.metadata.get("specificity") or "incidental"
                    ),
                },
            }
        )
    shortlists, shortlist_truncations = derive_vault_tile_shortlists(
        enriched,
        forced_tile_ids=plan["operations"]["reevaluate_tile_ids"],
    )
    proposal, relation_call_count = _propose_relations_bounded(
        evidence_rows=enriched,
        tile_shortlists=shortlists,
        llm=llm,
    )
    basis_relations = _basis_relations(
        proposal["relations"],
        identities=identities,
        operation_plan_fingerprint=plan["operation_plan_fingerprint"],
    )
    source_packet = _source_candidate_packet(
        plan=plan,
        current_memory=current_memory,
        basis_relations=basis_relations,
    )
    operational_packet = build_operational_memory_packet(
        brand_identity=str(plan["brand_identity"]),
        source_candidate_packet_fingerprint=source_packet[
            "candidate_packet_fingerprint"
        ],
        aggregation_policy_fingerprint=source_packet["manifest"][
            "aggregation_policy_fingerprint"
        ],
        candidate_tiles=source_packet["candidate_tiles"],
        current_accepted_tiles=(
            current_memory["content"]["accepted_tiles"]
            if current_memory is not None
            else []
        ),
        parent_canonical_memory_version=plan["canonical_memory_version"],
        current_pending_reassessments=(
            current_memory["content"].get("pending_reassessments") or []
            if current_memory is not None
            else []
        ),
    )
    if operational_packet["has_accepted_change"] is not False:
        raise EvidenceVaultIncrementalExecutorError(
            "model proposal unexpectedly changed accepted memory"
        )
    return {
        "schema_version": EVIDENCE_VAULT_OPERATION_RESULT_VERSION,
        "output_kind": "candidate_overlay",
        "operation_plan_fingerprint": plan["operation_plan_fingerprint"],
        "observation_hash": context["observation_hash"],
        "canonical_memory_version": plan["canonical_memory_version"],
        "selected_evidence_fingerprints": sorted(
            row["fingerprint"] for row in selected
        ),
        "selected_evidence_ids": sorted(
            {identity["evidence_id"] for identity in identities.values()}
        ),
        "selected_document_ids": sorted(
            {identity["document_id"] for identity in identities.values()}
        ),
        "evidence_work_dispositions": dict(sorted(dispositions.items())),
        "semantic_labels": {
            row["evidence_fingerprint"]: deepcopy(row["labels"])
            for row in sorted(
                enriched,
                key=lambda value: value["evidence_fingerprint"],
            )
        },
        "tile_shortlists": {
            fingerprint: [row["tile_id"] for row in rows]
            for fingerprint, rows in sorted(shortlists.items())
        },
        "shortlist_truncations": shortlist_truncations,
        "shortlisted_tile_ids": sorted(
            {row["tile_id"] for rows in shortlists.values() for row in rows}
        ),
        "labeling_debug": labeling_debug,
        "relation_proposal": proposal,
        "relation_proposal_call_count": relation_call_count,
        "basis_relations": basis_relations,
        "source_candidate_packet_fingerprint": source_packet[
            "candidate_packet_fingerprint"
        ],
        "source_candidate_packet": source_packet,
        "candidate_packet_fingerprint": operational_packet[
            "candidate_packet_fingerprint"
        ],
        "operational_candidate_packet": operational_packet,
        "authority": False,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }


def _source_candidate_packet(
    *,
    plan: Mapping[str, Any],
    current_memory: Mapping[str, Any] | None,
    basis_relations: list[dict[str, Any]],
) -> dict[str, Any]:
    registry = build_tile_contract_registry()["tiles"]
    current_by_id = {
        str(row["tile_id"]): row
        for row in (
            current_memory["content"]["accepted_tiles"]
            if current_memory is not None
            else []
        )
    }
    previous = [
        build_candidate_tile(
            tile_id=str(contract["tile_id"]),
            basis=(current_by_id.get(str(contract["tile_id"])) or {}).get("basis") or [],
            coverage_refs=(current_by_id.get(str(contract["tile_id"])) or {}).get("coverage_refs") or [],
            unresolved_refs=(current_by_id.get(str(contract["tile_id"])) or {}).get("unresolved_refs") or [],
        )
        for contract in registry
    ]
    new_by_tile: dict[str, list[dict[str, Any]]] = {}
    for relation in basis_relations:
        new_by_tile.setdefault(str(relation["tile_id"]), []).append(
            {key: value for key, value in relation.items() if key != "tile_id"}
        )
    if current_memory is None:
        candidates = [
            build_candidate_tile(
                tile_id=str(contract["tile_id"]),
                basis=new_by_tile.get(str(contract["tile_id"]), []),
            )
            for contract in registry
        ]
    else:
        updates: list[dict[str, Any]] = []
        previous_by_id = {str(row["tile_id"]): row for row in previous}
        for tile_id, relations in sorted(new_by_tile.items()):
            prior = previous_by_id[tile_id]
            combined = sorted(
                [*prior["basis"], *relations],
                key=lambda row: str(row["relation_id"]),
            )
            probe = build_candidate_tile(
                tile_id=tile_id,
                basis=combined,
                unresolved_refs=[
                    f"operation:{plan['operation_plan_fingerprint']}:{tile_id}"
                ],
            )
            previous_state = TileState(prior["candidate_state"])
            candidate_state = TileState(probe["candidate_state"])
            if candidate_state is TileState.CONTRADICTION:
                delta_kind = "contradiction"
            elif candidate_state is previous_state:
                delta_kind = "strengthened"
            else:
                delta_kind = "candidate_update"
            updates.append(
                {
                    "tile_id": tile_id,
                    "delta_kind": delta_kind,
                    "basis": combined,
                    "unresolved_refs": (
                        [f"operation:{plan['operation_plan_fingerprint']}:{tile_id}"]
                        if candidate_state is TileState.CONTRADICTION
                        else []
                    ),
                }
            )
        candidates = build_incremental_candidate_tiles(
            previous_candidate_tiles=previous,
            tile_updates=updates,
        )
    unresolved = [
        {
            "unresolved_id": ref,
            "kind": "contradiction",
            "blocking": True,
            "details": {"source": "vault_incremental_executor"},
        }
        for candidate in candidates
        for ref in candidate.get("unresolved_refs") or []
        if candidate["candidate_state"] == TileState.CONTRADICTION.value
    ]
    identity = {
        "operation_plan_fingerprint": plan["operation_plan_fingerprint"],
        "parent_canonical_memory_version": plan["canonical_memory_version"],
        "basis_relations": basis_relations,
    }
    return build_candidate_packet(
        brand_identity=str(plan["brand_identity"]),
        parent_canonical_memory_version=plan["canonical_memory_version"],
        candidate_memory_version=canonical_fingerprint(
            "evidence-vault-executor-candidate-memory-v1", identity
        ),
        accepted_memory_candidate_version=canonical_fingerprint(
            "evidence-vault-executor-accepted-memory-v1",
            current_memory["content"] if current_memory is not None else {},
        ),
        reviewed_memory_candidate_version=canonical_fingerprint(
            "evidence-vault-executor-reviewed-memory-v1",
            {
                "accepted_tiles": (
                    current_memory["content"]["accepted_tiles"]
                    if current_memory is not None
                    else []
                )
            },
        ),
        review_packet_set_fingerprint=canonical_fingerprint(
            "evidence-vault-executor-review-packet-set-v1", []
        ),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        coverage_summary=dict(plan["delta"].get("summary") or {}),
        unresolved_items=unresolved,
    )


def _basis_relations(
    proposals: list[Mapping[str, Any]],
    *,
    identities: Mapping[str, Mapping[str, Any]],
    operation_plan_fingerprint: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for proposal in proposals:
        fingerprint = str(proposal["evidence_fingerprint"])
        identity = identities.get(fingerprint)
        if identity is None:
            raise EvidenceVaultIncrementalExecutorError(
                "relation proposal has no durable evidence identity"
            )
        unsigned = {
            "operation_plan_fingerprint": operation_plan_fingerprint,
            "evidence_id": identity["evidence_id"],
            "source_identity_id": identity["document_id"],
            "tile_id": proposal["tile_id"],
            "polarity": proposal["polarity"],
            "literal_quote": proposal["literal_quote"],
        }
        rows.append(
            {
                "tile_id": proposal["tile_id"],
                "relation_id": canonical_fingerprint(
                    "evidence-vault-model-relation-v1", unsigned
                ),
                "evidence_id": identity["evidence_id"],
                "source_identity_id": identity["document_id"],
                "claim_id": None,
                "polarity": proposal["polarity"],
                "review_status": "unreviewed",
                "decision_event_id": None,
                "absence_test_contract_id": None,
                "coverage_assessment_id": None,
                "coverage_status": None,
                "tested_scope": None,
                "observed_result": None,
            }
        )
    return sorted(rows, key=lambda row: (row["tile_id"], row["relation_id"]))



def _propose_relations_bounded(
    *,
    evidence_rows: list[Mapping[str, Any]],
    tile_shortlists: Mapping[str, list[Mapping[str, Any]]],
    llm: Any,
) -> tuple[dict[str, Any], int]:
    total_pairs = sum(len(rows) for rows in tile_shortlists.values())
    if total_pairs == 0:
        return {
            "schema_version": EVIDENCE_TILE_RELATION_PROPOSAL_VERSION,
            "relations": [],
            "discarded_relations": [],
        }, 0
    proposal = propose_evidence_tile_relations(
        evidence_rows=evidence_rows, tile_shortlists=tile_shortlists, llm=llm
    )
    if proposal.get("schema_version") != EVIDENCE_TILE_RELATION_PROPOSAL_VERSION:
        raise EvidenceVaultIncrementalExecutorError(
            "relation proposer returned a stale proposal contract"
        )
    return proposal, evidence_tile_relation_call_count(
        evidence_rows=evidence_rows, tile_shortlists=tile_shortlists
    )


def _relation_chunk_fingerprints(
    tile_shortlists: Mapping[str, list[Any]],
) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    current_pairs = 0
    for fingerprint in sorted(tile_shortlists):
        pair_count = len(tile_shortlists[fingerprint])
        if pair_count == 0:
            continue
        if current and current_pairs + pair_count > _MAX_TOTAL_RELATION_PAIRS:
            chunks.append(current)
            current = []
            current_pairs = 0
        current.append(fingerprint)
        current_pairs += pair_count
    if current:
        chunks.append(current)
    return chunks


def _relation_chunk_count(
    tile_shortlists: Mapping[str, list[Any]],
) -> int:
    return len(_relation_chunk_fingerprints(tile_shortlists))


def derive_vault_tile_shortlists(
    evidence_rows: list[Mapping[str, Any]],
    *,
    forced_tile_ids: list[str],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
]:
    registry = build_tile_contract_registry()["tiles"]
    by_component: dict[str, list[dict[str, Any]]] = {}
    by_id = {str(row["tile_id"]): row for row in registry}
    for row in registry:
        by_component.setdefault(str(row["component_key"]), []).append(row)
    forced = {str(tile_id) for tile_id in forced_tile_ids}
    if len(forced) == len(by_id) or len(forced) > _MAX_TILES_PER_EVIDENCE:
        # First-lighting of many unlit tiles, like baseline, cannot pair every
        # canonical tile onto one evidence row. Classifier labels still choose
        # the workset; policy later adopts never-lit matches.
        forced = set()
    if not forced.issubset(by_id):
        raise EvidenceVaultIncrementalExecutorError("plan has an unknown forced tile")
    result: dict[str, list[dict[str, Any]]] = {}
    truncations: dict[str, dict[str, Any]] = {}
    for evidence in evidence_rows:
        fingerprint = str(evidence["evidence_fingerprint"])
        labels = dict(evidence.get("labels") or {})
        relevant = {
            str(block)
            for block in labels.get("relevant_blocks") or []
            if str(block) in by_component
        }
        eligible = set(forced)
        for block in relevant:
            eligible.update(str(row["tile_id"]) for row in by_component[block])
        if labels.get("identity_match_llm") == "none":
            eligible = set()
        forced_selected = sorted(eligible & forced)
        remaining = sorted(eligible - forced)
        selected = (
            forced_selected
            + remaining[: _MAX_TILES_PER_EVIDENCE - len(forced_selected)]
        )
        omitted = sorted(eligible - set(selected))
        if omitted:
            truncations[fingerprint] = {
                "eligible_count": len(eligible),
                "selected_count": len(selected),
                "omitted_tile_ids": omitted,
            }
        result[fingerprint] = [by_id[tile_id] for tile_id in selected]
    return result, dict(sorted(truncations.items()))


def _selected_evidence_rows(
    rows: list[Mapping[str, Any]],
    *,
    subject_url: str,
    fingerprints: list[str],
) -> list[dict[str, Any]]:
    selected_ids = set(fingerprints)
    representatives = canonical_evidence_representatives(
        rows,
        subject_url=subject_url,
    )
    missing = sorted(selected_ids - set(representatives))
    if missing:
        raise EvidenceVaultIncrementalExecutorError(
            "planned evidence is absent from the exact capture: " + ", ".join(missing)
        )
    return [
        {"fingerprint": fingerprint, "row": deepcopy(representatives[fingerprint])}
        for fingerprint in sorted(selected_ids)
    ]


def _evidence_pack(
    *,
    selected_rows: list[Mapping[str, Any]],
    brand_name: str,
    url: str,
) -> BrandEvidencePack:
    records = [
        EvidenceRecord(
            ref=str(row.get("ref") or ""),
            source=str(row.get("source") or "unknown"),
            evidence_type=str(row.get("evidence_type") or "unknown"),
            content=str(row.get("content") or ""),
            url=str(row.get("url") or "") or None,
            confidence=str(row.get("confidence") or "medium"),
            metadata=deepcopy(dict(row.get("metadata") or {})),
        )
        for row in selected_rows
    ]
    return BrandEvidencePack(
        brand_name=brand_name,
        url=url,
        evidence=records,
    )


def _no_delta_result(context: Mapping[str, Any]) -> dict[str, Any]:
    return _delta_only_result(context, output_kind="no_delta")


def _material_delta_only_result(context: Mapping[str, Any]) -> dict[str, Any]:
    return _delta_only_result(context, output_kind="material_delta_only")


def _delta_only_result(
    context: Mapping[str, Any],
    *,
    output_kind: str,
) -> dict[str, Any]:
    plan = context["plan"]
    delta = plan["delta"]
    return {
        "schema_version": EVIDENCE_VAULT_OPERATION_RESULT_VERSION,
        "output_kind": output_kind,
        "operation_plan_fingerprint": plan["operation_plan_fingerprint"],
        "observation_hash": context["observation_hash"],
        "canonical_memory_version": plan["canonical_memory_version"],
        "delta_fingerprint": delta.get("delta_fingerprint"),
        "delta_summary": dict(delta.get("summary") or {}),
        "authority": False,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }


def _is_no_delta_plan(plan: Mapping[str, Any]) -> bool:
    operations = plan["operations"]
    return not (
        operations["llm_required"]
        or operations["create_candidate_packet"]
        or operations["create_diagnostic_report"]
    )


def _materialize_and_finalize_result(
    *,
    repository: VaultIncrementalExecutorRepository,
    source_scan_id: str,
    operation: Mapping[str, Any],
    workspace_slug: str,
    work_performed: bool = False,
) -> dict[str, Any]:
    result = operation.get("result_payload")
    if not isinstance(result, Mapping):
        raise EvidenceVaultIncrementalExecutorError(
            "persisted operation has no reconstructable result"
        )
    validate_vault_operation_result(result)
    latest_context = repository.get_capture_operation_plan(
        source_scan_id,
        workspace_slug=workspace_slug,
    )
    if latest_context is None:
        raise EvidenceVaultIncrementalExecutorError(
            "persisted operation context disappeared"
        )
    latest_plan = dict(latest_context["plan"])
    validate_vault_scan_plan(latest_plan)
    candidate_fingerprint: str | None = (
        str(result["candidate_packet_fingerprint"])
        if result["output_kind"] == "candidate_overlay"
        else None
    )
    if result["output_kind"] == "candidate_overlay":
        try:
            repository.register_evidence_vault_operational_source_packet(
                str(
                    result["source_candidate_packet"]["manifest"][
                        "brand_identity"
                    ]
                ),
                result["source_candidate_packet"],
                source_scan_id=source_scan_id,
                operation_plan_fingerprint=result[
                    "operation_plan_fingerprint"
                ],
                workspace_slug=workspace_slug,
            )
            latest = repository.get_capture_operation_plan(
                source_scan_id,
                workspace_slug=workspace_slug,
            )
            if latest is not None and latest.get("status") == "completed":
                return {
                    "execution_status": "completed",
                    "operation": latest,
                    "work_performed": work_performed,
                }
            repository.register_evidence_vault_operational_memory_packet(
                str(
                    result["source_candidate_packet"]["manifest"][
                        "brand_identity"
                    ]
                ),
                dict(result["operational_candidate_packet"]),
                source_scan_id=source_scan_id,
                operation_plan_fingerprint=result[
                    "operation_plan_fingerprint"
                ],
                workspace_slug=workspace_slug,
            )
        except EvidenceVaultOperationalAdoptionConflictError:
            terminal = repository.finalize_capture_operation_plan(
                source_scan_id,
                operation_plan_fingerprint=result[
                    "operation_plan_fingerprint"
                ],
                result_fingerprint=str(operation["result_fingerprint"]),
                candidate_packet_fingerprint=candidate_fingerprint,
                workspace_slug=workspace_slug,
            )
            if terminal["status"] == "superseded":
                return {
                    "execution_status": "superseded",
                    "operation": terminal,
                    "work_performed": work_performed,
                }
            raise
    finalized = repository.finalize_capture_operation_plan(
        source_scan_id,
        operation_plan_fingerprint=result["operation_plan_fingerprint"],
        result_fingerprint=str(operation["result_fingerprint"]),
        candidate_packet_fingerprint=candidate_fingerprint,
        workspace_slug=workspace_slug,
    )
    return {
        "execution_status": str(finalized["status"]),
        "operation": finalized,
        "work_performed": work_performed,
    }


def _sha256(value: Any, *, field: str) -> str:
    text = str(value or "")
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise EvidenceVaultIncrementalExecutorError(f"{field} must be a SHA-256")
    return text


__all__ = [
    "EVIDENCE_VAULT_INCREMENTAL_EXECUTOR_VERSION",
    "EVIDENCE_VAULT_OPERATION_RESULT_VERSION",
    "EvidenceVaultIncrementalExecutorError",
    "VaultIncrementalExecutorRepository",
    "derive_vault_tile_shortlists",
    "execute_vault_operation_plan",
    "validate_vault_operation_result",
]
