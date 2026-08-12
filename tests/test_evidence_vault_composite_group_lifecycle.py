from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from src.history.repository import _validate_composite_group_resolution
from src.services.evidence_vault_candidate_resolver import (
    canonical_aggregation_policy_fingerprint,
)
from src.services.evidence_vault_canonical_core import (
    build_candidate_tile,
    canonical_fingerprint,
    build_tile_contract_registry,
)
from src.services.evidence_vault_composite_group_lifecycle import (
    EvidenceVaultCompositeGroupLifecycleError,
    attest_active_composite_group,
    build_composite_group_reopen_artifact,
    build_composite_group_reopen_operational_packet,
    build_composite_group_reopen_source_candidate,
    build_composite_group_reopen_source_resolution,
    validate_composite_group_reopen_artifact,
    validate_composite_group_reopen_source_resolution,
)
from src.services.evidence_vault_exact_relation_supplement import (
    build_exact_relation_source_candidate,
    build_exact_relation_source_resolution,
    build_exact_relation_supplement_artifact,
)
from src.services.evidence_vault_incremental_refresh import (
    build_incremental_evidence_delta,
)
from src.services.evidence_vault_operational_authority import (
    EvidenceVaultOperationalAuthorityError,
    build_operational_adoption_event,
    project_adopted_operational_memory,
)
from src.services.evidence_vault_operational_memory import (
    build_operational_memory_packet,
)
from src.services.evidence_vault_operational_review import (
    build_reviewed_operational_source,
)
from src.services.evidence_vault_operational_scoring import (
    build_operational_score_evaluation,
)


_ROOT = Path(__file__).parents[1]
_AUDIT = _ROOT / "audits/evidence_vault_field_validation_v1"
_FIXTURE = _ROOT / "fixtures/evidence_vault_field_validation_v1"
_REOPEN_EVENT_ID = "c8e1a183-1da7-59e8-abda-8837d8031232"


def test_active_c7_group_attestation_rederives_exact_all_of_authority() -> None:
    current, exact_record, group, _pack = _accepted_c7_state()

    attestation = attest_active_composite_group(
        current_operational_memory=current,
        exact_source_record=exact_record,
    )

    assert attestation["brand_identity"] == current["brand_identity"]
    assert attestation["canonical_memory_version"] == current[
        "canonical_memory_version"
    ]
    assert attestation["group_id"] == group["group_id"]
    assert attestation["decision_rule"] == "all_of"
    assert attestation["member_channel_roles"] == [
        "external_social_profile",
        "owned_web",
    ]
    assert attestation["member_relation_ids"] == sorted(
        row["relation_id"] for row in group["relations"]
    )


