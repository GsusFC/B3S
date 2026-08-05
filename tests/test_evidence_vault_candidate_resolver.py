from __future__ import annotations

import os
from copy import deepcopy
from uuid import uuid4

import pytest

from src.services.evidence_claim_tile_ledger import (
    build_evidence_claim_tile_ledger,
)
from src.services.evidence_memory_adjudication import (
    EvidenceMemoryAdjudicationCommand,
)
from src.services.evidence_memory_identity_v2 import (
    build_evidence_memory_identity_v2,
)
from src.services.evidence_scoring_recovery_review import (
    EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION,
    build_reviewed_scoring_memory_shadow,
)
from src.services.evidence_vault_candidate_resolver import (
    EvidenceVaultCandidateResolverError,
    build_canonical_aggregation_policy,
    build_resolved_canonical_memory_candidate,
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_authority import (
    CanonicalMemoryPromotionCommand,
    build_promotion_event,
    plan_canonical_memory_promotion,
    project_promoted_canonical_memory,
    promotion_request_fingerprint,
)


PACKET_FINGERPRINT = "a" * 64


def test_resolver_builds_one_promotable_baseline_from_accepted_durable_path() -> None:
    report = _report(
        "one",
        "2026-08-01T00:00:00Z",
        "Our mission is to make financial work radically simpler.",
    )
    ledger = build_evidence_claim_tile_ledger([report], mode="shadow")
    mapping = ledger["mappings"][0]

    result = _resolve(
        [report],
        ledger=ledger,
        adjudications=[_evidence_review(mapping["source_evidence_id"])],
        mapping_reviews=[_mapping_review(mapping, "accepted")],
        registered_packets={PACKET_FINGERPRINT},
    )

    packet = result["packet"]
    mission = _tile(packet, "M1")
    assert packet["manifest"]["candidate_count"] == 80
    assert packet["manifest"]["unresolved_items"] == []
    assert mission["candidate_state"] == "ok"
    assert mission["delta_kind"] == "baseline"
    assert mission["provenance_status"] == "accepted_basis"
    assert mission["basis"][0]["relation_id"] == mapping["mapping_id"]
    assert mission["basis"][0]["claim_id"] == mapping["claim_variant_id"]
    assert result["reference_resolution"]["reference_resolution_fingerprint"]
    assert result["authority"] is False
    assert result["runtime_effect"] is False

    plan_canonical_memory_promotion(
        packet,
        resolved_references=result["resolved_references"],
        current_memory=None,
    )


def test_unreviewed_mapping_is_visible_and_blocks_baseline_without_lighting_tile() -> None:
    report = _report(
        "one",
        "2026-08-01T00:00:00Z",
        "Our mission is to make financial work radically simpler.",
    )
    ledger = build_evidence_claim_tile_ledger([report], mode="shadow")

    result = _resolve([report], ledger=ledger)

    packet = result["packet"]
    mission = _tile(packet, "M1")
    unresolved = packet["manifest"]["unresolved_items"]
    assert mission["candidate_state"] == "sin_evidencia"
    assert mission["basis"] == []
    assert len(unresolved) == 1
    assert unresolved[0]["kind"] == "claim_tile_review_pending"
    assert unresolved[0]["blocking"] is True
    assert mission["unresolved_refs"] == [unresolved[0]["unresolved_id"]]


def test_mapping_acceptance_cannot_bypass_missing_evidence_acceptance() -> None:
    report = _report(
        "one",
        "2026-08-01T00:00:00Z",
        "Our mission is to make financial work radically simpler.",
    )
    ledger = build_evidence_claim_tile_ledger([report], mode="shadow")
    mapping = ledger["mappings"][0]

    result = _resolve(
        [report],
        ledger=ledger,
        mapping_reviews=[_mapping_review(mapping, "accepted")],
        registered_packets={PACKET_FINGERPRINT},
    )

    packet = result["packet"]
    assert _tile(packet, "M1")["candidate_state"] == "sin_evidencia"
    assert packet["manifest"]["unresolved_items"][0]["kind"] == ("accepted_mapping_evidence_not_accepted")


def test_review_must_resolve_to_a_registered_packet() -> None:
    report = _report(
        "one",
        "2026-08-01T00:00:00Z",
        "Our mission is to make financial work radically simpler.",
    )
    ledger = build_evidence_claim_tile_ledger([report], mode="shadow")
    mapping = ledger["mappings"][0]

    with pytest.raises(
        EvidenceVaultCandidateResolverError,
        match="unregistered packets",
    ):
        _resolve(
            [report],
            ledger=ledger,
            adjudications=[_evidence_review(mapping["source_evidence_id"])],
            mapping_reviews=[_mapping_review(mapping, "accepted")],
        )


def test_ledger_and_immutable_reports_must_resolve_to_same_state() -> None:
    report = _report(
        "one",
        "2026-08-01T00:00:00Z",
        "Our mission is to make financial work radically simpler.",
    )
    tampered_ledger = deepcopy(build_evidence_claim_tile_ledger([report], mode="shadow"))
    tampered_ledger["state_fingerprint"] = "f" * 64

    with pytest.raises(
        EvidenceVaultCandidateResolverError,
        match="does not match the immutable reports",
    ):
        _resolve([report], ledger=tampered_ledger)


def test_same_source_reformulation_does_not_strengthen_canonical_tile() -> None:
    first = _report(
        "one",
        "2026-08-01T00:00:00Z",
        "Our mission is to make financial work radically simpler.",
    )
    first_ledger = build_evidence_claim_tile_ledger([first], mode="shadow")
    first_mapping = first_ledger["mappings"][0]
    baseline = _resolve(
        [first],
        ledger=first_ledger,
        adjudications=[_evidence_review(first_mapping["source_evidence_id"])],
        mapping_reviews=[_mapping_review(first_mapping, "accepted")],
        registered_packets={PACKET_FINGERPRINT},
    )
    canonical = _promote(baseline)

    second = _report(
        "two",
        "2026-08-02T00:00:00Z",
        "Our mission is to turn financial operations into a daily advantage.",
    )
    evolved_ledger = build_evidence_claim_tile_ledger(
        [first, second],
        mode="shadow",
    )
    mappings = evolved_ledger["mappings"]
    result = _resolve(
        [first, second],
        ledger=evolved_ledger,
        adjudications=[
            _evidence_review(mapping["source_evidence_id"], seed=index)
            for index, mapping in enumerate(mappings, start=1)
        ],
        mapping_reviews=[
            _mapping_review(mapping, "accepted", seed=index) for index, mapping in enumerate(mappings, start=1)
        ],
        registered_packets={PACKET_FINGERPRINT},
        current=canonical,
    )

    packet = result["packet"]
    mission = _tile(packet, "M1")
    assert mission["delta_kind"] == "no_change"
    assert mission["candidate_state"] == "ok"
    assert len(mission["basis"]) == 1
    assert sum(tile["delta_kind"] == "no_change" for tile in packet["candidate_tiles"]) == 80


def test_independent_source_strengthens_only_its_existing_tile() -> None:
    first = _report(
        "one",
        "2026-08-01T00:00:00Z",
        "Our mission is to make financial work radically simpler.",
    )
    first_ledger = build_evidence_claim_tile_ledger([first], mode="shadow")
    first_mapping = first_ledger["mappings"][0]
    baseline = _resolve(
        [first],
        ledger=first_ledger,
        adjudications=[_evidence_review(first_mapping["source_evidence_id"])],
        mapping_reviews=[_mapping_review(first_mapping, "accepted")],
        registered_packets={PACKET_FINGERPRINT},
    )
    canonical = _promote(baseline)

    second = _report(
        "two",
        "2026-08-02T00:00:00Z",
        "Our mission is to turn financial operations into a daily advantage.",
    )
    for row in second["raw"]["flow"]["candidate"]["evidence_pack"]["evidence"]:
        row["url"] = "https://example.com/principles"
    evolved_ledger = build_evidence_claim_tile_ledger(
        [first, second],
        mode="shadow",
    )
    mappings = evolved_ledger["mappings"]
    result = _resolve(
        [first, second],
        ledger=evolved_ledger,
        adjudications=[
            _evidence_review(mapping["source_evidence_id"], seed=index)
            for index, mapping in enumerate(mappings, start=1)
        ],
        mapping_reviews=[
            _mapping_review(mapping, "accepted", seed=index) for index, mapping in enumerate(mappings, start=1)
        ],
        registered_packets={PACKET_FINGERPRINT},
        current=canonical,
    )

    packet = result["packet"]
    mission = _tile(packet, "M1")
    assert mission["delta_kind"] == "strengthened"
    assert mission["candidate_state"] == "ok"
    assert len(mission["basis"]) == 2
    assert len({row["source_identity_id"] for row in mission["basis"]}) == 2
    assert sum(tile["delta_kind"] == "no_change" for tile in packet["candidate_tiles"]) == 79


def test_pending_relation_and_coverage_loss_preserve_canonical_tile() -> None:
    first = _report(
        "one",
        "2026-08-01T00:00:00Z",
        "Our mission is to make financial work radically simpler.",
    )
    first_ledger = build_evidence_claim_tile_ledger([first], mode="shadow")
    first_mapping = first_ledger["mappings"][0]
    baseline = _resolve(
        [first],
        ledger=first_ledger,
        adjudications=[_evidence_review(first_mapping["source_evidence_id"])],
        mapping_reviews=[_mapping_review(first_mapping, "accepted")],
        registered_packets={PACKET_FINGERPRINT},
    )
    canonical = _promote(baseline)

    second = _report(
        "two",
        "2026-08-02T00:00:00Z",
        "Our mission is to turn financial operations into a daily advantage.",
    )
    ledger = build_evidence_claim_tile_ledger([first, second], mode="shadow")
    mappings = ledger["mappings"]
    accepted_mapping = next(
        mapping for mapping in mappings if mapping["source_evidence_id"] == first_mapping["source_evidence_id"]
    )

    result = _resolve(
        [first, second],
        ledger=ledger,
        adjudications=[
            _evidence_review(mapping["source_evidence_id"], seed=index)
            for index, mapping in enumerate(mappings, start=1)
        ],
        mapping_reviews=[_mapping_review(accepted_mapping, "accepted")],
        registered_packets={PACKET_FINGERPRINT},
        current=canonical,
    )

    mission = _tile(result["packet"], "M1")
    unresolved = result["packet"]["manifest"]["unresolved_items"]
    assert mission["delta_kind"] == "coverage_loss"
    assert mission["basis"] == canonical["content"]["tiles"][0]["basis"]
    assert any(item["kind"] == "claim_tile_review_pending" and item["blocking"] is True for item in unresolved)


def test_explicit_revocation_creates_verified_deprecation_not_silent_no() -> None:
    report = _report(
        "one",
        "2026-08-01T00:00:00Z",
        "Our mission is to make financial work radically simpler.",
    )
    ledger = build_evidence_claim_tile_ledger([report], mode="shadow")
    mapping = ledger["mappings"][0]
    baseline = _resolve(
        [report],
        ledger=ledger,
        adjudications=[_evidence_review(mapping["source_evidence_id"])],
        mapping_reviews=[_mapping_review(mapping, "accepted")],
        registered_packets={PACKET_FINGERPRINT},
    )
    canonical = _promote(baseline)

    revoked = _mapping_review(mapping, "revoked", seed=2)
    result = _resolve(
        [report],
        ledger=ledger,
        adjudications=[_evidence_review(mapping["source_evidence_id"])],
        mapping_reviews=[revoked],
        registered_packets={PACKET_FINGERPRINT},
        current=canonical,
    )

    mission = _tile(result["packet"], "M1")
    assert mission["delta_kind"] == "verified_deprecation"
    assert mission["candidate_state"] == "sin_evidencia"
    assert mission["changes_score"] is True
    assert [row["polarity"] for row in mission["basis"]] == ["invalidates_candidate"]
    assert result["packet"]["manifest"]["unresolved_items"] == []


def test_tampered_canonical_parent_fails_closed() -> None:
    report = _report(
        "one",
        "2026-08-01T00:00:00Z",
        "Our mission is to make financial work radically simpler.",
    )
    ledger = build_evidence_claim_tile_ledger([report], mode="shadow")
    mapping = ledger["mappings"][0]
    resolved = _resolve(
        [report],
        ledger=ledger,
        adjudications=[_evidence_review(mapping["source_evidence_id"])],
        mapping_reviews=[_mapping_review(mapping, "accepted")],
        registered_packets={PACKET_FINGERPRINT},
    )
    tampered = deepcopy(_promote(resolved))
    tampered["content"]["tiles"][0]["state"] = "no"

    with pytest.raises(
        EvidenceVaultCandidateResolverError,
        match="fingerprint mismatch",
    ):
        _resolve(
            [report],
            ledger=ledger,
            adjudications=[_evidence_review(mapping["source_evidence_id"])],
            mapping_reviews=[_mapping_review(mapping, "accepted")],
            registered_packets={PACKET_FINGERPRINT},
            current=tampered,
        )


def test_aggregation_contract_is_content_addressed_and_has_no_runtime_effect() -> None:
    policy = build_canonical_aggregation_policy()
    fingerprint = canonical_aggregation_policy_fingerprint()

    assert len(fingerprint) == 64
    assert policy["tile_points"] == {
        "ok": 1,
        "no": 0,
        "sin_evidencia": 0,
        "contradiction": None,
    }
    assert policy["scanner_runtime_effect"] is False
    assert policy["production_runtime_effect"] is False
    assert len(policy["components"]) == 10


def test_current_literal_evidence_can_reach_a_tile_without_a_claim() -> None:
    report = _direct_report(
        "one",
        "2026-08-01T00:00:00Z",
        state="ok",
    )
    ledger = build_evidence_claim_tile_ledger([report], mode="shadow")
    preview = build_reviewed_scoring_memory_shadow([report])
    source_evidence_id = preview["accepted_evidence"][0]["source_evidence_ids"][0]

    result = _resolve(
        [report],
        ledger=ledger,
        adjudications=[_evidence_review(source_evidence_id)],
    )

    mission = _tile(result["packet"], "M1")
    assert ledger["mappings"] == []
    assert mission["candidate_state"] == "ok"
    assert mission["provenance_status"] == "scanner_derived_unreviewed"
    assert mission["basis"][0]["claim_id"] is None
    assert mission["basis"][0]["review_status"] == "unreviewed"
    assert result["packet"]["manifest"]["coverage_summary"]["direct_evidence_tile_relation_count"] == 1
    plan_canonical_memory_promotion(
        result["packet"],
        resolved_references=result["resolved_references"],
        current_memory=None,
    )


def test_report_order_does_not_change_direct_candidate_packet() -> None:
    first = _direct_report(
        "one",
        "2026-08-01T00:00:00Z",
        state="ok",
    )
    second = _direct_report(
        "two",
        "2026-08-02T00:00:00Z",
        state="ok",
    )
    reports = [first, second]
    ledger = build_evidence_claim_tile_ledger(reports, mode="shadow")
    preview = build_reviewed_scoring_memory_shadow(reports)
    source_evidence_id = preview["accepted_evidence"][0]["source_evidence_ids"][0]
    adjudications = [_evidence_review(source_evidence_id)]

    ordered = _resolve(
        reports,
        ledger=ledger,
        adjudications=adjudications,
    )
    reversed_result = _resolve(
        list(reversed(reports)),
        ledger=ledger,
        adjudications=adjudications,
    )

    assert reversed_result["packet"] == ordered["packet"]
    assert reversed_result["resolved_references"] == ordered["resolved_references"]


def test_reviewed_recovery_is_accepted_direct_basis_without_a_claim() -> None:
    first = _direct_report(
        "one",
        "2026-08-01T00:00:00Z",
        state="ok",
    )
    second = _direct_report(
        "two",
        "2026-08-02T00:00:00Z",
        state="sin_evidencia",
    )
    reports = [first, second]
    ledger = build_evidence_claim_tile_ledger(reports, mode="shadow")
    preview = build_reviewed_scoring_memory_shadow(reports)
    candidate = preview["recovery_review_candidates"][0]
    source_evidence_id = candidate["evidence"]["source_evidence_ids"][0]

    result = _resolve(
        reports,
        ledger=ledger,
        adjudications=[_evidence_review(source_evidence_id)],
        recovery_reviews=[_recovery_review(candidate, "accepted")],
    )

    mission = _tile(result["packet"], "M1")
    assert mission["candidate_state"] == "ok"
    assert mission["provenance_status"] == "accepted_basis"
    assert mission["basis"][0]["claim_id"] is None
    assert mission["basis"][0]["review_status"] == "accepted"
    assert mission["basis"][0]["decision_event_id"] == "recovery-review-1"


def test_revoked_direct_recovery_creates_verified_deprecation() -> None:
    first = _direct_report(
        "one",
        "2026-08-01T00:00:00Z",
        state="ok",
    )
    second = _direct_report(
        "two",
        "2026-08-02T00:00:00Z",
        state="sin_evidencia",
    )
    reports = [first, second]
    ledger = build_evidence_claim_tile_ledger(reports, mode="shadow")
    preview = build_reviewed_scoring_memory_shadow(reports)
    candidate = preview["recovery_review_candidates"][0]
    source_evidence_id = candidate["evidence"]["source_evidence_ids"][0]
    adjudications = [_evidence_review(source_evidence_id)]
    accepted = _recovery_review(candidate, "accepted")
    baseline = _resolve(
        reports,
        ledger=ledger,
        adjudications=adjudications,
        recovery_reviews=[accepted],
    )
    canonical = _promote(baseline)
    revoked = _recovery_review(
        candidate,
        "revoked",
        sequence=2,
        previous_event_id=accepted["event_id"],
    )

    result = _resolve(
        reports,
        ledger=ledger,
        adjudications=adjudications,
        recovery_reviews=[revoked],
        current=canonical,
    )

    mission = _tile(result["packet"], "M1")
    assert mission["delta_kind"] == "verified_deprecation"
    assert mission["candidate_state"] == "sin_evidencia"
    assert [row["polarity"] for row in mission["basis"]] == ["invalidates_candidate"]


def test_repository_safe_entrypoint_accepts_no_caller_supplied_packet() -> None:
    from src.history.repository import PostgresHistoryRepository

    report = _report(
        "one",
        "2026-08-01T00:00:00Z",
        "Our mission is to make financial work radically simpler.",
    )
    ledger = build_evidence_claim_tile_ledger([report], mode="shadow")
    mapping = ledger["mappings"][0]
    adjudication = _evidence_review(mapping["source_evidence_id"])
    review = _mapping_review(mapping, "accepted")

    class Repository(PostgresHistoryRepository):
        def __init__(self) -> None:
            self.registered_packet = None
            self.checked_review_packet = None

        def _ensure_migrated(self) -> None:
            return None

        def list_report_payloads_for_domain(
            self,
            _domain,
            *,
            workspace_slug,
            limit,
            offset,
        ):
            assert workspace_slug == "b3s"
            assert limit == 500
            return [report] if offset == 0 else []

        def get_evidence_claim_tile_ledger(self, *_args, **_kwargs):
            return ledger

        def list_current_evidence_memory_adjudications(
            self,
            *_args,
            **_kwargs,
        ):
            return [adjudication]

        def list_current_evidence_claim_reconciliations(
            self,
            *_args,
            **_kwargs,
        ):
            return []

        def list_current_evidence_claim_tile_reviews(
            self,
            *_args,
            **_kwargs,
        ):
            return [review]

        def list_current_evidence_scoring_recovery_reviews(
            self,
            *_args,
            **_kwargs,
        ):
            return []

        def get_evidence_claim_tile_review_packet(
            self,
            _domain,
            fingerprint,
            **_kwargs,
        ):
            self.checked_review_packet = fingerprint
            return {"packet_fingerprint": fingerprint}

        def get_evidence_vault_canonical_memory(
            self,
            *_args,
            **_kwargs,
        ):
            return None

        def register_evidence_vault_canonical_memory_packet(
            self,
            _domain,
            packet,
            *,
            resolved_references,
            workspace_slug,
        ):
            self.registered_packet = packet
            assert resolved_references == {field: packet["manifest"][field] for field in resolved_references}
            assert workspace_slug == "b3s"
            return {"packet": packet}, False

    repository = Repository()
    stored, replayed = repository.build_and_register_evidence_vault_canonical_memory_packet(
        "https://www.example.com/path"
    )

    assert replayed is False
    assert repository.checked_review_packet == PACKET_FINGERPRINT
    assert repository.registered_packet is stored["packet"]
    assert _tile(stored["packet"], "M1")["candidate_state"] == "ok"


@pytest.mark.skipif(
    not os.environ.get("B3S_TEST_DATABASE_URL"),
    reason="B3S_TEST_DATABASE_URL is required for PostgreSQL integration",
)
def test_generated_packet_is_idempotent_and_survives_repository_restart(
    monkeypatch,
) -> None:
    import psycopg

    from src.history.repository import PostgresHistoryRepository

    if os.environ.get("B3S_ALLOW_SCHEMA_DROP") != "1":
        raise RuntimeError("B3S_ALLOW_SCHEMA_DROP=1 is required for the disposable test database")
    monkeypatch.setenv(
        "B3S_EVIDENCE_CLAIM_TILE_LEDGER_MODE",
        "shadow",
    )
    dsn = os.environ["B3S_TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")

    report = _direct_report(
        "durable-one",
        "2026-08-01T00:00:00Z",
        state="ok",
    )
    repository = PostgresHistoryRepository(dsn)
    try:
        repository.migrate()
        repository.import_report(report)
        evidence_id = build_evidence_memory_identity_v2([report])["entries"][0]["evidence_id"]
        repository.append_evidence_memory_adjudication(
            "example.com",
            EvidenceMemoryAdjudicationCommand(
                subject_id=evidence_id,
                decision="accepted",
                expected_current_event_id=None,
                reviewer="gsus",
                reason_code="identity_reviewed",
                rationale="The durable evidence identity was reviewed.",
                evaluator_version="manual-review-v1",
                actor_id="gsus",
                idempotency_key_hash="1" * 64,
                request_fingerprint="2" * 64,
            ),
        )

        stored, replayed = repository.build_and_register_evidence_vault_canonical_memory_packet("example.com")
        restarted = PostgresHistoryRepository(dsn)
        repeated, repeated_replayed = restarted.build_and_register_evidence_vault_canonical_memory_packet("example.com")

        assert replayed is False
        assert repeated_replayed is True
        assert repeated["packet"]["candidate_packet_fingerprint"] == stored["packet"]["candidate_packet_fingerprint"]
        assert repeated["packet"] == stored["packet"]
        assert _tile(stored["packet"], "M1")["candidate_state"] == "ok"
        assert stored["authority_state"] == "pending_review"
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS b3s_history CASCADE")


def _resolve(
    reports: list[dict],
    *,
    ledger: dict,
    adjudications: list[dict] | None = None,
    mapping_reviews: list[dict] | None = None,
    recovery_reviews: list[dict] | None = None,
    registered_packets: set[str] | None = None,
    current: dict | None = None,
) -> dict:
    return build_resolved_canonical_memory_candidate(
        brand_identity="example.com",
        reports=reports,
        evidence_adjudications=adjudications or [],
        claim_tile_ledger=ledger,
        claim_tile_reviews=mapping_reviews or [],
        scoring_recovery_reviews=recovery_reviews or [],
        registered_review_packet_fingerprints=(registered_packets or set()),
        current_canonical_memory=current,
    )


def _promote(resolved: dict) -> dict:
    packet = resolved["packet"]
    plan = plan_canonical_memory_promotion(
        packet,
        resolved_references=resolved["resolved_references"],
        current_memory=None,
    )
    reviewer_id = "gsus"
    reviewed_at = "2026-08-03T12:00:00+02:00"
    rationale = "Exact durable baseline reviewed."
    command = CanonicalMemoryPromotionCommand(
        candidate_packet_fingerprint=packet["candidate_packet_fingerprint"],
        parent_canonical_memory_version=None,
        reviewer_id=reviewer_id,
        reviewed_at=reviewed_at,
        rationale=rationale,
        idempotency_key_hash="f" * 64,
        request_fingerprint=promotion_request_fingerprint(
            candidate_packet_fingerprint=packet["candidate_packet_fingerprint"],
            parent_canonical_memory_version=None,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            rationale=rationale,
        ),
    )
    event = build_promotion_event(
        plan,
        command,
        event_id=str(uuid4()),
        sequence=1,
        previous_event_id=None,
    )
    return project_promoted_canonical_memory(plan, event)


def _tile(packet: dict, tile_id: str) -> dict:
    return next(tile for tile in packet["candidate_tiles"] if tile["tile_id"] == tile_id)


def _evidence_review(
    evidence_id: str,
    *,
    seed: int = 1,
    decision: str = "accepted",
) -> dict:
    return {
        "id": f"evidence-review-{seed}",
        "subject_type": "evidence",
        "subject_id": evidence_id,
        "sequence": 1,
        "decision": decision,
        "reviewer": "gsus",
        "reason_code": "identity_reviewed",
        "rationale": "The evidence identity was reviewed.",
        "evaluator_version": "manual-review-v1",
        "actor_id": "gsus",
        "created_at": f"2026-08-0{seed}T10:00:00Z",
        "runtime_effect": False,
        "authority": False,
    }


def _mapping_review(
    mapping: dict,
    decision: str,
    *,
    seed: int = 1,
) -> dict:
    return {
        **{
            field: mapping[field]
            for field in (
                "mapping_id",
                "mapping_series_id",
                "source_evidence_id",
                "claim_variant_id",
                "component_key",
                "tile_id",
                "tile_key",
                "polarity",
            )
        },
        "subject_id": mapping["mapping_id"],
        "event_id": f"mapping-review-{seed}",
        "sequence": seed,
        "decision": decision,
        "reviewer_id": "gsus",
        "rationale": "The tile contract mapping was reviewed.",
        "reviewed_at": f"2026-08-0{seed}T11:00:00Z",
        "evaluator_version": "manual-review-v1",
        "review_packet_fingerprint": PACKET_FINGERPRINT,
        "runtime_effect": False,
        "authority": False,
        "automatic_tile_effect": False,
        "automatic_scoring_effect": False,
    }


def _recovery_review(
    candidate: dict,
    decision: str,
    *,
    sequence: int = 1,
    previous_event_id: str | None = None,
) -> dict:
    return {
        "schema_version": EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION,
        "case_id": candidate["case_id"],
        "candidate_fingerprint": candidate["candidate_fingerprint"],
        "event_id": f"recovery-review-{sequence}",
        "sequence": sequence,
        "previous_event_id": previous_event_id,
        "decision": decision,
        "reviewer_id": "gsus",
        "rationale": "The direct evidence-to-tile relation was reviewed.",
        "reviewed_at": f"2026-08-0{sequence}T11:30:00Z",
        "runtime_effect": False,
        "authority": False,
    }


def _direct_report(
    report_id: str,
    created_at: str,
    *,
    state: str,
) -> dict:
    report = _report(
        report_id,
        created_at,
        "Our mission is to make financial work radically simpler.",
    )
    evidence = report["raw"]["flow"]["candidate"]["evidence_pack"]["evidence"]
    report["raw"]["flow"]["candidate"]["evidence_pack"]["evidence"] = [evidence[0]]
    verdict = report["raw"]["sv9"]["result"]["components"]["mission"]["tile_profile"][0]
    verdict["estado"] = state
    verdict["evidencia"] = evidence[0]["content"] if state == "ok" else ""
    report["components"] = [
        {
            "key": "mission",
            "label": "Misión",
            "status": "scored",
            "score": 1 if state == "ok" else 0,
            "scale": 5,
            "points": 1 if state == "ok" else 0,
            "confidence": "high",
            "resumen": verdict["evidencia"],
            "veredicto": "Synthetic direct-relation fixture.",
            "message": "Synthetic direct-relation fixture.",
            "tile_profile": [dict(verdict)],
        }
    ]
    return report


def _report(
    report_id: str,
    created_at: str,
    claim: str,
) -> dict:
    source = {
        "ref": "web.0",
        "source": "web",
        "evidence_type": "raw_input",
        "url": "https://example.com/about",
        "content": claim,
        "metadata": {"source_class": "owned_copy"},
    }
    interpreted_claim = {
        "ref": "claim.0",
        "source": "derived_strategy",
        "evidence_type": "interpreted_claim",
        "url": "https://example.com/about",
        "content": claim,
        "metadata": {
            "source_class": "derived_strategy",
            "source_evidence_ref": "web.0",
            "claim_slot_key": "mission.primary",
            "claim_type": "mission",
        },
    }
    return {
        "id": report_id,
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": created_at,
        "reliability_status": "shadow",
        "acquisition_gate": {"state": "pass"},
        "components": [],
        "blocks": [],
        "raw": {
            "schema_version": "report-v1",
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "brand_name": "Example",
                        "url": "https://example.com",
                        "evidence": [source, interpreted_claim],
                    }
                }
            },
            "sv9": {
                "evaluator_model": "tile-evaluator-a",
                "result": {
                    "rubric_version": "baldosas-v3-1",
                    "components": {
                        "mission": {
                            "tile_profile": [
                                {
                                    "id": "M1",
                                    "estado": "ok",
                                    "evidencia": claim,
                                    "evidence_ref": "",
                                }
                            ]
                        }
                    },
                },
            },
        },
    }
