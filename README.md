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
.venv/bin/python -m uvicorn web.app:app --port 8035
```

`http://127.0.0.1:8035` — submit a brand URL, watch the run (phases plus per-source acquisition steps), then read the evidence-first report: score, components, per-block coverage (`evidence / implied / verified absent / insufficient`), cited snippets, absence records, and acquisition attempts. Every stored report lands in the home list. Reports persist as JSON files under `data/reports/` (`B3S_REPORTS_DIR` overrides); the store moves to Postgres with the port milestone.

## Database

Storage today is SQLite (embedded, zero setup) inherited from the shared ancestry. B3S targets **Postgres** as its system of record; a local instance ships with:

```bash
docker compose up -d db   # postgres 16 on localhost:5433 (b3s/b3s)
```

Porting the store is the first infrastructure milestone. The inherited analysis lives in `docs/database_read_model_and_postgres_plan.md` (read models first, migrate what earns it).

## Status

Experimental. Scoring runs shadow-only. Contracts and policies are expected to change; policy changes must carry a changelog entry justified by a real captured case.

Known failures, kept visible on purpose: the inherited Visual Signature review/calibration tooling broke upstream during a module split (a syntax error and a lost re-export hid 14 tests behind collection errors; this fork surfaced them — `thresholds_for_scope()` call sites drifted and need repair, or the tooling gets removed). One legacy magnetism extractor test and one order-dependent worker test also fail.
