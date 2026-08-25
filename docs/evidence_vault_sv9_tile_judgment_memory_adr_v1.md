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

PR5 added the dedicated append-only PostgreSQL candidate relation. PR6 may only run after successful immutable report publication, behind `BRAND3_VAULT_SV9_JUDGMENT_SHADOW_ENABLED=true`; it resolves Flow's advisory refs back to the exact persisted Vault capture before an injected strict component call.

Candidates remain `pending`, `shadow_only`, and cross-scan-untrusted. Exact same capture/plan replay is provider-free; missing accepted occurrence bindings force a full safe workset. The hook neither changes report bytes nor enters selector, API, Markdown, history, ranking, or UI paths. SQLite `sv9_scans`, public `report_store`, and legacy operational shadows remain rejected as judgment authority.
