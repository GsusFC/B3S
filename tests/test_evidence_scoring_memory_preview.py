from __future__ import annotations

from copy import deepcopy
import subprocess
import sys

import pytest

from src.evidence_identity import canonical_evidence_digest
from src.history.repository import PostgresHistoryRepository
from src.services.evidence_scoring_memory_preview import (
    EvidenceScoringMemoryPreviewError,
    build_evidence_scoring_memory_preview,
)
from src.services.evidence_scoring_recovery_review import (
    EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION,
    build_reviewed_scoring_memory_shadow,
)
from src.sv9.rubric import COMPONENTS, component_points


def test_preview_module_imports_cleanly_in_a_fresh_process() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.services.evidence_scoring_memory_preview "
                "import build_evidence_scoring_memory_preview"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_prior_literal_evidence_recovers_only_a_later_blind_spot() -> None:
    older = _report(
        "older",
        "2026-07-01T08:00:00Z",
        target_state="ok",
    )
    newer = _report(
        "newer",
        "2026-07-02T08:00:00Z",
        target_state="sin_evidencia",
    )

    preview = build_evidence_scoring_memory_preview([newer, older])

    assert preview["runtime_effect"] is False
    assert preview["authority"] is False
    assert preview["summary"]["accepted_evidence_count"] == 1
    assert preview["summary"]["recovered_blind_spot_count"] == 1
    assert preview["recoveries"] == [
        {
            "component_key": "magnetism",
            "tile_id": "MG1",
            "latest_state": "sin_evidencia",
            "preview_state": "ok",
            "reason": (
                "prior_reproducible_accepted_evidence_recovers_"
                "latest_blind_spot"
            ),
            "tile_evidence_ids": [
                preview["accepted_evidence"][0]["tile_evidence_id"]
            ],
        }
    ]
    assert preview["scoring"] == {
        "status": "preview_available",
        "current_score": 0,
        "recomputed_current_score": 0,
        "preview_score": 2,
        "score_delta": 2,
        "current_score_reproducible": True,
        "magnetism_capped": False,
    }


def test_explicit_latest_negative_retains_history_without_overriding_it() -> None:
    older = _report(
        "older",
        "2026-07-01T08:00:00Z",
        target_state="ok",
    )
    newer = _report(
        "newer",
        "2026-07-02T08:00:00Z",
        target_state="no",
    )

    preview = build_evidence_scoring_memory_preview([older, newer])

    assert preview["summary"]["accepted_evidence_count"] == 1
    assert preview["summary"]["recovered_blind_spot_count"] == 0
    assert preview["summary"]["explicit_negative_conflict_count"] == 1
    assert preview["conflicts"][0]["preview_state"] == "no"
    assert preview["scoring"]["preview_score"] == 0
    assert preview["scoring"]["score_delta"] == 0


def test_non_literal_or_rejected_identity_never_enters_scoring_memory() -> None:
    non_literal = _report(
        "non-literal",
        "2026-07-01T08:00:00Z",
        target_state="ok",
        quote="This sentence is not in the persisted source.",
    )
    later = _report(
        "later",
        "2026-07-02T08:00:00Z",
        target_state="sin_evidencia",
    )

    non_literal_preview = build_evidence_scoring_memory_preview(
        [non_literal, later]
    )

    assert non_literal_preview["accepted_evidence"] == []
    assert non_literal_preview["recoveries"] == []
    assert (
        non_literal_preview["summary"]["excluded_reason_counts"][
            "quote_without_eligible_literal_source"
        ]
        == 1
    )

    accepted = _report(
        "accepted",
        "2026-07-01T08:00:00Z",
        target_state="ok",
    )
    record = accepted["raw"]["flow"]["candidate"]["evidence_pack"][
        "evidence"
    ][0]
    source_evidence_id = canonical_evidence_digest(
        source_class="owned_copy",
        evidence_type=record["evidence_type"],
        url=record["url"],
        content=record["content"],
    )
    rejected_preview = build_evidence_scoring_memory_preview(
        [accepted, later],
        evidence_adjudications=[
            {
                "id": "review-1",
                    "subject_type": "evidence",
                "subject_id": source_evidence_id,
                "sequence": 1,
                "decision": "rejected",
                "created_at": "2026-07-02T09:00:00Z",
            }
        ],
    )

    assert rejected_preview["accepted_evidence"] == []
    assert rejected_preview["recoveries"] == []


