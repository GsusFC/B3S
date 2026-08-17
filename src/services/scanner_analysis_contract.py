"""Stable identity for the interpretation and scoring contract of a report."""

from __future__ import annotations

import hashlib
import json
from typing import Any


ANALYSIS_CONTRACT_VERSION = "scanner-analysis-contract-v1"


def analysis_contract_from_report(report: dict[str, Any]) -> dict[str, Any]:
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    flow = raw.get("flow") if isinstance(raw.get("flow"), dict) else {}
    candidate = (
        flow.get("candidate")
        if isinstance(flow.get("candidate"), dict)
        else {}
    )
    debug = (
        flow.get("interpretation_debug")
        if isinstance(flow.get("interpretation_debug"), dict)
        else {}
    )
    labeling = (
        debug.get("evidence_labeling")
        if isinstance(debug.get("evidence_labeling"), dict)
        else {}
    )
    semantic_analysis_contract = (
        debug.get("semantic_analysis_contract")
        if isinstance(debug.get("semantic_analysis_contract"), dict)
        else {}
    )
    sv9 = raw.get("sv9") if isinstance(raw.get("sv9"), dict) else {}
    result = (
        sv9.get("result") if isinstance(sv9.get("result"), dict) else {}
    )
    evaluator_model = next(
        (
            str(component.get("evaluation_model") or "").strip()
            for component in report.get("components") or []
            if isinstance(component, dict)
            and str(component.get("evaluation_model") or "").strip()
        ),
        "",
    )
    values = {
        "rubric_version": _text(
            result.get("rubric_version") or report.get("rubric_version")
        ),
        "model": _text(result.get("model") or report.get("model")),
        "evaluator_model": _text(
            result.get("evaluator_model")
            or result.get("evaluation_model")
            or evaluator_model
        ),
        "candidate_schema_version": _text(candidate.get("schema_version")),
        "interpretation_prompt_version": _text(debug.get("prompt_version")),
        "shortlist_version": _text(debug.get("shortlist_version")),
        "evidence_labeling_version": _text(labeling.get("version")),
        "evaluation_evidence_version": _text(
            candidate.get("evaluation_evidence_version")
        ),
        "gate_authority": _text(debug.get("gate_authority")),
    }
    semantic_contract_fingerprint = _text(
        semantic_analysis_contract.get(
            "semantic_analysis_contract_fingerprint"
        )
    )
    if semantic_contract_fingerprint:
        values["vault_semantic_analysis_contract_fingerprint"] = (
            semantic_contract_fingerprint
        )
    fingerprint = hashlib.sha256(
        json.dumps(
            values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": ANALYSIS_CONTRACT_VERSION,
        **values,
        "fingerprint": fingerprint,
    }


def analysis_contracts_match(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    return bool(
        baseline.get("fingerprint")
        and baseline.get("fingerprint") == candidate.get("fingerprint")
    )


def _text(value: Any) -> str:
    return str(value or "").strip()
