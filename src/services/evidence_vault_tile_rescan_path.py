"""The tile-by-tile path a re-scan of an allow-listed brand publishes through (re-scan rules v2).

It starts from the accepted result: each tile keeps its accepted row unless a durable
fact changes it (``evidence_vault_tile_rescan_apply``), and a healthy re-scan supersedes
the accepted candidate with that v3 candidate instead of reopening review. Core is called
only on the components a gone proof or new owned content names (``would_call``), plus the
Coherencia the planner reopens with them; a component Core fails keeps its accepted rows.
Past its preconditions every other failure raises: nothing is published and the accepted
result stands.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import json
import logging
import time
from typing import Any

from src.config import BRAND3_VAULT_TILE_RESCAN_APPLY_DOMAINS, BRAND3_VAULT_TILE_RESCAN_APPLY_ENABLED
from src.history.report_parser import normalize_domain
from src.services import evidence_vault_evidence_ledger as ledger
from src.services import evidence_vault_sv9_authority_application as application
from src.services import evidence_vault_sv9_authority_evaluation as evaluation_service
from src.services import evidence_vault_tile_rescan_rule as rule
from src.services.evidence_vault_sv9_authoritative_relations import (
    _project_evidence_vault_sv9_evaluation_input_from_facts,
    build_evidence_vault_sv9_authoritative_relation_witness,
    validate_evidence_vault_sv9_evaluation_input,
)
from src.services.evidence_vault_sv9_shared_process import is_core_shared_series_contract
from src.services.evidence_vault_tile_rescan_apply import build_tile_rescan_candidate
from src.sv9 import incremental_evaluation as evaluation
from src.sv9 import incremental_planner as planner
from src.sv9 import judgment_memory as memory

_LOG = logging.getLogger(__name__)
# Rule 11 with Q2 = B: a failed or bot-walled page is a broken capture; a 404 or truncated page is only not checked.
_CAPTURE_GAPS = frozenset({"page_not_captured", "page_obstructed"})
_COMPACT_CANDIDATE = ("id", "source_scan_id", "canonical_plan_fingerprint", "complete_record_fingerprint", "assessment_fingerprint", "score_fingerprint")


class EvidenceVaultTileRescanError(RuntimeError):
    """A tile re-scan failed past its preconditions: nothing was published and the accepted result stands."""

    reason_code = "vault_rescan_publish_failed"

    def __init__(self, step: str, reason: str) -> None:
        super().__init__(f"{step}: {reason}")
        self.step, self.reason = step, reason


class EvidenceVaultTileRescanCaptureError(RuntimeError):
    """The re-scan's capture is broken, or misses a page that holds accepted proof (rule 11)."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def tile_rescan_listed(domain_or_url: str) -> bool:
    """Whether this brand's re-scans take the tile-by-tile path."""

    return BRAND3_VAULT_TILE_RESCAN_APPLY_ENABLED and normalize_domain(domain_or_url) in BRAND3_VAULT_TILE_RESCAN_APPLY_DOMAINS


def run_tile_rescan_path(
    *,
    repository: Any,
    flow: Any,
    domain_or_url: str,
    source_scan_id: str,
    current_series_contract: Mapping[str, Any],
    workspace_slug: str = "b3s",
) -> dict[str, Any] | None:
    """Publish one re-scan tile by tile, or return None to leave it on today's path.

    None means the brand is not listed or has no readable accepted authority, the scan is
    the accepted one or the one its pending review holds, or the judgment series changed.
    The result has the application's shape, with status ``authority_advanced``.
    """

    if not tile_rescan_listed(domain_or_url):
        return None
    started = time.monotonic()
    summary: dict[str, Any] = {"scan_id": source_scan_id, "timings_ms": {}} | dict.fromkeys(
        ("status", "step", "reason_codes", "error", "prior_scan_id", "accepted_score", "score", "candidate_id", "counts", "doubts", "change_signal", "core_plan", "carried_calls", "total_ms")
    )
    try:
        result = _rescan(summary, repository, flow, domain_or_url, source_scan_id, current_series_contract, workspace_slug)
    except EvidenceVaultTileRescanCaptureError as exc:
        _log(summary, started, "failed", [exc.reason_code], exc)
        raise
    except EvidenceVaultTileRescanError as exc:
        _log(summary, started, "failed", [exc.reason], exc)
        raise
    except Exception as exc:
        _log(summary, started, "failed", [type(exc).__name__], exc)
        raise EvidenceVaultTileRescanError(str(summary["step"]), type(exc).__name__) from exc
    if result is None:
        _log(summary, started, "skipped", summary["reason_codes"])
        return None
    _log(summary, started, "applied", [result["status"], *result["reason_codes"]])
    return result


