#!/usr/bin/env python3
"""Generate a pending-only coverage supplement from frozen evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from src.config import SV9_FLOW_LABELING_MODEL
from src.features.llm_analyzer import LLMAnalyzer
from src.services.evidence_vault_canonical_core import (
    build_tile_contract_registry,
)
from src.services.evidence_vault_coverage_supplement import (
    build_coverage_supplement_request,
    execute_coverage_supplement,
)
from src.services.scanner_evidence_comparison import (
    canonical_evidence_representatives,
)


class _PersistedProposalReplay:
    api_key = "persisted-replay"

    def __init__(self, relations: list[Any]) -> None:
        self._relations = relations

    def _call_json(self, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
        del system, user, kwargs
        return {"relations": self._relations}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worksheet", type=Path, required=True)
    parser.add_argument("--replay-proposal-from", type=Path)
    args = parser.parse_args()
    config_path = args.config.resolve()
    config = _json_object(config_path)
    if (
        config.get("schema_version")
        != "evidence-vault-field-coverage-supplement-source-v1"
        or config.get("authority") is not False
        or config.get("runtime_effect") is not False
    ):
        raise SystemExit("coverage supplement source config is invalid")
    pack_path = (config_path.parent / config["source_evidence_pack_file"]).resolve()
    pack = _json_object(pack_path)
    canonical_pack = json.dumps(
        pack,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(canonical_pack).hexdigest() != config[
        "source_evidence_pack_canonical_sha256"
    ]:
        raise SystemExit("coverage supplement evidence pack integrity failed")
    representatives = canonical_evidence_representatives(
        pack["evidence"],
        subject_url=config["subject_url"],
    )
    by_ref = {
        str(row.get("ref") or ""): (fingerprint, row)
        for fingerprint, row in representatives.items()
    }
    requested_refs = [row["ref"] for row in config["evidence_tile_requests"]]
    if len(requested_refs) != len(set(requested_refs)) or not set(
        requested_refs
    ).issubset(by_ref):
        raise SystemExit("coverage supplement evidence refs are invalid")
    evidence_rows: list[dict[str, Any]] = []
    shortlists: dict[str, list[str]] = {}
    for item in config["evidence_tile_requests"]:
        fingerprint, raw = by_ref[item["ref"]]
        evidence_rows.append(
            {
                "evidence_fingerprint": fingerprint,
                **dict(raw),
            }
        )
        shortlists[fingerprint] = list(item["tile_ids"])
    request = build_coverage_supplement_request(
        brand_identity=config["brand_identity"],
        subject_url=config["subject_url"],
        parent_canonical_memory_version=config[
            "parent_canonical_memory_version"
        ],
        source_evidence_pack_sha256=config[
            "source_evidence_pack_canonical_sha256"
        ],
        evidence_rows=evidence_rows,
        tile_shortlists=shortlists,
        target_tile_ids=list(config["target_tile_ids"]),
        rationale=config["rationale"],
    )
    execution_mode = "live_provider"
    replay_source_result_fingerprint = None
    replay_source_file_sha256 = None
    replay_source_file = None
    if args.replay_proposal_from is None:
        llm = LLMAnalyzer(model=SV9_FLOW_LABELING_MODEL)
        if not llm.api_key:
            raise SystemExit("configured Brand3 LLM API key is required")
    else:
        replay_artifact = _json_object(args.replay_proposal_from.resolve())
        replay_source_result_fingerprint = replay_artifact.get(
            "result", {}
        ).get("result_fingerprint")
        replay_source_file_sha256 = hashlib.sha256(
            args.replay_proposal_from.resolve().read_bytes()
        ).hexdigest()
        replay_source_file = args.replay_proposal_from.name
        replay_relations = replay_artifact.get("result", {}).get(
            "relation_proposal", {}
        ).get("relations")
        if not isinstance(replay_relations, list):
            raise SystemExit("replay artifact has no relation proposal")
        llm = _PersistedProposalReplay(replay_relations)
        execution_mode = "persisted_relation_replay"
    result = execute_coverage_supplement(
        request,
        evidence_rows=evidence_rows,
        llm=llm,
    )
    contracts = {
        row["tile_id"]: row
        for row in build_tile_contract_registry()["tiles"]
    }
    evidence_by_fingerprint = {
        row["evidence_fingerprint"]: row for row in evidence_rows
    }
    worksheet = []
    for relation in result["basis_relations"]:
        evidence = evidence_by_fingerprint[
            relation["evidence_fingerprint"]
        ]
        contract = contracts[relation["tile_id"]]
        worksheet.append(
            {
                "relation_id": relation["relation_id"],
                "evidence_fingerprint": relation[
                    "evidence_fingerprint"
                ],
                "evidence_ref": evidence["ref"],
                "evidence_url": evidence.get("url") or "",
                "source_class": evidence.get("metadata", {}).get(
                    "source_class"
                )
                or "",
                "tile_id": relation["tile_id"],
                "tile_key": contract["tile_key"],
                "tile_condition": contract["condition"],
                "evidence_contract_ok": contract["evidence_contract"]["ok"],
                "evidence_contract_reject": contract[
                    "evidence_contract"
                ]["reject"],
                "polarity": relation["polarity"],
                "literal_quote": relation["literal_quote"],
                "rationale": relation["rationale"],
                "review_effect": "relation_decision_only_no_runtime_effect",
                "allowed_human_decisions": "accept|reject",
                "human_decision": "",
                "human_rationale": "",
                "reviewer_id": "",
                "reviewed_at": "",
            }
        )
    output = {
        "schema_version": "evidence-vault-field-coverage-supplement-artifact-v1",
        "model": SV9_FLOW_LABELING_MODEL,
        "execution_mode": execution_mode,
        "replay_source_result_fingerprint": replay_source_result_fingerprint,
        "replay_source_file_sha256": replay_source_file_sha256,
        "replay_source_file": replay_source_file,
        "request": request,
        "result": result,
        "explicit_no_candidate_tile_ids": config[
            "explicit_no_candidate_tile_ids"
        ],
        "limitations": config["limitations"],
        "worksheet_relation_count": len(worksheet),
        "review_instructions": {
            "allowed_human_decisions": ["accept", "reject"],
            "required_fields": [
                "human_decision",
                "human_rationale",
                "reviewer_id",
                "reviewed_at",
            ],
            "effect": "relation_decision_only_no_runtime_effect",
        },
        "authority": False,
        "runtime_effect": False,
        "cutover_authorized": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    args.worksheet.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(worksheet[0]) if worksheet else [
        "relation_id",
        "human_decision",
        "human_rationale",
        "reviewer_id",
        "reviewed_at",
    ]
    with args.worksheet.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(worksheet)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
