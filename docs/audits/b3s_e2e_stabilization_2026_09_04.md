# B3S end-to-end stabilization — engineering record

Status: isolated repair candidate, **not a declaration that production is fixed**.

## Baseline and decisions

Initial main: `dd666ae64d3ca4a4171ef26a4bbbeb0af0c5ef5d`; verified source tree `866834042d6d20b876dd445c76ce9d53cad77cbe`.

PR #251 (`84cedb5211987ebda5b245f7de588e0d663346ac`) already proposed immutable resume successors. It was reused unchanged as a dependency, not reimplemented. Integration is confined to draft PR #254, branch `codex/b3s-stabilization-20260904`. The additional production repair commit is `12a1e458df5bc3d23a8ebf75d1f7ef9e3725204a`.

Root AGENTS, README, CONTRIBUTING, owning contracts, migrations, runtime/deployment gates and existing fixtures were inspected. The local environment had no checkout or outbound Git access; temporary CI transport delivered verified source and applied the local patch. Transport files and CI edits were removed before the resulting source was tested. CI is byte-identical to main, blob `c1082f6b5c53a364b8d38e43936b93722c0183d1`.

No main merge, deployment, live provider call, production data operation, migration, new lock or existing user-worktree modification was performed.

## Architecture discovered

| Boundary | Concrete owner | Durable state / interpretation |
| --- | --- | --- |
| API and scanner job | `web/api_v1/router.py`, `web/scan_runner.py`, `src/storage/scanner_api_jobs.py`, `SQLiteStore` | SQLite `b3s_scanner_jobs`; thread events, `_SCANS` and `_SCAN_OWNERS` are runtime state, not durable evaluation progress. |
| Acquisition to capture | `evidence_vault_scan_orchestration.prepare_vault_scan_after_capture`, `build_capture_observation_from_snapshot`, `src/history/capture_observation.py` | PostgreSQL `scan_runs`, `captures`, `evidence_records`, acquisition attempts and artifacts. Observation and raw-capture hashes remain distinct. |
| Capture to operation | Same orchestration module, `evidence_vault_incremental_refresh.build_vault_scan_plan`, `PostgresHistoryRepository` | `evidence_vault_operation_plans` (015), exact raw observation, canonical parent, plan and persisted result. |
| Operational memory | `evidence_vault_incremental_executor.execute_vault_operation_plan`, existing source registration/review/promotion methods in `src/history/repository.py` | Source packets, accepted relation reviews and canonical promotion events. Operational output is not automatically public scoring authority. |
| Evidence authority | `evidence_vault_sv9_authoritative_relations.project_evidence_vault_sv9_evaluation_input`, repository `load_evidence_vault_sv9_authoritative_relation_facts` | Derived signed projection of frozen evidence and accepted basis; witness bound to current authority. |
| Workset and hints | `evidence_vault_sv9_judgment_delta.py`, `evidence_vault_sv9_workset_partition.py` | Derived healthy/review/reused work. Hints route tile/evidence pairs; they do not grant authority. |
| Flow and acceptance | `evidence_vault_sv9_authority_evaluation.py`, `FlowSv9StrictComponentAdapter`, `src/sv9/incremental_evaluation.py::_run_partial`, `_accept` | Canonical request and temporary provider response; strict request, series, tile and evidence acceptance. |
| Checkpoint | Evaluation callback, `evidence_vault_sv9_evaluation_checkpoint.py`, repository append/lookup | `evidence_vault_sv9_evaluation_checkpoints` and evidence bindings (034). One accepted evaluation, not an accumulated prefix. |
| Full candidate and score | Incremental replay, candidate builder, `src/sv9/assessment_kernel.py` | `evidence_vault_sv9_judgment_candidates` and bindings (031), witnessed validation (033), complete existing SV9 assessment. |
| Authority adoption | `evidence_vault_sv9_authority_application.py`, existing repository adopt/reopen methods | `evidence_vault_sv9_judgment_authority_events` (032). Adoption is durable before report persistence. |
| Report and publication | `evidence_vault_sv9_authority_report.project_vault_authority_publication`, scanner composition, `web/report_store.py` | Strict publish/retain/no-score decision; immutable `report_snapshots` and existing JSON recovery store. |
| Exact Resume | `web/exact_resume_controller.py`, `prepare_vault_exact_resume`, `_run_vault_exact_resume` | SQLite `b3s_scanner_resume_actions`; frozen operation/capture validation; separate action identity, report identity and source provenance. |