def _rescan(summary: dict[str, Any], repository: Any, flow: Any, domain: str, source: str, series: Mapping[str, Any], workspace: str) -> dict[str, Any] | None:
    with _step(summary, "read"):
        state, authority, details = application._read(repository, domain, workspace)
    skip = _skip(state, authority, details, source, series)
    if skip is None:
        with _step(summary, "facts"):
            facts = repository.load_evidence_ledger_scan_facts(domain, source_scan_id=source, workspace_slug=workspace)
        skip = "no_prior_scan" if facts is None else None
    if skip is not None:
        summary["reason_codes"] = [skip]
        return None
    accepted = authority["accepted_candidate"]
    if facts["accepted"]["candidate_id"] != accepted["id"]:
        raise EvidenceVaultTileRescanError("facts", "accepted_authority_mismatch")
    prior = facts["prior"]
    summary.update(prior_scan_id=prior["scan_id"], accepted_score=accepted["assessment"]["sv9_score"])
    with _step(summary, "health"):
        rows = _healthy_capture_ledger(facts, accepted)
    with _step(summary, "witness"):
        evaluation_input, witness = _rescan_witness(repository, source, workspace)
        reviewed = accepted["authoritative_relation_witness"]
        # The re-scan's witness must descend from the accepted one before the builder protects its relations.
        if witness["operational_witness"] != reviewed["operational_witness"] or [row["tile_id"] for row in witness["authoritative_relations"]] != [row["tile_id"] for row in reviewed["authoritative_relations"]]:
            raise EvidenceVaultTileRescanError("witness", "witness_mismatch")
    # A component the accepted re-scan named but Core never answered is called now: its content is already in the prior capture.
    summary["carried_calls"] = _pending_calls(accepted)
    would_call = rule._registry_order({*_signals(facts, accepted, rows, evaluation_input)["core_plan"]["would_call"], *summary["carried_calls"]})
    called: list[str] = []
    if would_call:
        with _step(summary, "core"):
            called = _core_calls(repository, flow, domain, source, workspace, series, authority, evaluation_input, would_call)
            # Core's verdicts reach the builder and the ledger's shown-to-Core index through this scan's checkpoints.
            facts = repository.load_evidence_ledger_scan_facts(domain, source_scan_id=source, workspace_slug=workspace)
            if facts is None or facts["accepted"]["candidate_id"] != accepted["id"]:
                raise EvidenceVaultTileRescanError("core", "accepted_authority_mismatch")
    current = facts["current"]
    with _step(summary, "ledger"):
        rows = _healthy_capture_ledger(facts, accepted)
        repository.append_evidence_ledger_rows(source, prior_source_scan_id=prior["scan_id"], rows=rows, workspace_slug=workspace)
    with _step(summary, "build"):
        signals = _signals(facts, accepted, rows, evaluation_input)
        summary["core_plan"] = signals["core_plan"]
        # A component Core was asked for and left without a checkpoint keeps its accepted rows (rules 2 and 10).
        failed = rule._registry_order(set(would_call) | set(called))
        failed = [component for component in failed if component not in signals["core_plan"]["actual_calls"]]
        built = build_tile_rescan_candidate(
            accepted_candidate=accepted, ledger_rows=rows, current_judgments=current["judgments"], witness=witness,
            guard={"core_plan": signals["core_plan"], "failed_components": failed},
        )
        summary["change_signal"] = built["change_signal"]
        candidate = built["candidate"]
        if candidate is None:
            raise EvidenceVaultTileRescanError("build", ",".join(built["reason_codes"]) or "unavailable")
    decisions = candidate["tile_rescan"]["decisions"]
    summary.update(
        counts=dict(sorted(Counter(row["decision"] for row in decisions).items())),
        doubts=[row["tile_id"] for row in decisions if rule.DOUBT in row["reason_codes"]],
        score=candidate["assessment"]["sv9_score"],
    )
    with _step(summary, "snapshot"):
        snapshot = _snapshot(flow, accepted, candidate, current) if is_core_shared_series_contract(series) else None
    with _step(summary, "append"):
        stored, inserted = repository.append_evidence_vault_sv9_judgment_candidate(source, candidate, workspace_slug=workspace, shared_analysis_payload=snapshot)
        summary["candidate_id"] = stored["id"]
    with _step(summary, "adopt"):
        bound = {"accepted_candidate_id": accepted["id"], "active_event_id": authority["active_authority_event"]["event_id"], "current_head_event_fingerprint": details["head"]}
        outcome = {"status": "candidate_available", "reason_codes": [] if inserted else ["candidate_already_present"], "accepted_authority": bound, "candidate": {key: stored[key] for key in _COMPACT_CANDIDATE}}
        result = application._apply_candidate(repository, domain, source, workspace, outcome)
    if result["status"] != "authority_advanced":
        raise EvidenceVaultTileRescanError("adopt", result["status"])
    return result