def test_evaluator_repeat_does_not_change_memory_version_or_score() -> None:
    older = _report(
        "older",
        "2026-07-01T08:00:00Z",
        target_state="ok",
    )
    repeat = deepcopy(older)
    repeat["id"] = "repeat"
    repeat["created_at"] = "2026-07-01T09:00:00Z"
    later = _report(
        "later",
        "2026-07-02T08:00:00Z",
        target_state="sin_evidencia",
    )

    without_repeat = build_evidence_scoring_memory_preview([older, later])
    with_repeat = build_evidence_scoring_memory_preview(
        [repeat, later, older]
    )

    assert (
        without_repeat["memory_version"]
        == with_repeat["memory_version"]
    )
    assert (
        without_repeat["scoring"]
        == with_repeat["scoring"]
    )
    assert (
        with_repeat["accepted_evidence"][0]["observation_count"]
        == 2
    )


def test_revoked_review_falls_back_to_deterministic_identity() -> None:
    older = _report(
        "older",
        "2026-07-01T08:00:00Z",
        target_state="ok",
    )
    later = _report(
        "later",
        "2026-07-02T08:00:00Z",
        target_state="sin_evidencia",
    )
    record = older["raw"]["flow"]["candidate"]["evidence_pack"][
        "evidence"
    ][0]
    source_evidence_id = canonical_evidence_digest(
        source_class="owned_copy",
        evidence_type=record["evidence_type"],
        url=record["url"],
        content=record["content"],
    )

    preview = build_evidence_scoring_memory_preview(
        [older, later],
        evidence_adjudications=[
            {
                "id": "review-1",
                "subject_type": "evidence",
                "subject_id": source_evidence_id,
                "sequence": 1,
                "decision": "accepted",
                "created_at": "2026-07-02T09:00:00Z",
            },
            {
                "id": "review-2",
                "subject_type": "evidence",
                "subject_id": source_evidence_id,
                "sequence": 2,
                "decision": "revoked",
                "created_at": "2026-07-02T10:00:00Z",
            },
        ],
    )

    assert preview["summary"]["accepted_evidence_count"] == 1
    assert preview["summary"]["recovered_blind_spot_count"] == 1
    assert preview["scoring"]["score_delta"] == 2


def test_rubric_mismatch_is_retained_but_not_reused() -> None:
    older = _report(
        "older",
        "2026-07-01T08:00:00Z",
        target_state="ok",
        rubric_version="baldosas-v0",
    )
    later = _report(
        "later",
        "2026-07-02T08:00:00Z",
        target_state="sin_evidencia",
    )

    preview = build_evidence_scoring_memory_preview([older, later])

    assert preview["summary"]["accepted_evidence_count"] == 1
    assert preview["recoveries"] == []
    assert preview["scoring"]["score_delta"] == 0


def test_reports_from_different_domains_are_rejected() -> None:
    first = _report(
        "first",
        "2026-07-01T08:00:00Z",
        target_state="ok",
    )
    second = _report(
        "second",
        "2026-07-02T08:00:00Z",
        target_state="sin_evidencia",
    )
    second["url"] = "https://other.test"

    with pytest.raises(
        EvidenceScoringMemoryPreviewError,
        match="exactly one canonical domain",
    ):
        build_evidence_scoring_memory_preview([first, second])