Service modules in the table are under `src/services/` unless a full path is shown.

## Transition invariants and interruption behavior

1. Capture preparation persists and reads back an exact observation/operation before Flow. Repeated identical input resolves the stored operation; a different capture under the same ID fails.
2. Operation states include pending, claimed, running, result_persisted, completed, failed_retryable and superseded. Existing lease/fencing rules apply. Persisted-result recovery materializes the result rather than repeating semantic work.
3. Projection/workset construction does not turn missing, duplicate or ambiguous evidence into support. Healthy partial work can proceed only under the existing signed partition contract. Unsigned/global identity diagnostics remain fail-closed.
4. `_accept()` validates into temporary evaluation state. A rejected response creates no checkpoint. The durable callback runs before the next provider call and before temporary accepted progress is committed.
5. Interruption before checkpoint persistence reissues that evaluation. Interruption after persistence revalidates and reuses it. A checkpoint read/write failure is not a successful cache miss or safely recorded progress.
6. A complete candidate is replayed, fingerprint-checked and durably adopted. Partial progress never invents a new public score. Coherencia and score arithmetic remain owned by existing SV9 code.
7. Report publication validates complete assessment and provenance. A no-score original remains byte-identical; a completed resume can publish a deterministic successor. Same-action repetition validates/reuses that successor before Flow.
8. Authority adoption, report storage and SQLite action completion are separate durable boundaries, not one transaction. Failure between them must recover from validated persisted facts, not assume an earlier write never happened.
9. A later scan can retain a successor only when assessment, score/fingerprints and original capture provenance match. Merely newer reports cannot substitute for the accepted authority.
10. Phase Zero/One/Two, Visual Signature, evidence acceptance, Flow transport, scoring policy, schema and locking semantics remain unchanged.

## Confirmed failure map

| ID | Symptom → cause → violated invariant | Repair boundary |
| --- | --- | --- |
| F01 | Repeated no-score raises `vault_exact_resume_execution_failed`: regenerated content is saved with the immutable original ID. | Existing PR #251: preserve original on no-score; completed resume writes an action-derived successor and validates repeat reuse. |
| F02 | Complete resume raises `vault_exact_resume_report_invalid` before saving: source resolver rejects a candidate sourced from the current scan, although that is valid newly accepted authority. | `_accepted_authority_source_report` distinguishes malformed provenance from a current-scan candidate with no older report to load. |
| F03 | After adoption and failed report writing, retry records no-score despite complete persisted authority: retained authority was interpreted only as retaining an already-published report. | Pure publication projector permits materializing validated same-scan authority without a reopen overlay through the existing `publish_current` result. |
| F04 | Next scan loses the resumed score: lookup follows original no-score ID and retention requires report ID equal source scan ID. | Existing brand-report lookup finds only strictly matching successors; fenced reread and pure retention validation preserve original provenance and exact assessment. |
| F05 | Failed PostgreSQL report read becomes `None` when no durable fallback exists: unknown state is treated as confirmed absence. | `load_report` propagates failed reads without a valid fallback, while preserving explicit local-only mode and validated file recovery. |

These are reproduced failures, not merely source hypotheses. Main failed both initial composed resume cases. Independently verified PR #251 production source fixed no-score but still failed F02. Additional composed regressions reproduced F03/F04/F05 before repair.

## Regression coverage and measured evidence

`tests/test_vault_stabilization_replay.py` reuses the existing `_ApplicationRepository` and `_Flow` fixtures. It runs the real exact runner, authority application, strict acceptance/checkpoint callbacks, publication projector and immutable JSON writer. Only completed-operation preparation and external provider behavior are fixture-controlled. It is not a second production implementation or a claimed all-real PostgreSQL scan.

