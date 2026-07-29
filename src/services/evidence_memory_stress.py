"""Adversarial replay harness for the longitudinal evidence-memory direction.

The harness is deliberately read-only. It tests properties that the current
evidence ledger can already guarantee and reports explicit promotion blockers
for claims, reviewer identity, tiles, and memory-based scoring that do not
exist yet.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from src.evidence_identity import canonical_evidence_digest
from src.external_identity_provenance import (
    build_external_identity_provenance,
)
from src.services.evidence_claim_memory import build_evidence_claim_memory
from src.services.evidence_memory_identity_v2 import (
    build_evidence_memory_identity_v2,
)
from src.services.evidence_ledger_shadow import build_evidence_ledger_shadow
from src.services.scanner_evidence_comparison import canonical_evidence_records


EVIDENCE_MEMORY_STRESS_VERSION = "evidence-memory-stress-v2"
EVIDENCE_MEMORY_STRESS_POLICY_VERSION = "evidence-memory-stress-policy-v3"
_CURRENT_STATES = {"observed", "repeated", "validation_candidate"}


def run_evidence_memory_stress(
    histories: Mapping[str, Iterable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run controlled adversarial probes plus replays over real histories."""

    normalized_histories = {
        str(domain): _ordered_reports(reports)
        for domain, reports in (histories or {}).items()
        if str(domain).strip()
    }
    controlled = _controlled_probes()
    replay = _replay_real_histories(normalized_histories)
    identity_v2_replay = _replay_identity_v2(normalized_histories)
    claim_memory_replay = _replay_claim_memory(normalized_histories)
    executable_failures = [
        probe["id"]
        for probe in controlled
        if probe["kind"] == "executable_invariant" and probe["status"] == "fail"
    ]
    for check in replay["checks"]:
        if check["status"] == "fail":
            executable_failures.append(f"real_history:{check['id']}")
    promotion_blockers = [
        probe["id"]
        for probe in controlled
        if probe["status"] == "blocked"
    ]

    if executable_failures:
        verdict = "foundation_rejected"
    elif promotion_blockers:
        verdict = "foundation_supported_promotion_blocked"
    else:
        verdict = "promotion_ready"

    return {
        "schema_version": EVIDENCE_MEMORY_STRESS_VERSION,
        "policy_version": EVIDENCE_MEMORY_STRESS_POLICY_VERSION,
        "mutates_state": False,
        "verdict": verdict,
        "promotion_ready": verdict == "promotion_ready",
        "summary": {
            "controlled_probe_count": len(controlled),
            "executable_invariant_count": sum(
                1 for probe in controlled if probe["kind"] == "executable_invariant"
            ),
            "executable_failure_count": len(executable_failures),
            "promotion_blocker_count": len(promotion_blockers),
            "real_history_count": replay["history_count"],
            "real_history_with_material_evidence_count": replay[
                "history_with_material_evidence_count"
            ],
            "v1_changed_candidate_count": identity_v2_replay["summary"][
                "v1_changed_candidate_count"
            ],
            "v2_revision_candidate_count": identity_v2_replay["summary"][
                "v2_revision_candidate_count"
            ],
            "v2_removed_url_only_change_pressure_count": identity_v2_replay[
                "summary"
            ]["removed_url_only_change_pressure_count"],
            "claim_slot_count": claim_memory_replay["summary"][
                "claim_slot_count"
            ],
            "claim_variant_count": claim_memory_replay["summary"][
                "claim_variant_count"
            ],
            "claim_relation_candidate_count": claim_memory_replay["summary"][
                "relation_candidate_count"
            ],
        },
        "executable_failures": executable_failures,
        "promotion_blockers": promotion_blockers,
        "controlled_probes": controlled,
        "real_history_replay": replay,
        "identity_v2_replay": identity_v2_replay,
        "claim_memory_replay": claim_memory_replay,
        "interpretation": {
            "supported": (
                "Evidence identities can be remembered deterministically across "
                "acquisition loss, evaluator drift, ordering changes, and exact repeats."
            ),
            "not_yet_supported": (
                "Claim Memory can only propose coexistence or replacement relations; "
                "it cannot adjudicate a canonical claim, map stable evidence to tiles, "
                "complete pending human identity reviews, or produce a versioned "
                "memory score."
            ),
        },
    }