@pytest.mark.parametrize("member_index", [0, 1])
def test_material_change_reopens_whole_c7_group_and_suppresses_score(
    member_index: int,
) -> None:
    current, exact_record, group, pack = _accepted_c7_state()
    delta = _member_change_delta(group, pack, member_index=member_index)

    artifact = build_composite_group_reopen_artifact(
        current_operational_memory=current,
        exact_source_record=exact_record,
        reopen_event_id=_REOPEN_EVENT_ID,
        evidence_delta=delta,
    )
    assert artifact["trigger"]["affected_member_evidence_fingerprints"] == [
        group["relations"][member_index]["evidence_fingerprint"]
    ]
    assert artifact["current_group_lifecycle_state"] == "pending_reassessment"
    assert artifact["score_eligible"] is False

    source = build_composite_group_reopen_source_candidate(
        artifact,
        current_operational_memory=current,
    )
    c7_source = next(
        row for row in source["candidate_tiles"] if row["tile_id"] == "C7"
    )
    old_relation_ids = {
        row["relation_id"] for row in group["relations"]
    }
    assert old_relation_ids.isdisjoint(
        {row["relation_id"] for row in c7_source["basis"]}
    )
    invalidations = [
        row
        for row in c7_source["basis"]
        if row["polarity"] == "invalidates_candidate"
    ]
    assert len(invalidations) == 2
    assert {row["claim_id"] for row in invalidations} == {group["group_id"]}
    assert {row["decision_event_id"] for row in invalidations} == {
        _REOPEN_EVENT_ID
    }
    assert c7_source["delta_kind"] == "verified_deprecation"

    operational = build_composite_group_reopen_operational_packet(
        artifact,
        source_candidate_packet=source,
        current_operational_memory=current,
    )
    assert operational["reopened_tile_ids"] == ["C7"]
    assert operational["reopen_policy_fingerprint"] == artifact[
        "artifact_fingerprint"
    ]
    assert operational["has_accepted_change"] is True
    assert "C7" not in {
        row["tile_id"] for row in operational["accepted_memory"]["accepted_tiles"]
    }
    c7_projection = next(
        row
        for row in operational["scoring_projection"]["tiles"]
        if row["tile_id"] == "C7"
    )
    assert c7_projection["canonical_effective_points"] == 0
    assert c7_projection["score_eligible"] is False
    assert c7_projection["lifecycle_state"] == "superseded"
    assert operational["scoring_projection"]["coverage"][
        "pending_change_tile_count"
    ] == 1
    assert operational["scoring_projection"]["coverage"][
        "canonical_score_status"
    ] == "pending_reassessment"

    event = build_operational_adoption_event(
        operational,
        event_id="55ad9724-e936-59bf-a4ba-b6b69f8f70ef",
        sequence=current["adoption_sequence"] + 1,
        previous_event_id=current["adoption_event_id"],
        adopted_by="policy",
        actor_id="composite-group-reopen-policy-v1",
        policy_fingerprint=artifact["artifact_fingerprint"],
        created_at="2026-08-07T14:00:00+00:00",
        idempotency_key_hash=_digest(f"reopen-{member_index}"),
        expected_current_canonical_memory_version=current[
            "canonical_memory_version"
        ],
    )
    reopened_memory = project_adopted_operational_memory(operational, event)
    before = build_operational_score_evaluation(
        current,
        created_at="2026-08-07T13:59:00+00:00",
    )
    after = build_operational_score_evaluation(
        reopened_memory,
        created_at="2026-08-07T14:00:00+00:00",
    )
    assert after["score"] == before["score"] - 2
    assert after["authority_coverage"]["accepted_tile_count"] == (
        before["authority_coverage"]["accepted_tile_count"] - 1
    )
    assert after["authority_coverage"]["pending_change_tile_count"] == 1
    assert after["authority_coverage"]["canonical_score_status"] == (
        "pending_reassessment"
    )
    assert reopened_memory["content"]["pending_reassessments"] == [
        {
            "tile_id": "C7",
            "lifecycle_state": "pending_reassessment",
            "reopen_policy_fingerprint": artifact["artifact_fingerprint"],
            "prior_group_id": artifact["group_id"],
            "trigger_fingerprint": artifact["trigger"]["trigger_fingerprint"],
            "superseded_member_evidence_fingerprints": artifact["trigger"][
                "affected_member_evidence_fingerprints"
            ],
        }
    ]


def test_pending_reassessment_survives_unrelated_packet_projection() -> None:
    current, exact_record, group, pack = _accepted_c7_state()
    artifact = build_composite_group_reopen_artifact(
        current_operational_memory=current,
        exact_source_record=exact_record,
        reopen_event_id=_REOPEN_EVENT_ID,
        evidence_delta=_member_change_delta(group, pack, member_index=0),
    )
    source = build_composite_group_reopen_source_candidate(
        artifact,
        current_operational_memory=current,
    )
    reopen_packet = build_composite_group_reopen_operational_packet(
        artifact,
        source_candidate_packet=source,
        current_operational_memory=current,
    )
    event = build_operational_adoption_event(
        reopen_packet,
        event_id="55ad9724-e936-59bf-a4ba-b6b69f8f70ef",
        sequence=current["adoption_sequence"] + 1,
        previous_event_id=current["adoption_event_id"],
        adopted_by="policy",
        actor_id="composite-group-reopen-policy-v1",
        policy_fingerprint=artifact["artifact_fingerprint"],
        created_at="2026-08-07T14:00:00+00:00",
        idempotency_key_hash=_digest("reopen-carry"),
        expected_current_canonical_memory_version=current[
            "canonical_memory_version"
        ],
    )
    reopened = project_adopted_operational_memory(reopen_packet, event)
    accepted_by_id = {
        row["tile_id"]: row for row in reopened["content"]["accepted_tiles"]
    }
    candidates = [
        build_candidate_tile(
            tile_id=contract["tile_id"],
            basis=(accepted_by_id.get(contract["tile_id"]) or {}).get("basis")
            or [],
            coverage_refs=(
                accepted_by_id.get(contract["tile_id"]) or {}
            ).get("coverage_refs")
            or [],
            unresolved_refs=(
                accepted_by_id.get(contract["tile_id"]) or {}
            ).get("unresolved_refs")
            or [],
        )
        for contract in build_tile_contract_registry()["tiles"]
    ]
    carried = build_operational_memory_packet(
        brand_identity="causaprima.ai",
        source_candidate_packet_fingerprint=_digest("unrelated-source"),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        current_accepted_tiles=reopened["content"]["accepted_tiles"],
        parent_canonical_memory_version=reopened["canonical_memory_version"],
        current_pending_reassessments=reopened["content"][
            "pending_reassessments"
        ],
    )
    assert carried["has_accepted_change"] is False
    assert carried["accepted_memory"]["pending_reassessments"] == reopened[
        "content"
    ]["pending_reassessments"]
    assert carried["scoring_projection"]["coverage"][
        "pending_change_tile_count"
    ] == 1
    assert carried["scoring_projection"]["coverage"][
        "canonical_score_status"
    ] == "pending_reassessment"


