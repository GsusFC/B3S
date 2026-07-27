# B3S — Brand Evidence Lab

B3S explores one thesis: a brand scanner should be an **evidence lab**, not a scorer with a crawler. Acquisition observes, proves, and records absence; interpretation only speaks with citations; every number must survive the question *"show me the evidence"*.

B3S shares ancestry with the Brand3 codebase but is a different product idea, developed independently. Occasional fixes can still be transplanted between the two with `git cherry-pick` thanks to the common history.

## The evidence-first flow

```text
capture (web / Exa / GitHub proof / SearchAPI fallback)
  → BrandEvidencePack (citable records + attempt/absence diagnostics)
  → deterministic block shortlists
  → gated LLM interpretation (detected requires refs; structural gate is veto-only)
  → tile signals → SV9 score
  → evidence coverage (positive / implied_not_explicit / verified_absent / insufficient_acquisition)
```

Key properties, all enforced by contract or policy data:

- A block is only `detected` with content **and** evidence refs; ungrounded detections are demoted at the SV9 boundary.
- Absence is evidence: crawled strategic pages without values/vision terms emit `acquisition.absence.*` records, and failed external acquisition leaves `acquisition.attempt.*` records.
- `verified_absent` is derived deterministically — the LLM cannot self-certify an absence.
- Keyword policies are versioned data with a justified changelog (`src/sv9_flow/policy_data/calibration_terms.json`), not code drift.

## Layout

- `src/sv9_flow/` — the evidence-first flow (stdlib-only, versioned contracts)
- `src/sv9/` — SV9 evaluator: tiles, components, score
- `src/collectors/` + `src/services/` — capture: web crawl, Exa, GitHub org/repo proof, SearchAPI vertical fallback
- `scripts/sv9_flow_snapshot_eval.py` — run the flow over a frozen envelope (no DB, no crawling, no credits)
- `fixtures/` — real captured envelopes and shadow outputs (Vercel, Mercury) for deterministic runs

## Quickstart

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env   # add provider keys for live capture (not needed for fixtures)
PYTHONPATH=. .venv/bin/python -m pytest tests/ -q
PYTHONPATH=. .venv/bin/python scripts/sv9_flow_snapshot_eval.py \
  fixtures/vercel/vercel_fresh_capture_envelope.json --repeat 1 --output /tmp/vercel_eval.json
```

## Web lab

```bash
.venv/bin/python -m uvicorn web.app:app --port 8000
```

`http://127.0.0.1:8000` — submit a brand URL, watch the run (phases plus per-source acquisition steps), then read the evidence-first report: score, components, per-block coverage (`evidence / implied / verified absent / insufficient`), cited snippets, absence records, and acquisition attempts. Every stored report lands in the home list. When `B3S_DATABASE_URL` is configured, PostgreSQL serves the historical read model and mirrors completed reports; JSON files under `data/reports/` (`B3S_REPORTS_DIR` overrides) remain the compatibility writer and rollback fallback until the scan lifecycle cutover.

## Scanner API v1

B3S exposes a versioned asynchronous API for product integrations:

```text
POST /api/v1/scans
GET  /api/v1/scans/{scan_id}
GET  /api/v1/scans/{scan_id}/result
GET  /api/v1/scans/{scan_id}/evidence
```

Bearer authentication uses `BRAND3_SCANNER_API_TOKEN`. Create requests support
durable `Idempotency-Key` reservations, status survives restarts, errors share a
stable envelope, and completed result/evidence resources provide ETags.

- Interactive docs: `http://127.0.0.1:8000/api/v1/docs`
- Dedicated OpenAPI: `http://127.0.0.1:8000/api/v1/openapi.json`
- Full contract and examples: [`docs/scanner_api_v1.md`](docs/scanner_api_v1.md)

## Database

The B3S historical model is implemented in **PostgreSQL** under the isolated `b3s_history` schema. It stores immutable captures separately from versioned evaluations so re-scoring an old capture cannot look like a new brand observation. A local instance ships with:

```bash
docker compose up -d db   # postgres 16 on localhost:5433 (b3s/b3s)
```

Validate and import the current file-backed reports:

```bash
.venv/bin/python scripts/import_b3s_reports_postgres.py --dry-run
.venv/bin/python scripts/import_b3s_reports_postgres.py
```

The import is idempotent and rejects a reused report id with different content. The schema, invariants, queries and cutover boundary are documented in `docs/b3s_postgres_history_v1.md`.

## Temporal evidence stability

Repeated scans are compared against the first non-invalid baseline using normalized evidence, independently from LLM prose and scores. A weaker acquisition or a changed evaluation over materially equivalent evidence is retained in history but cannot silently replace the selected report.

Preview the policy globally without writes:

```bash
.venv/bin/python scripts/canonical_evidence_dry_run.py --format markdown
```

`B3S_CANONICAL_ENFORCEMENT_MODE` controls publication: `observe` only classifies, `repeated` enforces the selected baseline for brands with at least two scans, and `all` also enforces single-scan histories. Fly starts with `repeated`; local development defaults to `observe`. The contract and rollback procedure are documented in [`docs/canonical_evidence_stability.md`](docs/canonical_evidence_stability.md).

## Deployment

Fly deploys use the GitHub `production` environment and its `FLY_API_TOKEN` secret. The `Fly Deploy` workflow is manual from `main` while the guarded rollout is active. Automatic deploys after successful CI remain disabled until the repository variable `AUTO_DEPLOY_ENABLED` is explicitly changed from `false` to `true`.

Every deploy validates `fly.toml`, builds the committed Dockerfile and runs the idempotent PostgreSQL migration as Fly's `release_command` before replacing the application Machine.

## Status

Experimental. Scoring runs shadow-only. Contracts and policies are expected to change; policy changes must carry a changelog entry justified by a real captured case.

Known failures, kept visible on purpose: the inherited Visual Signature review/calibration tooling broke upstream during a module split (a syntax error and a lost re-export hid 14 tests behind collection errors; this fork surfaced them — `thresholds_for_scope()` call sites drifted and need repair, or the tooling gets removed). One legacy magnetism extractor test and one order-dependent worker test also fail.