def render_evidence_memory_stress_markdown(report: dict[str, Any]) -> str:
    """Render a concise human-reviewable stress report."""

    summary = report.get("summary") or {}
    lines = [
        "# Evidence memory stress report",
        "",
        f"- Verdict: `{report.get('verdict', 'unknown')}`",
        f"- Mutates state: `{str(bool(report.get('mutates_state'))).lower()}`",
        f"- Promotion ready: `{str(bool(report.get('promotion_ready'))).lower()}`",
        f"- Executable failures: `{summary.get('executable_failure_count', 0)}`",
        f"- Promotion blockers: `{summary.get('promotion_blocker_count', 0)}`",
        f"- Real histories replayed: `{summary.get('real_history_count', 0)}`",
        "",
        "## Controlled probes",
        "",
        "| probe | kind | status | observation |",
        "| --- | --- | --- | --- |",
    ]
    for probe in report.get("controlled_probes") or []:
        lines.append(
            "| "
            + " | ".join(
                (
                    _md_cell(str(probe.get("id") or "")),
                    _md_cell(str(probe.get("kind") or "")),
                    _md_cell(str(probe.get("status") or "")),
                    _md_cell(str(probe.get("observation") or "")),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Real-history replay",
            "",
            "| check | status | passed | total | rate |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    replay = report.get("real_history_replay") or {}
    for check in replay.get("checks") or []:
        lines.append(
            "| "
            + " | ".join(
                (
                    _md_cell(str(check.get("id") or "")),
                    _md_cell(str(check.get("status") or "")),
                    str(check.get("passed", 0)),
                    str(check.get("total", 0)),
                    f"{float(check.get('rate') or 0.0):.4f}",
                )
            )
            + " |"
        )
    diagnostics = replay.get("histories") or []
    if diagnostics:
        lines.extend(
            [
                "",
                "## Locator pressure",
                "",
                "| domain | reports | score range | identities | locators | multi-variant locators | affected identities | affected rate |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for diagnostic in sorted(
            diagnostics,
            key=lambda item: (
                float(item.get("multi_variant_entry_rate") or 0.0),
                int(item.get("evidence_identity_count") or 0),
            ),
            reverse=True,
        ):
            score_range = diagnostic.get("score_range")
            lines.append(
                "| "
                + " | ".join(
                    (
                        _md_cell(str(diagnostic.get("domain") or "")),
                        str(diagnostic.get("report_count", 0)),
                        "n/a" if score_range is None else f"{float(score_range):.2f}",
                        str(diagnostic.get("evidence_identity_count", 0)),
                        str(diagnostic.get("locator_count", 0)),
                        str(diagnostic.get("multi_variant_locator_count", 0)),
                        str(diagnostic.get("entries_in_multi_variant_locators", 0)),
                        f"{float(diagnostic.get('multi_variant_entry_rate') or 0.0):.4f}",
                    )
                )
                + " |"
            )
    identity_v2 = report.get("identity_v2_replay") or {}
    identity_v2_summary = identity_v2.get("summary") or {}
    lines.extend(
        [
            "",
            "## Identity v2 comparison",
            "",
            f"- V1 changed candidates: `{identity_v2_summary.get('v1_changed_candidate_count', 0)}`",
            f"- V2 stable-slot revision candidates: `{identity_v2_summary.get('v2_revision_candidate_count', 0)}`",
            f"- V1 locator-change candidates suppressed by v2: `{identity_v2_summary.get('removed_url_only_change_pressure_count', 0)}`",
            f"- V1 candidates retained as stable-slot revisions: `{identity_v2_summary.get('retained_explicit_revision_count', 0)}`",
            f"- V2 stable slots: `{identity_v2_summary.get('v2_claim_slot_count', 0)}`",
            f"- V2 stable-slot methods: `{_format_counts(identity_v2_summary.get('v2_claim_slot_method_counts'))}`",
            f"- Current external passages: `{identity_v2_summary.get('v2_current_external_passage_count', 0)}`",
            f"- Current external source clusters: `{identity_v2_summary.get('v2_current_external_cluster_count', 0)}`",
            f"- Confirmed independent external clusters: `{identity_v2_summary.get('v2_current_independent_external_cluster_count', 0)}`",
            "",
            "| domain | v1 changed | v2 revisions | claim slots | external passages | source clusters | confirmed independent |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in identity_v2.get("histories") or []:
        lines.append(
            "| "
            + " | ".join(
                (
                    _md_cell(str(row.get("domain") or "")),
                    str(row.get("v1_changed_candidate_count", 0)),
                    str(row.get("v2_revision_candidate_count", 0)),
                    str(row.get("v2_claim_slot_count", 0)),
                    str(row.get("v2_current_external_passage_count", 0)),
                    str(row.get("v2_current_external_cluster_count", 0)),
                    str(row.get("v2_current_independent_external_cluster_count", 0)),
                )
            )
            + " |"
        )
    claim_memory = report.get("claim_memory_replay") or {}
    claim_summary = claim_memory.get("summary") or {}
    lines.extend(
        [
            "",
            "## Claim Memory v1 replay",
            "",
            f"- Semantic claim slots: `{claim_summary.get('claim_slot_count', 0)}`",
            f"- Claim variants: `{claim_summary.get('claim_variant_count', 0)}`",
            f"- Claim occurrences: `{claim_summary.get('claim_occurrence_count', 0)}`",
            f"- Proposed relations: `{claim_summary.get('relation_candidate_count', 0)}`",
            f"- Ignored bare claim IDs: `{claim_summary.get('ignored_bare_claim_id_count', 0)}`",
            f"- Ignored structural metadata rows: `{claim_summary.get('ignored_claim_metadata_count', 0)}`",
            "",
            "| domain | reports | slots | variants | occurrences | relations | ignored metadata |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in claim_memory.get("histories") or []:
        lines.append(
            "| "
            + " | ".join(
                (
                    _md_cell(str(row.get("domain") or "")),
                    str(row.get("report_count", 0)),
                    str(row.get("claim_slot_count", 0)),
                    str(row.get("claim_variant_count", 0)),
                    str(row.get("claim_occurrence_count", 0)),
                    str(row.get("relation_candidate_count", 0)),
                    str(row.get("ignored_claim_metadata_count", 0)),
                )
            )
            + " |"
        )
    if report.get("promotion_blockers"):
        lines.extend(["", "## Promotion blockers", ""])
        lines.extend(f"- `{blocker}`" for blocker in report["promotion_blockers"])
    return "\n".join(lines) + "\n"


def _controlled_probes() -> list[dict[str, Any]]:
    stable_owned = _evidence(
        ref="web.0",
        source="web",
        source_class="owned_copy",
        evidence_type="raw_input",
        url="https://example.com/about",
        content="We help finance teams close their books faster.",
    )
    first = _report("one", "2026-01-01T00:00:00Z", [stable_owned])
    repeat = _report("two", "2026-01-02T00:00:00Z", [deepcopy(stable_owned)])
    baseline_history = [first, repeat]
    baseline = build_evidence_ledger_shadow(baseline_history, mode="shadow")
    baseline_identities = _identity_set(baseline)

    reversed_ledger = build_evidence_ledger_shadow(
        list(reversed(baseline_history)),
        mode="shadow",
    )
    order_pass = baseline["state_fingerprint"] == reversed_ledger["state_fingerprint"]

    dropout_report = _report("dropout", "2026-01-03T00:00:00Z", [])
    dropout_report["acquisition_gate"] = {
        "state": "warning",
        "warnings": [{"code": "provider_empty_result"}],
    }
    dropout = build_evidence_ledger_shadow(
        [*baseline_history, dropout_report],
        mode="shadow",
    )
    dropout_pass = (
        _identity_set(dropout) == baseline_identities
        and all(
            entry["state"] == "not_reacquired"
            for entry in dropout["entries"]
        )
    )

    evaluator_drift = deepcopy(repeat)
    evaluator_drift["id"] = "evaluation-drift"
    evaluator_drift["created_at"] = "2026-01-03T00:00:00Z"
    evaluator_drift["score"] = 5
    evaluator_drift["components"][0]["score"] = 0
    evaluator_drift["components"][0]["tile_profile"][0]["estado"] = "missing"
    evaluation_ledger = build_evidence_ledger_shadow(
        [*baseline_history, evaluator_drift],
        mode="shadow",
    )
    evaluation_pass = _identity_set(evaluation_ledger) == baseline_identities

    third_repeat = deepcopy(repeat)
    third_repeat["id"] = "three"
    third_repeat["created_at"] = "2026-01-03T00:00:00Z"
    repeated_ledger = build_evidence_ledger_shadow(
        [*baseline_history, third_repeat],
        mode="shadow",
    )
    repetition_pass = (
        _identity_set(repeated_ledger) == baseline_identities
        and _locator_set(repeated_ledger) == _locator_set(baseline)
        and repeated_ledger["entries"][0]["observation_count"] == 3
    )

    changed_owned = deepcopy(stable_owned)
    changed_owned["content"] = "We help operations teams automate procurement."
    change_ledger = build_evidence_ledger_shadow(
        [
            first,
            _report("changed", "2026-01-02T00:00:00Z", [changed_owned]),
        ],
        mode="shadow",
    )
    change_states = Counter(entry["state"] for entry in change_ledger["entries"])
    change_pass = (
        change_states["changed_candidate"] == 1
        and change_states["observed"] == 1
    )

    stale_ledger = build_evidence_ledger_shadow(
        [
            first,
            _report("aged-dropout", "2026-08-01T00:00:00Z", []),
        ],
        mode="shadow",
    )
    staleness_pass = (
        _identity_set(stale_ledger) == _identity_set(
            build_evidence_ledger_shadow([first], mode="shadow")
        )
        and stale_ledger["entries"][0]["state"] == "stale_candidate"
    )

    poison = _evidence(
        ref="exa.0",
        source="exa",
        source_class="external_proof",
        evidence_type="external_proof.external_mentions",
        url="https://unrelated.test/story",
        content="A different company called Example launched a product.",
        identity_match="brand_name",
    )
    poison_ledger = build_evidence_ledger_shadow(
        [
            _report("poison-one", "2026-01-01T00:00:00Z", [poison]),
            _report("poison-two", "2026-01-02T00:00:00Z", [deepcopy(poison)]),
        ],
        mode="shadow",
    )
    poison_state = poison_ledger["entries"][0]["state"]

    syndicated_rows = [
        _evidence(
            ref="exa.1",
            source="exa",
            source_class="external_proof",
            evidence_type="external_proof.external_mentions",
            url="https://wire-one.test/story",
            content="Example announced the same syndicated press release.",
            identity_match="domain",
        ),
        _evidence(
            ref="exa.2",
            source="exa",
            source_class="external_proof",
            evidence_type="external_proof.external_mentions",
            url="https://wire-two.test/copy",
            content="Example announced the same syndicated press release.",
            identity_match="domain",
        ),
    ]
    v2_poison = build_evidence_memory_identity_v2(
        [
            _report("poison-one", "2026-01-01T00:00:00Z", [poison]),
            _report("poison-two", "2026-01-02T00:00:00Z", [deepcopy(poison)]),
        ]
    )
    v2_poison_entry = v2_poison["entries"][0]
    v2_manually_accepted_poison = build_evidence_memory_identity_v2(
        [
            _report("poison-one", "2026-01-01T00:00:00Z", [poison]),
            _report("poison-two", "2026-01-02T00:00:00Z", [deepcopy(poison)]),
        ],
        adjudications=[
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "subject_type": "evidence",
                "subject_id": v2_poison_entry["evidence_id"],
                "sequence": 1,
                "decision": "accepted",
                "effective_state": "accepted",
                "schema_version": "evidence-memory-adjudication-v1",
                "policy_version": (
                    "evidence-memory-identity-adjudication-policy-v1"
                ),
                "evaluator_version": "adversarial-manual-review-v1",
                "reviewer": "adversarial-reviewer",
                "actor_id": "stress-harness",
                "reason_code": "deliberately_false_acceptance",
                "rationale": "Controlled poisoning probe.",
                "runtime_effect": False,
                "authority": False,
                "created_at": "2026-01-03T00:00:00+00:00",
            }
        ],
    )
    v2_manually_accepted_poison_entry = v2_manually_accepted_poison[
        "entries"
    ][0]
    strong_label_poison = deepcopy(poison)
    strong_label_poison["metadata"]["identity_match"] = "domain"
    v2_strong_label_poison = build_evidence_memory_identity_v2(
        [
            _report(
                "strong-poison-one",
                "2026-01-01T00:00:00Z",
                [strong_label_poison],
            ),
            _report(
                "strong-poison-two",
                "2026-01-02T00:00:00Z",
                [deepcopy(strong_label_poison)],
            ),
        ]
    )
    v2_strong_label_poison_entry = v2_strong_label_poison["entries"][0]
    reproducible_external = _evidence(
        ref="exa.verified",
        source="exa",
        source_class="external_proof",
        evidence_type="external_proof.external_mentions",
        url="https://press.test/story",
        content="Example launches a product.",
        identity_match="brand_name",
    )
    reproducible_external["metadata"]["external_identity_provenance"] = (
        build_external_identity_provenance(
            provider="exa",
            subject_url="https://example.com",
            source_url="https://press.test/story",
            matched_alias="Example",
            match_method="alias_in_title",
            match_score=0.95,
            collector_source_class="external",
            collector_relation="external",
            requires_human_review=False,
        )
    )
    v2_reproducible_external = build_evidence_memory_identity_v2(
        [
            _report(
                "verified-one",
                "2026-01-01T00:00:00Z",
                [reproducible_external],
            ),
            _report(
                "verified-two",
                "2026-01-02T00:00:00Z",
                [deepcopy(reproducible_external)],
            ),
        ]
    )
    v2_reproducible_external_entry = v2_reproducible_external["entries"][0]
    v2_syndication = build_evidence_memory_identity_v2(
        [
            _report("wire-one", "2026-01-01T00:00:00Z", syndicated_rows),
            _report("wire-two", "2026-01-02T00:00:00Z", deepcopy(syndicated_rows)),
        ]
    )
    paraphrased_rows = [
        _evidence(
            ref="exa.paraphrase.1",
            source="exa",
            source_class="external_proof",
            evidence_type="external_proof.external_mentions",
            url="https://publisher-one.test/story",
            content=(
                "Example announced a new platform that helps finance teams close "
                "monthly books faster with automated reporting across every subsidiary."
            ),
        ),
        _evidence(
            ref="exa.paraphrase.2",
            source="exa",
            source_class="external_proof",
            evidence_type="external_proof.external_mentions",
            url="https://publisher-two.test/copy",
            content=(
                "Example announced a new platform that helps finance teams close "
                "monthly books faster using automated reporting across every subsidiary."
            ),
        ),
    ]
    v2_paraphrase = build_evidence_memory_identity_v2(
        [_report("paraphrase", "2026-01-01T00:00:00Z", paraphrased_rows)]
    )
    second_owned_passage = deepcopy(stable_owned)
    second_owned_passage["ref"] = "web.0.chunk.2"
    second_owned_passage["content"] = "We automate monthly reporting."
    v2_multi_passage = build_evidence_memory_identity_v2(
        [
            _report(
                "multi-passage",
                "2026-01-01T00:00:00Z",
                [stable_owned, second_owned_passage],
            )
        ]
    )
    old_claim = deepcopy(stable_owned)
    old_claim["metadata"]["claim_id"] = "audience-primary"
    new_claim = deepcopy(old_claim)
    new_claim["content"] = "We help operations teams automate procurement."
    v2_explicit_change = build_evidence_memory_identity_v2(
        [
            _report("claim-old", "2026-01-01T00:00:00Z", [old_claim]),
            _report("claim-new", "2026-01-02T00:00:00Z", [new_claim]),
        ]
    )
    bare_claim_memory = build_evidence_claim_memory(
        [
            _report("claim-old", "2026-01-01T00:00:00Z", [old_claim]),
            _report("claim-new", "2026-01-02T00:00:00Z", [new_claim]),
        ]
    )
    semantic_old_claim = deepcopy(stable_owned)
    semantic_old_claim["metadata"].update(
        {
            "claim_slot_key": "audience.primary",
            "claim_type": "audience",
        }
    )
    semantic_new_claim = deepcopy(semantic_old_claim)
    semantic_new_claim["content"] = (
        "We help operations teams automate procurement."
    )
    semantic_claim_history = [
        _report(
            "semantic-claim-old",
            "2026-01-01T00:00:00Z",
            [semantic_old_claim],
        ),
        _report(
            "semantic-claim-new",
            "2026-01-02T00:00:00Z",
            [semantic_new_claim],
        ),
    ]
    semantic_claim_memory = build_evidence_claim_memory(
        semantic_claim_history
    )
    relation_subject_id = semantic_claim_memory["slots"][0][
        "relation_candidates"
    ][0]["relation_candidate_id"]
    accepted_claim_relation_memory = build_evidence_claim_memory(
        semantic_claim_history,
        claim_reconciliations=[
            {
                "id": "00000000-0000-0000-0000-000000000002",
                "subject_type": "claim_relation",
                "subject_id": relation_subject_id,
                "relation_type": "replacement_candidate",
                "sequence": 1,
                "decision": "accepted",
                "created_at": "2026-01-03T00:00:00Z",
            }
        ],
    )
    simultaneous_claim_memory = build_evidence_claim_memory(
        [
            _report(
                "semantic-claim-simultaneous",
                "2026-01-01T00:00:00Z",
                [semantic_old_claim, semantic_new_claim],
            )
        ]
    )
    reordered_claim_memory = build_evidence_claim_memory(
        list(reversed(semantic_claim_history))
    )

    return [
        _probe(
            "report_order_invariance",
            order_pass,
            "Reordering input reports leaves the deterministic ledger unchanged.",
        ),
        _probe(
            "acquisition_dropout_retains_identity_memory",
            dropout_pass,
            "A complete latest-capture dropout changes state to not_reacquired but removes no identity.",
        ),
        _probe(
            "evaluator_drift_does_not_rewrite_evidence",
            evaluation_pass,
            "Changing scores and tile states with identical evidence leaves identity memory unchanged.",
        ),
        _probe(
            "exact_repeat_does_not_inflate_breadth",
            repetition_pass,
            "A third exact observation increases persistence, not unique identities or locators.",
        ),
        _probe(
            "content_change_is_detected_not_resolved",
            change_pass,
            "Changed owned copy creates changed_candidate plus observed; no contradiction is inferred.",
        ),
        _probe(
            "ttl_never_auto_retires",
            staleness_pass,
            "Elapsed TTL proposes staleness while retaining the historical evidence identity.",
        ),
        _probe(
            "identity_v2_same_document_passages_are_not_revisions",
            (
                v2_multi_passage["summary"]["document_count"] == 1
                and v2_multi_passage["summary"]["passage_count"] == 2
                and v2_multi_passage["summary"]["revision_candidate_count"] == 0
            ),
            "Identity v2 keeps two passages from one URL without inventing a temporal revision.",
        ),
        _probe(
            "identity_v2_weak_external_identity_is_not_eligible",
            (
                v2_poison_entry["identity_status"] == "unverified"
                and v2_poison_entry["qualified_observation_count"] == 0
                and v2_poison_entry["state"] == "repeated"
            ),
            "Identity v2 remembers repeated brand-name-only evidence but does not validate it.",
        ),
        _probe(
            "accepted_identity_never_grants_runtime_authority",
            (
                v2_manually_accepted_poison_entry["adjudication_state"]
                == "accepted"
                and v2_manually_accepted_poison_entry["identity_status"]
                == "unverified"
                and v2_manually_accepted_poison_entry["state"] == "repeated"
                and v2_manually_accepted_poison["runtime_effect"] is False
                and v2_manually_accepted_poison["authority"] is False
            ),
            "Even a deliberately false manual acceptance changes only identity adjudication metadata.",
        ),
        _probe(
            "identity_v2_bare_upstream_domain_label_is_not_eligible",
            (
                v2_strong_label_poison_entry["identity_status"] == "unverified"
                and v2_strong_label_poison_entry["qualified_observation_count"] == 0
                and v2_strong_label_poison_entry["state"] == "repeated"
            ),
            "Identity v2 refuses a strong-looking upstream label without reproducible provenance.",
        ),
        _probe(
            "identity_v2_reproduced_external_identity_is_eligible",
            (
                v2_reproducible_external_entry["identity_status"] == "eligible"
                and v2_reproducible_external_entry[
                    "qualified_observation_count"
                ]
                == 2
                and v2_reproducible_external_entry["state"]
                == "validation_candidate"
            ),
            "Identity v2 independently reproduces persisted external attribution before proposing validation.",
        ),
        _probe(
            "identity_v3_exact_syndication_is_one_source_cluster",
            (
                v2_syndication["summary"]["current_external_publisher_count"] == 2
                and v2_syndication["summary"][
                    "current_external_cluster_count"
                ]
                == 1
                and v2_syndication["summary"][
                    "current_independent_external_cluster_count"
                ]
                == 0
                and {
                    entry["independence_status"]
                    for entry in v2_syndication["entries"]
                }
                == {"same_cluster"}
            ),
            "Identity policy v3 collapses exact syndicated copies without granting independent corroboration.",
        ),
        _probe(
            "identity_v3_paraphrased_syndication_is_not_independent",
            (
                v2_paraphrase["summary"]["current_external_cluster_count"] == 1
                and v2_paraphrase["summary"][
                    "current_independent_external_cluster_count"
                ]
                == 0
                and {
                    entry["independence_status"]
                    for entry in v2_paraphrase["entries"]
                }
                == {"same_cluster"}
            ),
            "Deterministic shingle similarity collapses lightly paraphrased copies without granting corroboration.",
        ),
        _probe(
            "identity_v3_unknown_ownership_never_counts_as_independent",
            (
                v2_reproducible_external_entry["independence_status"] == "unknown"
                and v2_reproducible_external["summary"][
                    "current_independent_external_cluster_count"
                ]
                == 0
            ),
            "Reproducible brand identity does not imply source independence when publisher ownership is unreviewed.",
        ),
        _probe(
            "identity_v2_stable_claim_slot_surfaces_revision",
            (
                v2_explicit_change["summary"]["revision_candidate_count"] == 1
                and any(
                    entry["state"] == "revision_candidate"
                    and entry["adjudication_state"] == "proposed"
                    for entry in v2_explicit_change["entries"]
                )
            ),
            "Identity v2 surfaces a controlled stable-slot change as a proposed revision without accepting it.",
        ),
        _probe(
            "claim_memory_rejects_bare_content_derived_claim_id",
            (
                bare_claim_memory["summary"]["claim_slot_count"] == 0
                and bare_claim_memory["summary"][
                    "ignored_bare_claim_id_count"
                ]
                == 1
            ),
            "Claim Memory refuses to treat a bare text-derived claim ID as a longitudinal slot.",
        ),
        _probe(
            "claim_memory_separates_slot_variant_and_occurrence",
            (
                semantic_claim_memory["summary"]["claim_slot_count"] == 1
                and semantic_claim_memory["summary"]["claim_variant_count"]
                == 2
                and semantic_claim_memory["summary"][
                    "claim_occurrence_count"
                ]
                == 2
                and semantic_claim_memory["summary"][
                    "relation_candidate_counts"
                ]
                == {"replacement_candidate": 1}
                and semantic_claim_memory["runtime_effect"] is False
                and semantic_claim_memory["authority"] is False
            ),
            "Claim Memory keeps semantic slot, content variant, and report occurrence identities separate.",
        ),
        _probe(
            "claim_memory_simultaneous_variants_are_coexistence_candidates",
            (
                simultaneous_claim_memory["summary"][
                    "relation_candidate_counts"
                ]
                == {"coexistence_candidate": 1}
                and simultaneous_claim_memory["slots"][0][
                    "latest_variant_count"
                ]
                == 2
            ),
            "Two variants observed together are proposed as coexistence, never replacement.",
        ),
        _probe(
            "claim_memory_report_order_invariance",
            (
                reordered_claim_memory["state_fingerprint"]
                == semantic_claim_memory["state_fingerprint"]
            ),
            "Claim Memory is deterministic when immutable reports arrive in a different order.",
        ),
        _probe(
            "accepted_claim_relation_never_grants_canonical_authority",
            (
                accepted_claim_relation_memory["slots"][0][
                    "relation_candidates"
                ][0]["adjudication_state"]
                == "accepted"
                and accepted_claim_relation_memory["claim_reconciliation"][
                    "automatic_canonical_selection"
                ]
                is False
                and accepted_claim_relation_memory["runtime_effect"] is False
                and accepted_claim_relation_memory["authority"] is False
                and "canonical_claim_variant_id"
                not in accepted_claim_relation_memory
            ),
            "An accepted relation remains reversible review metadata and never selects a canonical claim.",
        ),
        {
            "id": "identity_gold_set_pending_human_review",
            "kind": "promotion_blocker",
            "status": "blocked",
            "observation": (
                f"In v1 a deliberately wrong external item reached `{poison_state}` because "
                "brand-name identity metadata was trusted twice. V2 blocks eligibility and "
                "stores reversible decisions under a server-bound reviewer. A 14-case "
                "versioned candidate set exists, but it has no human review decisions yet."
            ),
            "required_capability": (
                "complete the candidate reviews and satisfy the frozen gold-set thresholds"
            ),
        },
        {
            "id": "source_independence_pending_production_review",
            "kind": "promotion_blocker",
            "status": "blocked",
            "observation": (
                "Source-independence policy v3 fails closed and passes controlled "
                "exact-copy, light-paraphrase, ownership, lineage, and ambiguity "
                "probes. The versioned registry has no production-reviewed source "
                "URLs, so real clusters cannot be confirmed independent."
            ),
            "required_capability": (
                "attributable reversible production source reviews plus measured "
                "semantic-paraphrase recall"
            ),
        },
        {
            "id": "changed_claim_has_no_canonical_resolution",
            "kind": "promotion_blocker",
            "status": "blocked",
            "observation": (
                "The append-only journal can accept, dispute, reject, supersede, and "
                "revoke relation decisions without authority. No reviewed policy yet "
                "turns those decisions into a canonical claim version."
            ),
            "required_capability": (
                "reviewed canonical-claim promotion policy with measured false "
                "replacement and missed-change rates"
            ),
        },
        {
            "id": "stable_evidence_has_no_tile_mapping",
            "kind": "promotion_blocker",
            "status": "blocked",
            "observation": (
                "The current ledger persists evidence identities but not evidence → claim → tile support."
            ),
            "required_capability": "persistent versioned tile-evidence support",
        },
        {
            "id": "no_versioned_memory_evaluator",
            "kind": "promotion_blocker",
            "status": "blocked",
            "observation": (
                "No score is currently computed from a canonical memory version, so score stability "
                "and real-change sensitivity cannot yet be measured."
            ),
            "required_capability": (
                "evaluation identity hash(memory_version, rubric_version, evaluator_version)"
            ),
        },
    ]


def _replay_real_histories(
    histories: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    check_ids = (
        "report_order_invariance",
        "acquisition_dropout_retains_identity_memory",
        "evaluator_drift_does_not_rewrite_evidence",
        "exact_repeat_does_not_inflate_breadth",
    )
    failures: dict[str, list[str]] = {check_id: [] for check_id in check_ids}
    material_history_count = 0
    diagnostics: list[dict[str, Any]] = []

    for domain, reports in sorted(histories.items()):
        if not reports:
            continue
        baseline = build_evidence_ledger_shadow(reports, mode="shadow")
        identities = _identity_set(baseline)
        if not identities:
            continue
        material_history_count += 1
        latest = reports[-1]

        reordered = build_evidence_ledger_shadow(
            list(reversed(reports)),
            mode="shadow",
        )
        if reordered["state_fingerprint"] != baseline["state_fingerprint"]:
            failures["report_order_invariance"].append(domain)

        dropout_report = _variant_report(
            latest,
            suffix="stress-dropout",
            seconds=1,
        )
        _set_evidence_rows(dropout_report, [])
        dropout_report["acquisition_gate"] = {
            "state": "warning",
            "warnings": [{"code": "stress_provider_dropout"}],
        }
        dropout = build_evidence_ledger_shadow(
            [*reports, dropout_report],
            mode="shadow",
        )
        if _identity_set(dropout) != identities:
            failures["acquisition_dropout_retains_identity_memory"].append(domain)

        evaluator_report = _variant_report(
            latest,
            suffix="stress-evaluator",
            seconds=2,
        )
        _mutate_evaluation(evaluator_report)
        evaluator = build_evidence_ledger_shadow(
            [*reports, evaluator_report],
            mode="shadow",
        )
        if _identity_set(evaluator) != identities:
            failures["evaluator_drift_does_not_rewrite_evidence"].append(domain)

        exact_repeat = _variant_report(
            latest,
            suffix="stress-repeat",
            seconds=3,
        )
        repeated = build_evidence_ledger_shadow(
            [*reports, exact_repeat],
            mode="shadow",
        )
        if (
            _identity_set(repeated) != identities
            or _locator_set(repeated) != _locator_set(baseline)
        ):
            failures["exact_repeat_does_not_inflate_breadth"].append(domain)

        state_counts = Counter(entry["state"] for entry in baseline["entries"])
        source_counts = Counter(entry["source_class"] for entry in baseline["entries"])
        entries_by_locator = Counter(
            str(entry.get("locator_hash") or "")
            for entry in baseline["entries"]
            if str(entry.get("locator_hash") or "")
        )
        multi_variant_locators = {
            locator: count
            for locator, count in entries_by_locator.items()
            if count > 1
        }
        entries_in_multi_variant_locators = sum(multi_variant_locators.values())
        diagnostics.append(
            {
                "domain": domain,
                "report_count": len(reports),
                "score_range": _score_range(reports),
                "evidence_identity_count": len(identities),
                "locator_count": len(_locator_set(baseline)),
                "multi_variant_locator_count": len(multi_variant_locators),
                "entries_in_multi_variant_locators": entries_in_multi_variant_locators,
                "multi_variant_entry_rate": round(
                    entries_in_multi_variant_locators / max(1, len(identities)),
                    4,
                ),
                "state_counts": dict(sorted(state_counts.items())),
                "source_class_counts": dict(sorted(source_counts.items())),
            }
        )

    checks = []
    for check_id in check_ids:
        failed_domains = failures[check_id]
        passed = material_history_count - len(failed_domains)
        checks.append(
            {
                "id": check_id,
                "status": "pass" if not failed_domains else "fail",
                "passed": passed,
                "total": material_history_count,
                "rate": round(passed / max(1, material_history_count), 4),
                "failed_domains": failed_domains,
            }
        )
    return {
        "history_count": sum(1 for reports in histories.values() if reports),
        "history_with_material_evidence_count": material_history_count,
        "checks": checks,
        "histories": diagnostics,
    }


def _replay_identity_v2(
    histories: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    metric_keys = (
        "v1_changed_candidate_count",
        "v2_revision_candidate_count",
        "removed_url_only_change_pressure_count",
        "retained_explicit_revision_count",
        "v2_only_revision_candidate_count",
        "v2_claim_slot_count",
        "v2_current_external_passage_count",
        "v2_current_external_publisher_count",
        "v2_current_external_syndication_cluster_count",
        "v2_current_external_cluster_count",
        "v2_current_independent_external_cluster_count",
    )
    rows: list[dict[str, Any]] = []
    totals = Counter()
    claim_slot_method_totals = Counter()
    independence_cluster_status_totals = Counter()
    for domain, reports in sorted(histories.items()):
        if not reports:
            continue
        v1 = build_evidence_ledger_shadow(reports, mode="shadow")
        v2 = build_evidence_memory_identity_v2(reports)
        v1_state_counts = Counter(
            str(entry.get("state") or "")
            for entry in v1.get("entries") or []
        )
        v2_state_counts = Counter(
            str(entry.get("state") or "")
            for entry in v2.get("entries") or []
        )
        v1_changed_fingerprints = {
            str(entry.get("evidence_fingerprint") or "")
            for entry in v1.get("entries") or []
            if entry.get("state") == "changed_candidate"
        }
        v1_changed_ids = {
            canonical_evidence_digest(
                source_class=record.source_class,
                evidence_type=record.evidence_type,
                url=record.url,
                content=record.normalized_content,
            )
            for report in reports
            for record in canonical_evidence_records(report)
            if record.fingerprint in v1_changed_fingerprints
        }
        v2_revision_ids = {
            str(entry.get("evidence_id") or "")
            for entry in v2.get("entries") or []
            if entry.get("state") == "revision_candidate"
        }
        v2_current_external = [
            entry
            for entry in v2.get("entries") or []
            if entry.get("source_class") == "external_proof"
            and entry.get("state") in _CURRENT_STATES
        ]
        row = {
            "domain": domain,
            "report_count": len(reports),
            "v1_changed_candidate_count": v1_state_counts.get(
                "changed_candidate",
                0,
            ),
            "v2_revision_candidate_count": v2_state_counts.get(
                "revision_candidate",
                0,
            ),
            "v2_document_count": int(v2["summary"]["document_count"]),
            "v2_passage_count": int(v2["summary"]["passage_count"]),
            "v2_multi_passage_document_count": int(
                v2["summary"]["multi_passage_document_count"]
            ),
            "v2_claim_slot_count": int(v2["summary"]["claim_slot_count"]),
            "v2_current_external_passage_count": len(v2_current_external),
            "v2_current_external_publisher_count": int(
                v2["summary"]["current_external_publisher_count"]
            ),
            "v2_current_external_syndication_cluster_count": int(
                v2["summary"]["current_external_syndication_cluster_count"]
            ),
            "v2_current_external_cluster_count": int(
                v2["summary"]["current_external_cluster_count"]
            ),
            "v2_current_independent_external_cluster_count": int(
                v2["summary"]["current_independent_external_cluster_count"]
            ),
            "v2_current_external_independence_cluster_status_counts": dict(
                v2["summary"][
                    "current_external_independence_cluster_status_counts"
                ]
            ),
            "v2_identity_status_counts": dict(
                v2["summary"]["identity_status_counts"]
            ),
            "v2_claim_slot_method_counts": dict(
                v2["summary"]["claim_slot_method_counts"]
            ),
        }
        row["removed_url_only_change_pressure_count"] = len(
            v1_changed_ids - v2_revision_ids
        )
        row["retained_explicit_revision_count"] = len(
            v1_changed_ids & v2_revision_ids
        )
        row["v2_only_revision_candidate_count"] = len(
            v2_revision_ids - v1_changed_ids
        )
        rows.append(row)
        for key in metric_keys:
            totals[key] += int(row[key])
        claim_slot_method_totals.update(row["v2_claim_slot_method_counts"])
        independence_cluster_status_totals.update(
            row["v2_current_external_independence_cluster_status_counts"]
        )
    return {
        "schema_version": "evidence-memory-identity-v2-replay-v2",
        "runtime_effect": False,
        "summary": {
            "history_count": len(rows),
            **{key: int(totals.get(key, 0)) for key in metric_keys},
            "v2_claim_slot_method_counts": dict(
                sorted(claim_slot_method_totals.items())
            ),
            "v2_current_external_independence_cluster_status_counts": dict(
                sorted(independence_cluster_status_totals.items())
            ),
        },
        "histories": rows,
    }


def _replay_claim_memory(
    histories: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    metric_keys = (
        "claim_slot_count",
        "claim_variant_count",
        "claim_occurrence_count",
        "current_claim_variant_count",
        "multi_variant_slot_count",
        "claim_type_conflict_count",
        "relation_candidate_count",
        "ignored_claim_metadata_count",
        "ignored_bare_claim_id_count",
    )
    rows: list[dict[str, Any]] = []
    totals = Counter()
    relation_totals = Counter()
    ignored_reason_totals = Counter()
    slot_method_totals = Counter()
    for domain, reports in sorted(histories.items()):
        if not reports:
            continue
        memory = build_evidence_claim_memory(reports)
        summary = memory["summary"]
        row = {
            "domain": domain,
            "report_count": len(reports),
            **{
                key: int(summary.get(key, 0))
                for key in metric_keys
            },
            "relation_candidate_counts": dict(
                summary["relation_candidate_counts"]
            ),
            "ignored_claim_reason_counts": dict(
                summary["ignored_claim_reason_counts"]
            ),
            "claim_slot_method_counts": dict(
                summary["claim_slot_method_counts"]
            ),
        }
        rows.append(row)
        for key in metric_keys:
            totals[key] += int(row[key])
        relation_totals.update(row["relation_candidate_counts"])
        ignored_reason_totals.update(row["ignored_claim_reason_counts"])
        slot_method_totals.update(row["claim_slot_method_counts"])
    return {
        "schema_version": "evidence-claim-memory-v1-replay-v1",
        "runtime_effect": False,
        "authority": False,
        "summary": {
            "history_count": len(rows),
            **{key: int(totals.get(key, 0)) for key in metric_keys},
            "relation_candidate_counts": dict(
                sorted(relation_totals.items())
            ),
            "ignored_claim_reason_counts": dict(
                sorted(ignored_reason_totals.items())
            ),
            "claim_slot_method_counts": dict(
                sorted(slot_method_totals.items())
            ),
        },
        "histories": rows,
    }


def _probe(probe_id: str, passed: bool, observation: str) -> dict[str, Any]:
    return {
        "id": probe_id,
        "kind": "executable_invariant",
        "status": "pass" if passed else "fail",
        "observation": observation,
    }


def _format_counts(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "none"
    return ", ".join(
        f"{key}={int(count)}"
        for key, count in sorted(value.items())
    )


def _identity_set(ledger: dict[str, Any]) -> set[str]:
    return {
        str(entry.get("evidence_fingerprint") or "")
        for entry in ledger.get("entries") or []
        if str(entry.get("evidence_fingerprint") or "")
    }


def _locator_set(ledger: dict[str, Any]) -> set[str]:
    return {
        str(entry.get("locator_hash") or "")
        for entry in ledger.get("entries") or []
        if str(entry.get("locator_hash") or "")
    }


def _ordered_reports(reports: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (deepcopy(report) for report in reports if isinstance(report, dict)),
        key=lambda report: (
            _timestamp(report.get("created_at")),
            str(report.get("id") or ""),
        ),
    )


def _variant_report(
    report: dict[str, Any],
    *,
    suffix: str,
    seconds: int,
) -> dict[str, Any]:
    variant = deepcopy(report)
    variant["id"] = f"{str(report.get('id') or 'report')}-{suffix}"
    variant["created_at"] = (
        _timestamp(report.get("created_at")) + timedelta(seconds=seconds)
    ).isoformat()
    return variant


def _mutate_evaluation(report: dict[str, Any]) -> None:
    score = report.get("score")
    report["score"] = (int(score) + 17) % 101 if isinstance(score, (int, float)) else 17
    components = report.get("components")
    if isinstance(components, list) and components and isinstance(components[0], dict):
        current = components[0].get("score")
        components[0]["score"] = (
            (int(current) + 1) % 11
            if isinstance(current, (int, float))
            else 1
        )
    blocks = report.get("blocks")
    if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict):
        blocks[0]["content"] = "Adversarial evaluator-only interpretation."


def _set_evidence_rows(report: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    raw = report.get("raw")
    if not isinstance(raw, dict):
        raw = {}
        report["raw"] = raw
    flow = raw.get("flow")
    if not isinstance(flow, dict):
        flow = {}
        raw["flow"] = flow
    candidate = flow.get("candidate")
    if not isinstance(candidate, dict):
        candidate = {}
        flow["candidate"] = candidate
    pack = candidate.get("evidence_pack")
    if not isinstance(pack, dict):
        pack = {}
        candidate["evidence_pack"] = pack
    pack["evidence"] = deepcopy(rows)


def _report(
    report_id: str,
    created_at: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    report = {
        "id": report_id,
        "brand_name": "Example",
        "url": "https://example.com",
        "created_at": created_at,
        "score": 50,
        "reliability_status": "shadow",
        "acquisition_gate": {"state": "pass"},
        "components": [
            {
                "key": "mission",
                "status": "scored",
                "score": 5,
                "tile_profile": [{"id": "M1", "estado": "ok"}],
                "block": {
                    "refs": [
                        {"ref": str(row.get("ref") or "")}
                        for row in evidence
                    ]
                },
            }
        ],
        "blocks": [
            {
                "name": "mission",
                "detected": True,
                "content": "A stable interpretation.",
                "coverage_status": "enough",
            }
        ],
        "raw": {
            "flow": {
                "candidate": {
                    "evidence_pack": {
                        "brand_name": "Example",
                        "url": "https://example.com",
                        "evidence": deepcopy(evidence),
                    }
                }
            }
        },
    }
    return report


def _evidence(
    *,
    ref: str,
    source: str,
    source_class: str,
    evidence_type: str,
    url: str,
    content: str,
    identity_match: str = "",
) -> dict[str, Any]:
    metadata = {"source_class": source_class}
    if identity_match:
        metadata["identity_match"] = identity_match
    return {
        "ref": ref,
        "source": source,
        "evidence_type": evidence_type,
        "url": url,
        "content": content,
        "confidence": "medium",
        "metadata": metadata,
    }


def _score_range(reports: Iterable[dict[str, Any]]) -> float | None:
    values = [
        float(report["score"])
        for report in reports
        if isinstance(report.get("score"), (int, float))
    ]
    return round(max(values) - min(values), 4) if values else None


def _timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")