Twenty local regressions passed (10.18 s), covering complete/no-score, repeated resume, original bytes, successor provenance, adopted-authority/report-write failure, next-scan retention, interruption before/after checkpoint, persistence failure, `_accept()` rejection, revalidation of corrupted checkpoints, unavailable report reads, valid durable recovery, confirmed absence, and rejection of manipulated successor identity/provenance/score/fingerprints. The adjacent pure-publication suite passed 29 tests (32.18 s).

Existing suites cover missing/duplicate/unmatched evidence, hint identity and authority boundaries, coverage loss, workset isolation, checkpoint contracts, candidate/authority persistence, controller finalization and existing ownership semantics. They remain unchanged except for the already-existing PR #251 scanner tests.

A real Vercel fixture test was added to `tests/test_evidence_vault_scan_orchestration_postgres.py`. It uses the existing disposable-database guard and repository to persist actual capture bytes, then prepares Exact Resume twice from the same durable operation. The frozen fixture has four raw input entries and 71 normalized evidence records; it is not the later 298-record live capture. It makes no model calls.

The existing SoccerSolver/CausaPrima normalized replay was run twice with identical results:

- Manifest SHA-256: `096b611cc6a6708bb17c462f18782a5a22d914857b2e45ec9c54783ddc98c60b`.
- Result fingerprint: `89ce4392b8364d1d4892e157ca473f0bc7730c57253dd0c642a0c57bfdde5c31`.
- Gate pass; level `normalized_evidence_pack_only`; authority false; cutover false.
- Historical reference: 137 accepted evidence records, 250 occurrences, 80 tiles, recomputed score 91. This is not a newly executed public scan.

The actual shared-source repository helper was measured using its existing 79-tile spy fixture: **one source lookup/validation, one operation lookup/validation, 79 individual policy checks**. Main already deduplicates this work. No new cache or speculative optimization was added; spy timing is not live PostgreSQL latency.

## Verification record

- Initial local focused suite: 301 passed, one dependency warning, 31.79 s; Ruff passed.
- Initial canonical CI run `33919720462`, job `101175013435`: 3,295 passed, one skipped, 235.13 s; Ruff passed; informative benchmark 85 with its existing traceability warning.
- First full repaired local run: 4,318 passed, 50 skipped, three failed, three subtests passed, 197.86 s. Two failures require historical Git objects absent from the offline archive. One was a partially synchronized PR #251 test; its exact upstream version was restored and the focused 44-test suite passed. These are not reported as a green full run.
- Local composed suite after negative tests: 20 passed; adjacent publication suite: 29 passed.
- Full PostgreSQL verification uses the unchanged canonical CI on this PR. Consult the check attached to the final commit; the baseline result is not proof for the repaired tree.

Commands remain `python -m ruff check .` and `python -m pytest -q`. Focused replay:

```sh
python -m pytest -q tests/test_vault_stabilization_replay.py tests/test_evidence_vault_field_replay.py tests/test_evidence_vault_scan_orchestration_postgres.py
```

## Rejected designs and unchanged areas

No snapshots, event sourcing, checkpoint-v2/v3, new adapters, persistence representations, migration, extra lock, concurrency framework, generic retry/orchestrator or replacement state machine. No weakening of immutability, `_accept()`, review or score completeness. No Flow/SV9 or Phase/Visual Signature changes. No competing implementation of PR #251. No broad formatting/refactoring of the scanner file.

## Remaining risks / hypotheses

- **Correctness:** composed application replay, frozen PostgreSQL preparation and separate controller tests do not prove one all-real acquisition-to-publication execution. No live end-to-end scan completed here.
- **Persistence:** the stores are not transactional together. Exception injection and controller tests do not prove OS power-loss durability or deployed restart recovery at every boundary.
- **Performance:** redundant shared-package work is already addressed. Large live brand histories and the successor fallback's existing report listing remain unmeasured.
- **Concurrency:** existing semantics remain; no multi-process in-flight load claim is made.
- **External dependencies:** acquisition, authentication, provider availability and real model responses remain unverified. Frozen replay deliberately removes their nondeterminism.
- **Hypotheses:** further cross-store interactions may exist; none justify speculative infrastructure without reproduction.

The result is a minimal repair candidate with explicit evidence and limits, not a claim that passing tests alone stabilize B3S.
