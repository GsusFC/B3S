"""Adversarial replay harness for the longitudinal evidence-memory direction.

The harness is deliberately read-only. It tests properties that the current
evidence ledger can already guarantee and reports explicit promotion blockers
for claims, tiles, adjudication, and memory-based scoring that do not exist yet.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from src.services.evidence_ledger_shadow import build_evidence_ledger_shadow


EVIDENCE_MEMORY_STRESS_VERSION = "evidence-memory-stress-v1"
EVIDENCE_MEMORY_STRESS_POLICY_VERSION = "evidence-memory-stress-policy-v1"


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
        },
        "executable_failures": executable_failures,
        "promotion_blockers": promotion_blockers,
        "controlled_probes": controlled,
        "real_history_replay": replay,
        "interpretation": {
            "supported": (
                "Evidence identities can be remembered deterministically across "
                "acquisition loss, evaluator drift, ordering changes, and exact repeats."
            ),
            "not_yet_supported": (
                "The current ledger cannot decide which claim is current, reject poisoned "
                "evidence, map stable evidence to tiles, or produce a versioned memory score."
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
    syndicated_ledger = build_evidence_ledger_shadow(
        [
            _report("wire-one", "2026-01-01T00:00:00Z", syndicated_rows),
            _report("wire-two", "2026-01-02T00:00:00Z", deepcopy(syndicated_rows)),
        ],
        mode="shadow",
    )
    syndicated_candidates = sum(
        1
        for entry in syndicated_ledger["entries"]
        if entry["state"] == "validation_candidate"
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
        {
            "id": "repeated_false_identity_can_become_validation_candidate",
            "kind": "promotion_blocker",
            "status": "blocked",
            "observation": (
                f"A deliberately wrong external item reached `{poison_state}` because "
                "identity metadata was trusted twice."
            ),
            "required_capability": "versioned identity adjudication plus rejected/revoked states",
        },
        {
            "id": "syndication_is_not_clustered",
            "kind": "promotion_blocker",
            "status": "blocked",
            "observation": (
                f"Two syndicated URLs produced {syndicated_candidates} validation candidates; "
                "the ledger stores a warning but no independence cluster."
            ),
            "required_capability": "deterministic source-independence and syndication clustering",
        },
        {
            "id": "changed_claim_has_no_canonical_resolution",
            "kind": "promotion_blocker",
            "status": "blocked",
            "observation": (
                "Old and new content coexist, but neither can be accepted, superseded, "
                "contradicted, or reversed."
            ),
            "required_capability": "versioned claim reconciliation with an appeal path",
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


def _probe(probe_id: str, passed: bool, observation: str) -> dict[str, Any]:
    return {
        "id": probe_id,
        "kind": "executable_invariant",
        "status": "pass" if passed else "fail",
        "observation": observation,
    }


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