def test_not_reacquired_is_coverage_only_and_cannot_reopen_c7() -> None:
    current, exact_record, group, pack = _accepted_c7_state()
    rows = _group_evidence_rows(group, pack)
    delta = build_incremental_evidence_delta(
        subject_url="https://causaprima.ai",
        current_evidence_records=[rows[1]],
        previous_capture_evidence_records=rows,
        known_evidence_records=rows,
    )
    assert delta["not_reacquired_evidence_fingerprints"] == [
        group["relations"][0]["evidence_fingerprint"]
    ]
    assert delta["superseded_evidence_fingerprints"] == []
    assert delta["coverage_loss_only"] is True

    with pytest.raises(
        EvidenceVaultCompositeGroupLifecycleError,
        match="coverage loss cannot reopen",
    ):
        build_composite_group_reopen_artifact(
            current_operational_memory=current,
            exact_source_record=exact_record,
            reopen_event_id=_REOPEN_EVENT_ID,
            evidence_delta=delta,
        )


def test_identical_or_unrelated_change_does_not_reopen_c7() -> None:
    current, exact_record, group, pack = _accepted_c7_state()
    rows = _group_evidence_rows(group, pack)
    identical = build_incremental_evidence_delta(
        subject_url="https://causaprima.ai",
        current_evidence_records=rows,
        previous_capture_evidence_records=rows,
        known_evidence_records=rows,
    )
    with pytest.raises(
        EvidenceVaultCompositeGroupLifecycleError,
        match="does not materially supersede",
    ):
        build_composite_group_reopen_artifact(
            current_operational_memory=current,
            exact_source_record=exact_record,
            reopen_event_id=_REOPEN_EVENT_ID,
            evidence_delta=identical,
        )

    unrelated_old = {
        "ref": "unrelated",
        "source": "web",
        "evidence_type": "raw_input",
        "content": "Unrelated stable content",
        "url": "https://causaprima.ai/unrelated",
        "metadata": {"source_class": "owned_copy"},
    }
    unrelated_new = {**unrelated_old, "content": "Unrelated modified content"}
    unrelated = build_incremental_evidence_delta(
        subject_url="https://causaprima.ai",
        current_evidence_records=[*rows, unrelated_new],
        previous_capture_evidence_records=[*rows, unrelated_old],
        known_evidence_records=[*rows, unrelated_old],
    )
    with pytest.raises(
        EvidenceVaultCompositeGroupLifecycleError,
        match="does not materially supersede",
    ):
        build_composite_group_reopen_artifact(
            current_operational_memory=current,
            exact_source_record=exact_record,
            reopen_event_id=_REOPEN_EVENT_ID,
            evidence_delta=unrelated,
        )