def _signals(facts: Mapping[str, Any], accepted: Mapping[str, Any], rows: list[dict[str, Any]], evaluation_input: Mapping[str, Any]) -> dict[str, Any]:
    prior, current = facts["prior"], facts["current"]
    return rule.rescan_signals(
        brand_domain=facts["domain"], prior_snapshot=prior["snapshot"], prior_rows=prior["evidence_rows"],
        current_snapshot=current["snapshot"], current_rows=current["evidence_rows"], current_evaluations=current["evaluations"],
        prior_judgments=accepted["candidate_tile_judgments"], ledger_rows=rows, hinted_rows=_hinted_rows(evaluation_input),
    )


def _pending_calls(accepted: Mapping[str, Any]) -> list[str]:
    """The components the accepted re-scan left unevaluated, from its ``tile_rescan.guard``.

    The guard is a contract here: the next re-scan reads ``core_plan.would_call``,
    ``core_plan.actual_calls`` and ``failed_components`` to call what Core never answered
    (``would_call`` minus ``actual_calls``, plus ``failed_components``). An accepted
    candidate without a tile re-scan, or with a guard of any other shape, carries nothing.
    """

    tile_rescan = accepted.get("tile_rescan")
    guard = tile_rescan.get("guard") if isinstance(tile_rescan, Mapping) else None
    plan = guard.get("core_plan") if isinstance(guard, Mapping) else None
    if not isinstance(plan, Mapping):
        return []
    named = [plan.get("would_call"), plan.get("actual_calls"), guard.get("failed_components")]
    known = {row["component_key"] for row in rule._COMPONENTS}
    if any(type(value) is not list or not set(value) <= known for value in named):
        return []
    would_call, actual_calls, failed = (set(value) for value in named)
    return rule._registry_order((would_call - actual_calls) | failed)