def test_repository_preview_rebuilds_from_durable_inputs(
    monkeypatch,
) -> None:
    reports = [
        _report(
            "older",
            "2026-07-01T08:00:00Z",
            target_state="ok",
        ),
        _report(
            "newer",
            "2026-07-02T08:00:00Z",
            target_state="sin_evidencia",
        ),
    ]
    repository = PostgresHistoryRepository(
        "postgresql://example.test/b3s"
    )
    monkeypatch.setattr(
        repository,
        "list_report_payloads_for_domain",
        lambda *_args, **_kwargs: deepcopy(reports),
    )
    monkeypatch.setattr(
        repository,
        "list_current_evidence_memory_adjudications",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        repository,
        "list_current_evidence_scoring_recovery_reviews",
        lambda *_args, **_kwargs: [],
    )

    first = repository.get_evidence_scoring_memory_preview(
        "example.com"
    )
    second = repository.get_evidence_scoring_memory_preview(
        "https://www.example.com"
    )

    assert first is not None
    assert second is not None
    assert first["memory_version"] == second["memory_version"]
    assert first["state_fingerprint"] == second["state_fingerprint"]
    assert first["scoring"]["score_delta"] == 2
    assert first["reviewed_shadow"]["scoring"]["score_delta"] == 0
    assert first["recovery_review"]["summary"]["pending_count"] == 1


def test_repository_preview_applies_current_review_after_restart(
    monkeypatch,
) -> None:
    reports = [
        _report(
            "older",
            "2026-07-01T08:00:00Z",
            target_state="ok",
        ),
        _report(
            "newer",
            "2026-07-02T08:00:00Z",
            target_state="sin_evidencia",
        ),
    ]
    candidate = build_reviewed_scoring_memory_shadow(reports)[
        "recovery_review_candidates"
    ][0]
    current_event = _durable_current_review_event(
        candidate,
        decision="accepted",
        sequence=2,
    )
    repository = PostgresHistoryRepository(
        "postgresql://example.test/b3s"
    )
    monkeypatch.setattr(
        repository,
        "list_report_payloads_for_domain",
        lambda *_args, **_kwargs: deepcopy(reports),
    )
    monkeypatch.setattr(
        repository,
        "list_current_evidence_memory_adjudications",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        repository,
        "list_current_evidence_scoring_recovery_reviews",
        lambda *_args, **_kwargs: [deepcopy(current_event)],
    )

    rebuilt = repository.get_evidence_scoring_memory_preview(
        "example.com"
    )

    assert rebuilt is not None
    assert rebuilt["scoring"]["score_delta"] == 2
    assert rebuilt["reviewed_shadow"]["scoring"]["score_delta"] == 2
    assert rebuilt["recovery_review"]["review_event_mode"] == (
        "current_projection"
    )
    assert rebuilt["recovery_review"]["summary"]["accepted_count"] == 1
    assert rebuilt["recovery_review"]["journal"] == {
        "stored_event_count": 1,
        "applicable_event_count": 1,
        "stale_event_count": 0,
        "stale_event_ids": [],
    }


def test_repository_preview_revocation_and_stale_events_fail_closed(
    monkeypatch,
) -> None:
    reports = [
        _report(
            "older",
            "2026-07-01T08:00:00Z",
            target_state="ok",
        ),
        _report(
            "newer",
            "2026-07-02T08:00:00Z",
            target_state="sin_evidencia",
        ),
    ]
    candidate = build_reviewed_scoring_memory_shadow(reports)[
        "recovery_review_candidates"
    ][0]
    revoked = _durable_current_review_event(
        candidate,
        decision="revoked",
        sequence=2,
    )
    stale = {
        **_durable_current_review_event(
            candidate,
            decision="accepted",
            sequence=1,
        ),
        "event_id": "00000000-0000-0000-0000-000000000099",
        "id": "00000000-0000-0000-0000-000000000099",
        "case_id": "scoring-recovery-stale-case",
    }
    repository = PostgresHistoryRepository(
        "postgresql://example.test/b3s"
    )
    monkeypatch.setattr(
        repository,
        "list_report_payloads_for_domain",
        lambda *_args, **_kwargs: deepcopy(reports),
    )
    monkeypatch.setattr(
        repository,
        "list_current_evidence_memory_adjudications",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        repository,
        "list_current_evidence_scoring_recovery_reviews",
        lambda *_args, **_kwargs: [deepcopy(revoked), deepcopy(stale)],
    )

    rebuilt = repository.get_evidence_scoring_memory_preview(
        "example.com"
    )

    assert rebuilt is not None
    assert rebuilt["scoring"]["score_delta"] == 2
    assert rebuilt["reviewed_shadow"]["scoring"]["score_delta"] == 0
    assert rebuilt["recovery_review"]["summary"]["revoked_count"] == 1
    assert rebuilt["recovery_review"]["summary"]["pending_count"] == 1
    assert rebuilt["recovery_review"]["journal"][
        "stale_event_ids"
    ] == [stale["event_id"]]


