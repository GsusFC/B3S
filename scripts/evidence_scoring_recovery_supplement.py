#!/usr/bin/env python3
"""Build or register a mapper-omission evidence-to-tile supplement."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import B3S_DATABASE_URL
from src.history.repository import PostgresHistoryRepository
from src.services.evidence_claim_tile_ledger import (
    build_evidence_claim_tile_ledger,
)
from src.services.evidence_memory_identity_v2 import (
    build_accepted_evidence_passage_catalog,
)
from src.services.evidence_scoring_recovery_review import (
    build_recovery_review_supplement_packet_from_preview,
    build_reviewed_scoring_memory_shadow,
)


PROPOSAL_VERSION = "evidence-scoring-recovery-supplement-proposal-v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--proposals", type=Path, required=True)
    parser.add_argument("--database-url", default=B3S_DATABASE_URL)
    parser.add_argument("--workspace-slug", default="b3s")
    parser.add_argument(
        "--register",
        action="store_true",
        help="Persist the immutable non-authoritative packet.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.database_url:
        raise SystemExit("B3S database URL is required")
    proposals = load_proposals(args.proposals)
    repository = PostgresHistoryRepository(args.database_url)
    reports = _all_reports(
        repository,
        args.domain,
        workspace_slug=args.workspace_slug,
    )
    if not reports:
        raise SystemExit("No immutable reports found for the brand")
    adjudications = repository.list_current_evidence_memory_adjudications(
        args.domain,
        workspace_slug=args.workspace_slug,
    )
    ledger = build_evidence_claim_tile_ledger(reports, mode="shadow")
    preview = build_reviewed_scoring_memory_shadow(
        reports,
        evidence_adjudications=adjudications,
        reviewed_claim_tile_memory=(
            repository.get_reviewed_claim_tile_memory_shadow(
                args.domain,
                workspace_slug=args.workspace_slug,
            )
        ),
        claim_tile_ledger=ledger,
    )
    source_catalog = build_accepted_evidence_passage_catalog(
        reports,
        adjudications=adjudications,
    )
    packet = build_recovery_review_supplement_packet_from_preview(
        preview,
        source_catalog=source_catalog,
        proposals=proposals,
    )
    stored = None
    replayed = False
    if args.register:
        stored, replayed = (
            repository.register_evidence_scoring_recovery_supplement_packet(
                args.domain,
                packet,
                workspace_slug=args.workspace_slug,
            )
        )
    print(
        json.dumps(
            {
                "status": "registered" if args.register else "preview",
                "schema_version": packet["schema_version"],
                "packet_fingerprint": packet["packet_fingerprint"],
                "candidate_count": len(packet["candidates"]),
                "case_ids": [
                    candidate["case_id"]
                    for candidate in packet["candidates"]
                ],
                "replayed": replayed,
                "stored_id": stored["id"] if stored else None,
                "authority": False,
                "runtime_effect": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def load_proposals(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get(
        "schema_version"
    ) != PROPOSAL_VERSION:
        raise ValueError("supplement proposal schema version mismatch")
    relations = payload.get("relations")
    if not isinstance(relations, list) or not relations or not all(
        isinstance(row, dict) for row in relations
    ):
        raise ValueError("supplement proposal relations are required")
    return [dict(row) for row in relations]


def _all_reports(
    repository: PostgresHistoryRepository,
    domain: str,
    *,
    workspace_slug: str,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = repository.list_report_payloads_for_domain(
            domain,
            workspace_slug=workspace_slug,
            limit=500,
            offset=offset,
        )
        reports.extend(batch)
        if len(batch) < 500:
            return reports
        offset += len(batch)


if __name__ == "__main__":
    raise SystemExit(main())