def test_accepted_contradiction_reopens_but_reject_does_not() -> None:
    current, exact_record, group, _ = _accepted_c7_state()
    trigger = {
        "kind": "accepted_contradiction",
        "source_candidate_packet_fingerprint": _digest("contradiction-source"),
        "relation_id": _digest("contradiction-relation"),
        "tile_id": "C7",
        "evidence_id": _digest("contradiction-evidence"),
        "source_identity_id": group["relations"][0]["source_identity_id"],
        "decision_event_id": "relation-review-event",
        "review_request_fingerprint": _digest("contradiction-review"),
        "decision": "accept",
        "polarity": "contradicts",
    }
    artifact = build_composite_group_reopen_artifact(
        current_operational_memory=current,
        exact_source_record=exact_record,
        reopen_event_id=_REOPEN_EVENT_ID,
        accepted_contradiction=trigger,
    )
    assert artifact["trigger"]["kind"] == "accepted_contradiction"
    assert artifact["trigger"][
        "affected_member_evidence_fingerprints"
    ] == [group["relations"][0]["evidence_fingerprint"]]
    assert artifact["group_id"] == group["group_id"]

    tampered = deepcopy(artifact)
    tampered["trigger"]["affected_member_evidence_fingerprints"] = [
        group["relations"][1]["evidence_fingerprint"]
    ]
    trigger_unsigned = {
        key: value
        for key, value in tampered["trigger"].items()
        if key != "trigger_fingerprint"
    }
    tampered["trigger"]["trigger_fingerprint"] = canonical_fingerprint(
        "evidence-vault-composite-group-accepted-contradiction-trigger-v1",
        trigger_unsigned,
    )
    artifact_unsigned = {
        key: value
        for key, value in tampered.items()
        if key != "artifact_fingerprint"
    }
    tampered["artifact_fingerprint"] = canonical_fingerprint(
        tampered["schema_version"],
        artifact_unsigned,
    )
    with pytest.raises(
        EvidenceVaultCompositeGroupLifecycleError,
        match="member binding drifted",
    ):
        validate_composite_group_reopen_artifact(
            tampered,
            current_operational_memory=current,
            exact_source_record=exact_record,
        )

    unrelated = {
        **trigger,
        "source_identity_id": _digest("unrelated-source-identity"),
    }
    with pytest.raises(
        EvidenceVaultCompositeGroupLifecycleError,
        match="exactly one group member",
    ):
        build_composite_group_reopen_artifact(
            current_operational_memory=current,
            exact_source_record=exact_record,
            reopen_event_id=_REOPEN_EVENT_ID,
            accepted_contradiction=unrelated,
        )

    rejected = {**trigger, "decision": "reject"}
    with pytest.raises(
        EvidenceVaultCompositeGroupLifecycleError,
        match="cannot reopen",
    ):
        build_composite_group_reopen_artifact(
            current_operational_memory=current,
            exact_source_record=exact_record,
            reopen_event_id=_REOPEN_EVENT_ID,
            accepted_contradiction=rejected,
        )


def test_partial_or_drifted_group_authority_fails_closed() -> None:
    current, exact_record, group, pack = _accepted_c7_state()
    tampered = deepcopy(current)
    c7 = next(
        row for row in tampered["content"]["accepted_tiles"] if row["tile_id"] == "C7"
    )
    c7["basis"] = c7["basis"][:-1]
    # The canonical version is deliberately left unchanged: both fingerprint
    # and exact group re-derivation must fail closed.
    with pytest.raises(
        EvidenceVaultCompositeGroupLifecycleError,
        match="parent memory is invalid|partial",
    ):
        build_composite_group_reopen_artifact(
            current_operational_memory=tampered,
            exact_source_record=exact_record,
            reopen_event_id=_REOPEN_EVENT_ID,
            evidence_delta=_member_change_delta(group, pack, member_index=0),
        )


def test_reopen_source_resolution_is_immutable_and_replay_deterministic() -> None:
    current, exact_record, group, pack = _accepted_c7_state()
    delta = _member_change_delta(group, pack, member_index=0)
    first = build_composite_group_reopen_artifact(
        current_operational_memory=current,
        exact_source_record=exact_record,
        reopen_event_id=_REOPEN_EVENT_ID,
        evidence_delta=delta,
    )
    second = build_composite_group_reopen_artifact(
        current_operational_memory=current,
        exact_source_record=exact_record,
        reopen_event_id=_REOPEN_EVENT_ID,
        evidence_delta=deepcopy(delta),
    )
    assert first == second
    source = build_composite_group_reopen_source_candidate(
        first,
        current_operational_memory=current,
    )
    resolution = build_composite_group_reopen_source_resolution(
        first,
        source_candidate_packet=source,
    )
    validate_composite_group_reopen_source_resolution(
        resolution,
        source_candidate_packet=source,
    )
    tampered = deepcopy(resolution)
    tampered["group_id"] = _digest("other-group")
    with pytest.raises(
        EvidenceVaultCompositeGroupLifecycleError,
        match="lineage mismatch",
    ):
        validate_composite_group_reopen_source_resolution(
            tampered,
            source_candidate_packet=source,
        )


