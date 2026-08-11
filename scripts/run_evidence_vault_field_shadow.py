#!/usr/bin/env python3
"""Run normalized real-brand baseline shadows in a disposable Vault database.

This command never reviews, adopts, scores, or touches the live scanner.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from src.config import SV9_FLOW_LABELING_MODEL
from src.features.llm_analyzer import LLMAnalyzer
from src.history.repository import PostgresHistoryRepository
from src.services.evidence_memory_identity_v2 import (
    project_evidence_memory_row_identity,
)
from src.services.evidence_vault_canonical_core import (
    build_tile_contract_registry,
)
from src.services.evidence_vault_field_replay import (
    has_libpq_connection_override_environment,
    is_local_postgres_dsn,
    postgres_database_name,
    validate_evidence_vault_field_replay,
)
from src.services.evidence_vault_incremental_executor import (
    execute_vault_operation_plan,
)
from src.services.evidence_vault_incremental_refresh import (
    build_vault_scan_plan,
)
from src.services.scanner_evidence_comparison import (
    canonical_evidence_representatives,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(
            "fixtures/evidence_vault_field_validation_v1/manifest.json"
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker-prefix", default="field-shadow-v1")
    args = parser.parse_args()
    database_name = postgres_database_name(args.dsn)
    if os.environ.get("B3S_FIELD_SHADOW_ALLOW_WRITE") != database_name:
        raise SystemExit(
            "B3S_FIELD_SHADOW_ALLOW_WRITE must equal the disposable database name"
        )
    if not is_local_postgres_dsn(args.dsn):
        raise SystemExit("field shadow refuses a non-local PostgreSQL DSN")
    if has_libpq_connection_override_environment(os.environ):
        raise SystemExit("field shadow refuses ambient libpq routing overrides")

    planner_gate = validate_evidence_vault_field_replay(args.manifest)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    llm = LLMAnalyzer(model=SV9_FLOW_LABELING_MODEL)
    if not llm.api_key:
        raise SystemExit("configured Brand3 LLM API key is required")
    repository = PostgresHistoryRepository(args.dsn)
    repository.migrate()

    results: list[dict[str, Any]] = []
    for case in manifest["cases"]:
        observation_meta = case["observations"][0]
        pack_path = args.manifest.parent / observation_meta[
            "evidence_pack_file"
        ]
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
        plan = build_vault_scan_plan(
            brand_identity=case["brand_identity"],
            subject_url=case["subject_url"],
            mode="baseline",
            current_evidence_records=pack["evidence"],
        )
        source_scan_id = (
            f"field-shadow-v1-{case['case_id']}-{observation_meta['report_id']}"
        )
        captured_at = observation_meta["report_created_at"]
        observation = {
            "schema_version": "b3s-capture-observation-v1",
            "source_scan_id": source_scan_id,
            "source_run_id": observation_meta["report_id"],
            "brand_name": pack["brand_name"],
            "url": pack["url"],
            "observed_at": captured_at,
            "recorded_at": captured_at,
            "pipeline_version": "vault-normalized-field-shadow-v1",
            "acquisition_state": "partial",
            "acquisition_summary": {
                "replay_level": "normalized_evidence_pack_only",
                "source_report_sha256": observation_meta[
                    "source_report_sha256"
                ],
                "evidence_pack_canonical_sha256": observation_meta[
                    "evidence_pack_canonical_sha256"
                ],
            },
            "limitations": list(manifest["limitations"]),
            "capture_payload": {
                "schema_version": "normalized-evidence-field-replay-v1",
                "case_id": case["case_id"],
                "source_report_id": observation_meta["report_id"],
                "fixture_manifest_schema_version": manifest["schema_version"],
            },
            "evidence_records": pack["evidence"],
            "acquisition_attempts": [],
            "artifacts": [],
            "metadata": {
                "mode": "baseline",
                "analysis_status": "pending",
                "operation_plan": plan,
                "operation_plan_fingerprint": plan[
                    "operation_plan_fingerprint"
                ],
                "replay_level": "normalized_evidence_pack_only",
                "authority": False,
                "runtime_effect": False,
            },
        }
        repository.persist_capture_observation(observation)
        execution = execute_vault_operation_plan(
            repository=repository,
            source_scan_id=source_scan_id,
            worker_id=f"{args.worker_prefix}-{case['case_id']}",
            llm=llm,
            lease_seconds=900,
        )
        operation = repository.get_capture_operation_plan(source_scan_id)
        if operation is None or operation["status"] != "completed":
            raise RuntimeError(
                f"{case['case_id']} did not complete: {execution['execution_status']}"
            )
        result = operation["result_payload"]
        if (
            result["authority"] is not False
            or result["operational_candidate_packet"][
                "has_accepted_change"
            ]
            is not False
            or repository.get_evidence_vault_operational_memory(
                case["brand_identity"]
            )
            is not None
        ):
            raise RuntimeError(
                f"{case['case_id']} crossed the shadow authority boundary"
            )
        worksheet = _review_worksheet(
            case=case,
            pack=pack,
            result=result,
        )
        results.append(
            {
                "case_id": case["case_id"],
                "brand_identity": case["brand_identity"],
                "source_scan_id": source_scan_id,
                "operation_status": operation["status"],
                "execution_status": execution["execution_status"],
                "operation_attempt_count": operation["attempt_count"],
                "operation_plan_fingerprint": operation[
                    "operation_plan_fingerprint"
                ],
                "result_fingerprint": operation["result_fingerprint"],
                "source_candidate_packet_fingerprint": result[
                    "source_candidate_packet_fingerprint"
                ],
                "operational_candidate_packet_fingerprint": result[
                    "candidate_packet_fingerprint"
                ],
                "selected_fingerprint_count": len(
                    result["selected_evidence_fingerprints"]
                ),
                "semantic_candidate_count": sum(
                    value == "semantic_candidate"
                    for value in result[
                        "evidence_work_dispositions"
                    ].values()
                ),
                "shortlisted_pair_count": sum(
                    len(rows) for rows in result["tile_shortlists"].values()
                ),
                "shortlist_truncation_count": len(
                    result["shortlist_truncations"]
                ),
                "omitted_shortlist_pair_count": sum(
                    len(row["omitted_tile_ids"])
                    for row in result["shortlist_truncations"].values()
                ),
                "shortlist_truncations": result["shortlist_truncations"],
                "relation_count": len(result["basis_relations"]),
                "discarded_relation_count": len(
                    result["relation_proposal"]["discarded_relations"]
                ),
                "discarded_relations": result["relation_proposal"][
                    "discarded_relations"
                ],
                "accepted_change": False,
                "authorized_memory_present": False,
                "score_created": False,
                "review_worksheet": worksheet,
            }
        )

    output = {
        "schema_version": "evidence-vault-normalized-field-shadow-result-v1",
        "replay_level": "normalized_evidence_pack_only",
        "model": SV9_FLOW_LABELING_MODEL,
        "planner_gate_fingerprint": planner_gate["result_fingerprint"],
        "authority": False,
        "runtime_effect": False,
        "cutover_authorized": False,
        "model_output_rejection_count": sum(
            row["discarded_relation_count"] for row in results
        ),
        "semantic_gate_status": "human_review_required",
        "cases": results,
        "next_required_action": (
            "human_review_every_pending_relation_before_any_adoption"
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _review_worksheet(
    *,
    case: dict[str, Any],
    pack: dict[str, Any],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    representatives = canonical_evidence_representatives(
        pack["evidence"],
        subject_url=case["subject_url"],
    )
    identities = {
        fingerprint: project_evidence_memory_row_identity(
            row,
            brand_domain=case["brand_identity"],
        )
        for fingerprint, row in representatives.items()
    }
    basis_by_key = {
        (
            row["evidence_id"],
            row["tile_id"],
            row["polarity"],
        ): row
        for row in result["basis_relations"]
    }
    contracts = {
        row["tile_id"]: row
        for row in build_tile_contract_registry()["tiles"]
    }
    worksheet: list[dict[str, Any]] = []
    for proposal in result["relation_proposal"]["relations"]:
        fingerprint = proposal["evidence_fingerprint"]
        identity = identities[fingerprint]
        basis = basis_by_key[
            (
                identity["evidence_id"],
                proposal["tile_id"],
                proposal["polarity"],
            )
        ]
        evidence = representatives[fingerprint]
        contract = contracts[proposal["tile_id"]]
        worksheet.append(
            {
                "relation_id": basis["relation_id"],
                "evidence_fingerprint": fingerprint,
                "evidence_ref": evidence.get("ref"),
                "evidence_url": evidence.get("url"),
                "tile_id": proposal["tile_id"],
                "tile_key": contract["tile_key"],
                "tile_condition": contract["condition"],
                "polarity": proposal["polarity"],
                "literal_quote": proposal["literal_quote"],
                "rationale": proposal["rationale"],
                "semantic_labels": result["semantic_labels"][fingerprint],
                "human_decision": None,
                "human_rationale": None,
            }
        )
    return worksheet



if __name__ == "__main__":
    raise SystemExit(main())