def _core_calls(repository: Any, flow: Any, domain: str, source: str, workspace: str, series: Mapping[str, Any], authority: Mapping[str, Any], evaluation_input: Mapping[str, Any], would_call: list[str]) -> list[str]:
    """Call Core on ``would_call`` through the incremental planner and checkpoints; return the components planned.

    Every tile of a named component sees the same rows: the current rows the hints route
    to it and the current rows of its accepted supports. A tile whose rows equal its own
    supports is left out, since the planner would send it to review. Coherencia gets its
    own rows, or the planner reopens it on its prior supports. A provider failure only
    leaves components without a checkpoint; any other failure raises.
    """

    prior, sentinels, _ids, _overlay, snapshot = evaluation_service._authority(authority, source)
    context, records = evaluation_service._source(repository, source, evaluation_input, workspace, normalize_domain(domain))
    hinted, by_tile, deltas = _hinted_rows(evaluation_input), {row["tile_id"]: row for row in prior}, []
    for component, tiles in planner._COMPONENT_TILES.items():
        if component not in would_call and not (component == "coherencia" and deltas):
            continue
        pairs = {(row["evidence_ref"], row["evidence_fingerprint"]) for row in hinted if row["component_key"] == component}
        pairs |= {(item["evidence_ref"], item["evidence_fingerprint"]) for row in prior if row["component_key"] == component for item in row["supporting_evidence"]}
        evidence = [{"evidence_ref": ref, "evidence_fingerprint": fingerprint} for ref, fingerprint in sorted(pairs & set(records))]
        if not evidence:
            continue
        for tile in tiles:
            delta = memory.build_tile_evidence_delta_projection(tile_id=tile, component_key=component, disposition="relevant", evidence=evidence, **context)
            if tile not in by_tile or delta["evidence"] != by_tile[tile]["supporting_evidence"]:
                deltas.append(delta)
    if not deltas:
        return []
    plan = planner.build_incremental_plan(prior, deltas, dict(series), prior_component_sentinels=sentinels)
    if plan["review_set"]:
        raise EvidenceVaultTileRescanError("core", "plan_review")
    packets, _bindings = evaluation_service._packets(plan, records, context)
    lookup, persist = evaluation_service._checkpoint_callbacks(repository, source, workspace, evaluation_input, snapshot, plan, flow=flow)
    result = evaluation.execute_incremental_evaluation(plan, packets, flow, lookup_evaluation=lookup, persist_evaluation=persist)
    if result["status"] != "available" and result["reason_code"] != "provider_failure":
        raise EvidenceVaultTileRescanError("core", str(result["reason_code"]))
    return list(plan["component_workset"])


def _skip(state: str, authority: Mapping[str, Any] | None, details: Mapping[str, Any] | None, source: str, series: Mapping[str, Any]) -> str | None:
    if state != "authority":
        return "no_accepted_authority" if state == "absent" else "authority_unreadable"
    # A re-run of the accepted scan keeps the exact-reuse path, and the scan a pending review holds keeps that review.
    if details["candidate"]["source_scan_id"] == source:
        return "accepted_scan"
    if details["review_scan_id"] == source:
        return "review_scan"
    # A new judgment series re-baselines the brand: not a tile-by-tile re-scan.
    if authority["accepted_candidate"]["plan"]["current_series_contract"] != series:
        return "series_change"
    return None