def test_replacement_group_cannot_reuse_superseded_member_evidence() -> None:
    current, exact_record, group, pack = _accepted_c7_state()
    reopen_artifact = build_composite_group_reopen_artifact(
        current_operational_memory=current,
        exact_source_record=exact_record,
        reopen_event_id=_REOPEN_EVENT_ID,
        evidence_delta=_member_change_delta(group, pack, member_index=0),
    )
    reopen_source = build_composite_group_reopen_source_candidate(
        reopen_artifact,
        current_operational_memory=current,
    )
    reopen_packet = build_composite_group_reopen_operational_packet(
        reopen_artifact,
        source_candidate_packet=reopen_source,
        current_operational_memory=current,
    )
    reopen_event = build_operational_adoption_event(
        reopen_packet,
        event_id="56ad9724-e936-59bf-a4ba-b6b69f8f70ef",
        sequence=current["adoption_sequence"] + 1,
        previous_event_id=current["adoption_event_id"],
        adopted_by="policy",
        actor_id="composite-group-reopen-policy-v1",
        policy_fingerprint=reopen_artifact["artifact_fingerprint"],
        created_at="2026-08-07T14:00:00+00:00",
        idempotency_key_hash=_digest("stale-replacement-reopen"),
        expected_current_canonical_memory_version=current[
            "canonical_memory_version"
        ],
    )
    reopened = project_adopted_operational_memory(
        reopen_packet,
        reopen_event,
    )

    assessment, rows, _, worksheet_sha = _sources()
    stale_artifact = build_exact_relation_supplement_artifact(
        brand_identity="causaprima.ai",
        subject_url="https://causaprima.ai",
        parent_canonical_memory_version=reopened[
            "canonical_memory_version"
        ],
        selected_tile_ids=["C7"],
        assessment_artifact=assessment,
        assessment_review_rows=rows,
        assessment_worksheet_sha256=worksheet_sha,
        evidence_pack=pack,
    )
    stale_source = build_exact_relation_source_candidate(
        stale_artifact,
        current_operational_memory=reopened,
    )
    stale_pending = [
        relation
        for tile in stale_source["candidate_tiles"]
        for relation in tile["basis"]
        if relation["review_status"] == "unreviewed"
    ]
    reviewed, operational = build_reviewed_operational_source(
        stale_source,
        decisions={
            relation["relation_id"]: {
                "decision": "accept",
                "decision_event_id": f"stale-accept-{index}",
            }
            for index, relation in enumerate(stale_pending)
        },
        current_operational_memory=reopened,
    )
    accepted_by_id = {
        row["tile_id"]: row
        for row in operational["accepted_memory"]["accepted_tiles"]
    }
    pending_by_id = {
        row["tile_id"]: row
        for row in reopened["content"]["pending_reassessments"]
    }
    stale_record = {
        "packet": stale_source,
        "reference_resolution": build_exact_relation_source_resolution(
            stale_artifact,
            source_candidate_packet=stale_source,
        ),
    }
    with pytest.raises(
        EvidenceVaultOperationalAuthorityError,
        match="reuses superseded member evidence",
    ):
        _validate_composite_group_resolution(
            resolved_tile_ids={"C7"},
            pending_reassessments=pending_by_id,
            accepted_by_id=accepted_by_id,
            reviewed_source_packet=reviewed,
            reviewed_source_resolution={
                "schema_version": (
                    "evidence-vault-operational-reviewed-resolution-v1"
                ),
                "source_candidate_packet_fingerprint": stale_source[
                    "candidate_packet_fingerprint"
                ],
            },
            exact_source_record=stale_record,
            expected_parent=reopened["canonical_memory_version"],
        )


