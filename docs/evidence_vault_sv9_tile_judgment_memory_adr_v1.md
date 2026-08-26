# ADR v1 — Immutable SV9 Tile Judgment Contracts

**Status:** Accepted PR1 contract boundary; no runtime integration.

## Proven current flow

`persisted/read-back Vault capture → live build_flow_sv9_shadow_eval → nine SV9 components plus Coherencia → assessment_kernel arithmetic → validated public report`.

Operational Vault SV9 shadow is diagnostic-only; Semantic v3 is a legacy diagnostic projection. Neither is judgment-memory authority.

## Decision

PR1 adds pure, canonical value contracts only. A tile judgment binds executable registry tile/component identity, `ok|no|sin_evidencia`, exact evidence references (identity plus SHA-256 fingerprint), capture/operation origins, authority/review state, lifecycle (`active|reopened|superseded`) and reason, series identity, and a domain-separated judgment fingerprint.

Authority, review, and lifecycle are independent axes. A semantic result may be `pending` and require no human; `superseded` is derived append-only. Scoring eligibility is outside this value contract.

The executable rubric, registry fingerprint, and scoring-policy constants are authority. The series also binds evaluator, prompt, model, Flow, and normalization versions; no 80-tile copy or arithmetic is introduced. Unsigned construction canonicalizes accepted input; signed replay accepts only exact canonical JSON objects and evidence arrays, rejecting reordered, whitespace-normalized, malformed, duplicate, unknown, or tampered values.

| Boundary | Canonical authority | Explicitly not authoritative |
| --- | --- | --- |
| Evidence memory | Captured facts and provenance | Tile semantic judgment |
| Tile judgment memory | Immutable evaluator value and lifecycle | Scoring or narrative |
| Assessment kernel | Complete-vector arithmetic | Pending/review or authority |
| Narrative | Founder-facing interpretation | Canonical facts or scores |

## Deferred work

The typed tile-evidence delta preserves tile/component, disposition, evidence references, origins, and fingerprint for PR2. Incremental planning, evaluation, persistence, Scanner/public integration, LLM/I/O/clock/random state, scoring, Social Tiles, and migrations are deferred and non-authoritative in PR1.

Later persistence will use `b3s_history` brand/capture identities, locking, replay/idempotency, and ACL patterns in a dedicated append-only relation. Evidence Vault packets, the claim-tile ledger, migrations 025–028 full-assessment shadow, SQLite `sv9_scans`, and public `report_store` are rejected as judgment authority.