def _healthy_capture_ledger(facts: Mapping[str, Any], accepted: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The evidence ledger of a healthy re-capture; a broken or partial one raises (rule 11)."""

    prior, current = facts["prior"], facts["current"]
    health = {"gate_state": ledger._gate_state(current["snapshot"]), "web_step_status": ledger._web_step_status(current["snapshot"])}
    if ledger._capture_reasons(health):
        raise EvidenceVaultTileRescanCaptureError("vault_rescan_capture_broken")
    rows = ledger.build_evidence_ledger(
        brand_domain=facts["domain"], prior_snapshot=prior["snapshot"], prior_rows=prior["evidence_rows"],
        current_snapshot=current["snapshot"], current_rows=current["evidence_rows"], shown_index=ledger.build_shown_index(current["evaluations"]),
    )["rows"]
    lit = {(item["evidence_ref"], item["evidence_fingerprint"]) for row in accepted["candidate_tile_judgments"] if row["assessment_state"] == rule.OK for item in row["supporting_evidence"]}
    if any(
        row["evidence_class"] == ledger.OWNED_PAGE_CLASS and (row["evidence_ref"], row["evidence_fingerprint"]) in lit and _CAPTURE_GAPS & set(row["reason_codes"])
        for row in rows
    ):
        raise EvidenceVaultTileRescanCaptureError("vault_rescan_capture_partial")
    return rows


def _rescan_witness(repository: Any, source: str, workspace: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The re-scan's evaluation input and authoritative relation witness, built as the evaluation builds a candidate's."""

    # Loaded here rather than through the input projector, which would swallow the cause of a refused load.
    facts = repository.load_evidence_vault_sv9_authoritative_relation_facts(source, workspace_slug=workspace)
    value = _project_evidence_vault_sv9_evaluation_input_from_facts(facts, source_scan_id=source, workspace_slug=workspace)
    if value.get("status") != "available":
        raise EvidenceVaultTileRescanError("witness", f"evaluation_input_{(value.get('reason_codes') or ['unavailable'])[0]}")
    value = validate_evidence_vault_sv9_evaluation_input(value, source_scan_id=source, workspace_slug=workspace)
    # A reviewed relation whose evidence identity changed has no witness yet: the scan fails visibly.
    if value["authority_coverage_loss"] or value["reopen_tile_ids"]:
        raise EvidenceVaultTileRescanError("witness", "reviewed_relation_coverage_loss")
    origin = value["source_identity"]
    projection = {
        "status": "available", "reason_codes": [], "authoritative_relations": value["authoritative_relations"],
        "operational_witness": value["operational_witness"], "projection_fingerprint": value["relation_projection_fingerprint"],
        **{key: value[key] for key in ("current_identity_bindings", "authority_continuity", "authority_coverage_loss", "reopen_tile_ids")},
    }
    witness = build_evidence_vault_sv9_authoritative_relation_witness(
        source_scan_id=source, projection=projection,
        capture_origin={key: origin[key] for key in ("capture_id", "capture_fingerprint")},
        operation_origin={"operation_id": origin["operation_plan_id"], "operation_fingerprint": origin["operation_fingerprint"]},
    )
    return value, witness


def _hinted_rows(evaluation_input: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The current rows the persisted operational hints route to a component."""

    bindings = {row["evidence_record_id"]: row for row in evaluation_input["current_identity_bindings"]}
    return [
        {key: bindings[hint["evidence_record_id"]][key] for key in ("evidence_ref", "evidence_fingerprint")} | {"component_key": hint["component_key"]}
        for hint in evaluation_input["non_authoritative_hints"]
    ]


def _snapshot(flow: Any, accepted: Mapping[str, Any], candidate: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """The shared analysis the candidate publishes: the accepted one again when no tile changed (Q3 = A)."""

    kept, chosen = ({row["tile_id"]: row for row in value["candidate_tile_judgments"]} for value in (accepted, candidate))
    # The builder returns decisions, not sources: a tile is current when its row is not the accepted one.
    sources = {tile: "current" if tile in chosen and chosen[tile] != kept.get(tile) else "accepted" for tile in evaluation._ORDER}
    if "current" not in sources.values():
        return flow.build_unchanged_shared_analysis_payload(assessment=candidate["assessment"])
    fresh = {evaluation._BY_TILE[tile][1] for tile, origin in sources.items() if origin == "current"}
    components = {row["component_key"]: row["component_result"] for row in current["evaluations"] if row["component_key"] in fresh}
    # Without the re-scan's Flow context (no component evaluated or restored), merging would prepare a new one with model calls.
    if getattr(flow, "_candidate", None) is None:
        raise EvidenceVaultTileRescanError("snapshot", "no_flow_context")
    payload, _assessment = flow.build_merged_shared_analysis_payload(assessment=candidate["assessment"], current_components=components, tile_source_map=sources)
    return payload


@contextmanager
def _step(summary: dict[str, Any], name: str) -> Iterator[None]:
    summary["step"], started = name, time.monotonic()
    try:
        yield
    finally:
        summary["timings_ms"][name] = round((time.monotonic() - started) * 1000)


def _log(summary: dict[str, Any], started: float, status: str, reasons: list[str], exc: BaseException | None = None) -> None:
    summary.update(status=status, reason_codes=reasons, error=None if exc is None else _error_chain(exc), total_ms=round((time.monotonic() - started) * 1000))
    # WARNING with the JSON in the message: without a logging config, INFO and extra fields never reach the log.
    _LOG.warning("vault tile rescan apply %s", json.dumps(summary, sort_keys=True, separators=(",", ":"), default=str))


def _error_chain(exc: BaseException) -> str:
    """The exception and its causes, bounded: a repository refusal names its failing check in the cause."""

    parts: list[str] = []
    while exc is not None and len(parts) < 3:
        parts.append(f"{type(exc).__name__}: {exc}")
        exc = exc.__cause__ if exc.__cause__ is not None else (None if exc.__suppress_context__ else exc.__context__)
    return " <- ".join(parts)[:400]