def _accepted_c7_state() -> tuple[dict, dict, dict, dict]:
    baseline = _baseline_memory()
    assessment, rows, pack, worksheet_sha = _sources()
    artifact = build_exact_relation_supplement_artifact(
        brand_identity="causaprima.ai",
        subject_url="https://causaprima.ai",
        parent_canonical_memory_version=baseline["canonical_memory_version"],
        selected_tile_ids=["C7", "MG1", "MG3", "MG5"],
        assessment_artifact=assessment,
        assessment_review_rows=rows,
        assessment_worksheet_sha256=worksheet_sha,
        evidence_pack=pack,
    )
    source = build_exact_relation_source_candidate(
        artifact,
        current_operational_memory=baseline,
    )
    pending = [
        relation
        for tile in source["candidate_tiles"]
        for relation in tile["basis"]
        if relation["review_status"] == "unreviewed"
    ]
    decisions = {
        relation["relation_id"]: {
            "decision": "accept",
            "decision_event_id": f"accepted-{index}",
        }
        for index, relation in enumerate(pending)
    }
    reviewed, operational = build_reviewed_operational_source(
        source,
        decisions=decisions,
        current_operational_memory=baseline,
    )
    event = build_operational_adoption_event(
        operational,
        event_id="302fa0cc-236c-5a94-87fb-97e0440e2ab7",
        sequence=2,
        previous_event_id=baseline["adoption_event_id"],
        adopted_by="human",
        actor_id="fixture-reviewer",
        policy_fingerprint=_digest("fixture-exact-review-policy"),
        created_at="2026-08-07T12:00:00+00:00",
        idempotency_key_hash=_digest("fixture-exact-review-adoption"),
        expected_current_canonical_memory_version=baseline[
            "canonical_memory_version"
        ],
    )
    current = project_adopted_operational_memory(operational, event)
    exact_record = {
        "packet": source,
        "reference_resolution": build_exact_relation_source_resolution(
            artifact,
            source_candidate_packet=source,
        ),
        "authority": False,
        "authority_scope": "b3s-vault",
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
    }
    group = next(row for row in artifact["groups"] if row["tile_id"] == "C7")
    return current, exact_record, group, pack


def _baseline_memory() -> dict:
    basis = {
        "relation_id": _digest("p1-relation"),
        "evidence_id": _digest("p1-evidence"),
        "source_identity_id": _digest("p1-source"),
        "claim_id": None,
        "polarity": "supports",
        "review_status": "accepted",
        "decision_event_id": "review-current-p1",
        "absence_test_contract_id": None,
        "coverage_assessment_id": None,
        "coverage_status": None,
        "tested_scope": None,
        "observed_result": None,
    }
    candidates = [
        build_candidate_tile(
            tile_id=row["tile_id"],
            basis=[basis] if row["tile_id"] == "P1" else [],
        )
        for row in build_tile_contract_registry()["tiles"]
    ]
    packet = build_operational_memory_packet(
        brand_identity="causaprima.ai",
        source_candidate_packet_fingerprint=_digest("current-source"),
        aggregation_policy_fingerprint=canonical_aggregation_policy_fingerprint(),
        candidate_tiles=candidates,
        dispositions={
            "P1": {
                "authority_state": "accepted",
                "review_state": "resolved",
                "authority_profile_id": "human-reviewed-relation-v1",
                "authority_source": "human",
                "decision_event_id": "review-current-p1",
            }
        },
    )
    event = build_operational_adoption_event(
        packet,
        event_id="57b8b70b-e826-5063-970d-e9b0d6659b9b",
        sequence=1,
        previous_event_id=None,
        adopted_by="human",
        actor_id="fixture-reviewer",
        policy_fingerprint=_digest("fixture-policy"),
        created_at="2026-08-07T10:00:00+00:00",
        idempotency_key_hash=_digest("fixture-adoption"),
        expected_current_canonical_memory_version=None,
    )
    return project_adopted_operational_memory(packet, event)


def _member_change_delta(group: dict, pack: dict, *, member_index: int) -> dict:
    rows = _group_evidence_rows(group, pack)
    changed = deepcopy(rows)
    changed[member_index]["content"] += " Materially changed."
    return build_incremental_evidence_delta(
        subject_url="https://causaprima.ai",
        current_evidence_records=changed,
        previous_capture_evidence_records=rows,
        known_evidence_records=rows,
    )


def _group_evidence_rows(group: dict, pack: dict) -> list[dict]:
    by_ref = {row["ref"]: row for row in pack["evidence"]}
    return [deepcopy(by_ref[row["ref"]]) for row in group["relations"]]


def _sources() -> tuple[dict, list[dict[str, str]], dict, str]:
    assessment = json.loads(
        (_AUDIT / "causa-prima-missing-tile-independent-audit.json").read_text(
            encoding="utf-8"
        )
    )
    worksheet = _AUDIT / "causa-prima-missing-tile-independent-review.csv"
    with worksheet.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    pack = json.loads(
        (
            _FIXTURE
            / "causa_prima-a8ba05137817-normalized-evidence-pack.json"
        ).read_text(encoding="utf-8")
    )
    return assessment, rows, pack, hashlib.sha256(worksheet.read_bytes()).hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