def _durable_current_review_event(
    candidate: dict,
    *,
    decision: str,
    sequence: int,
) -> dict:
    event_id = (
        "00000000-0000-0000-0000-"
        f"{sequence:012d}"
    )
    previous_event_id = (
        None
        if sequence == 1
        else "00000000-0000-0000-0000-000000000001"
    )
    return {
        "id": event_id,
        "event_id": event_id,
        "subject_type": "scoring_recovery",
        "subject_id": candidate["candidate_fingerprint"],
        "case_id": candidate["case_id"],
        "candidate_fingerprint": candidate[
            "candidate_fingerprint"
        ],
        "sequence": sequence,
        "decision": decision,
        "effective_state": decision,
        "supersedes_event_id": previous_event_id,
        "previous_event_id": previous_event_id,
        "schema_version": (
            EVIDENCE_SCORING_RECOVERY_REVIEW_EVENT_VERSION
        ),
        "policy_version": (
            "evidence-scoring-recovery-review-policy-v1"
        ),
        "evaluator_version": "manual-review-v1",
        "reviewer": "gsus",
        "reviewer_id": "gsus",
        "actor_id": "gsus",
        "reason_code": "tile_contract_reviewed",
        "rationale": "The semantic tile contract was reviewed.",
        "runtime_effect": False,
        "authority": False,
        "automatic_scoring_effect": False,
        "created_at": "2026-07-29T15:00:00+00:00",
        "reviewed_at": "2026-07-29T15:00:00+00:00",
    }


def _report(
    report_id: str,
    created_at: str,
    *,
    target_state: str,
    quote: str = "The signal survived the next acquisition.",
    rubric_version: str = "baldosas-v3-1",
) -> dict:
    source_text = "The signal survived the next acquisition."
    evidence_record = {
        "ref": "web.home.0",
        "source": "web",
        "evidence_type": "raw_input",
        "content": source_text,
        "url": "https://example.com",
        "confidence": "high",
        "metadata": {"source_class": "owned_copy"},
    }
    components: dict[str, dict] = {}
    current_score = 0
    for component_key, spec in COMPONENTS.items():
        profile = []
        for tile in spec["tiles"]:
            state = (
                target_state
                if component_key == "magnetism"
                and tile["id"] == "MG1"
                else "no"
            )
            profile.append(
                {
                    "id": tile["id"],
                    "estado": state,
                    "evidencia": (
                        quote if state == "ok" else ""
                    ),
                    "motivo": (
                        "" if state == "ok" else "not present"
                    ),
                    "contexto_requerido": "",
                }
            )
        lit = sum(
            1 for verdict in profile if verdict["estado"] == "ok"
        )
        current_score += component_points(component_key, lit)
        components[component_key] = {
            "component": component_key,
            "status": "scored",
            "score": lit,
            "tile_profile": profile,
        }
    return {
        "id": report_id,
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": created_at,
        "score": current_score,
        "raw": {
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "evidence": [evidence_record]
                    }
                }
            },
            "sv9": {
                "result": {
                    "rubric_version": rubric_version,
                    "brand3_score": current_score,
                    "components": components,
                }
            },
        },
    }
