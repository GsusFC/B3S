# B3S PostgreSQL History v1

## Decision

PostgreSQL is the system of record for B3S brand history. The schema is relational for identity, ordering, scores and provenance; JSONB retains variable provider and model payloads; binary artifacts remain references rather than database blobs.

The implementation lives in `src/history/` and uses the isolated `b3s_history` schema. PostgreSQL is the primary historical read model when `B3S_DATABASE_URL` is configured. Completed scans are persisted to PostgreSQL and JSON at report completion; JSON remains the compatibility fallback until the scan lifecycle cutover described below. A conflicting immutable report id aborts before the JSON fallback is changed.

The same repository also contains the Vault operational, lineage, verified-raw provenance, immutable planning-context, and strict raw-replay contracts through migration `023`. Those paths can persist exact reviewed authority inside the isolated Vault history, but do not grant production, scanner, scoring, or presentation authority. The verified-raw worker, operational pipeline, and C7 runtime cutover remain dormant unless their separate explicit deployment capabilities are enabled.

## Historical semantics

B3S records two different timelines:

- Observation history: what the platform captured from a brand at `captures.observed_at`.
- Evaluation history: how a specific capture was interpreted at `evaluation_runs.evaluated_at` with a particular rubric, prompt and model.

Captures and evaluations are append-only. Re-evaluating an old capture creates a new evaluation row; it does not create a new observation and does not replace the original evaluation.

`b3s_history.brand_history` selects the latest evaluation for every capture. `b3s_history.brand_current_state` then selects the latest observed capture for each brand. Ordering by observation time before evaluation time prevents a newly re-evaluated old capture from becoming the apparent current brand state.

`brand_current_state` deliberately preserves those chronological semantics.
Temporal publication is a separate derived projection in
`brand_canonical_selections`: the latest observation and the selected
canonical/provisional reference are related but not interchangeable.

## Tables

- `workspaces`: tenancy boundary prepared before authentication and RLS.
- `brands`: stable identity grouped by canonical domain.
- `scan_runs`: execution lifecycle and source report identity.
- `captures`: immutable observed state and acquisition payload.
- `evidence_records`: individually addressable evidence with source class and content hash.
- `evaluation_runs`: versioned rubric, prompt, model, score and reliability.
- `block_interpretations`: detected strategic blocks.
- `block_evidence_links`: relational provenance from a block to evidence.
- `component_evaluations`: SV9 result per component.
- `tile_verdicts`: per-tile state, quote and optional evidence reference.
- `acquisition_attempts`: failed, skipped or degraded provider attempts.
- `artifacts`: screenshot and visual artifact references.
- `report_snapshots`: complete report projection for exact retrieval and migration rollback.
- `capture_fingerprints`: normalized evidence identity and acquisition profile.
- `evaluation_comparisons`: derived baseline/previous comparison and reason codes.
- `brand_canonical_selections`: current canonical or provisional reference per brand.
- `evidence_ledger_shadow_states`: current non-authoritative longitudinal
  evidence projection per brand.
- `evidence_ledger_shadow_entries`: exact evidence identities and proposed
  shadow states.
- `evidence_ledger_shadow_observations`: captures supporting each shadow entry.
- `evidence_memory_adjudication_events`: append-only evidence-to-brand identity
  review history.
- `evidence_claim_reconciliation_events`: append-only decisions over stable
  claim-relation candidates.
- `evidence_claim_tile_ledger_states`: current non-authoritative mapping
  projection per brand.
- `evidence_claim_tile_mapping_series`: immutable evaluator/policy identity for
  evidence-to-claim-to-tile mappings.
- `evidence_claim_tile_mappings`: polarity, source independence, lifecycle, and
  hashed quote provenance for each unique mapping.
- `evidence_claim_tile_mapping_observations`: capture-level persistence for a
  mapping.
- `evidence_claim_tile_review_events`: append-only semantic decisions over
  immutable claim-to-tile mapping ids, including their evidence, claim variant,
  tile, polarity, and mapping-series snapshot.
- `evidence_vault_*`: versioned canonical and operational memory, score
  evaluations, plans, exact relation reviews, capture lineage, verified raw
  receipts/bindings/dispositions, and bounded C7 shadow-read projections.

## Invariants

- `(workspace_id, source_report_id)` is unique.
- Reimporting identical content is a no-op.
- Reusing a report id with a different SHA-256 raises `ReportConflictError`.
- Evidence references are unique inside a capture.
- Block references must resolve to evidence before import.
- A scan has one observed capture; a capture can have many evaluation revisions.
- Historical records are deleted only through explicit parent deletion, never by routine import.
- Stability recomputation updates only derived comparison/selection tables; immutable report payloads are returned exactly as imported.
- Evidence-ledger rebuilds run inside a savepoint, never affect scoring or
  canonical selection, and cannot abort an authoritative report import.
- Claim-to-tile rebuilds use a separate savepoint, retain older versioned
  mapping series, return no raw claim/quote text, and enforce
  `runtime_effect=false` and `authority=false`.
- Evidence adjudication, claim reconciliation, scoring recovery, and
  claim-to-tile review use separate append-only journals with idempotency,
  optimistic concurrency, explicit supersession, revocation, and
  database-enforced `runtime_effect=false` and `authority=false`.
- Claim-to-tile review also enforces `automatic_tile_effect=false` and
  `automatic_scoring_effect=false`; its foreign key can reference only a
  mapping already present in immutable brand history.

PostgreSQL cannot represent NUL inside `text` or `jsonb`. Imported text replaces NUL with U+FFFD for querying, while `evidence_records.content_raw` and `report_snapshots.payload_raw` preserve canonical original bytes. Hashes are calculated from the unsanitized content.

## Local operation

```bash
docker compose up -d db
.venv/bin/python scripts/import_b3s_reports_postgres.py --dry-run
.venv/bin/python scripts/import_b3s_reports_postgres.py --migrate-only
.venv/bin/python scripts/import_b3s_reports_postgres.py
B3S_EVIDENCE_CLAIM_TILE_LEDGER_MODE=shadow \
  .venv/bin/python scripts/import_b3s_reports_postgres.py \
  --migrate-only \
  --rebuild-evidence-claim-tile-ledger-shadow
```

`B3S_DATABASE_URL` defaults to `postgresql://b3s:b3s@localhost:5433/b3s`.

The importer validates every JSON before connecting. It processes reports oldest first so the brand identity projection converges on the latest observation, although the database upsert also guards against out-of-order imports.

Compatible Brand3 SQLite history uses a separate importer and a fixed archive
workspace:

```bash
# Read-only validation; no PostgreSQL connection.
.venv/bin/python scripts/import_brand3_sqlite_postgres.py \
  /path/to/brand3.sqlite3

# Explicit persistence into b3s-archive.
.venv/bin/python scripts/import_brand3_sqlite_postgres.py \
  /path/to/brand3.sqlite3 --apply
```

The adapter selects one latest evaluation for every persisted capture so model
reruns cannot become false observations. It never writes the SQLite source,
never imports legacy five-dimension scores, and does not expose archive brands
through the default `b3s` workspace. The measured real-archive result and
safety boundary are documented in
[`brand3_sqlite_archive_import_v1.md`](brand3_sqlite_archive_import_v1.md).

## Testing

Parser tests do not require PostgreSQL. Integration tests are opt-in locally and run against PostgreSQL 16 in CI:

```bash
B3S_TEST_DATABASE_URL=postgresql://... \
B3S_ALLOW_SCHEMA_DROP=1 \
B3S_TEST_ALLOW_SCHEMA_DROP=1 \
  .venv/bin/python -m pytest tests/test_b3s_history.py -q
```

The Brand3 archive integration uses the same disposable-database guard:

```bash
B3S_TEST_DATABASE_URL=postgresql://... \
B3S_ALLOW_SCHEMA_DROP=1 \
B3S_TEST_ALLOW_SCHEMA_DROP=1 \
  .venv/bin/python -m pytest \
  tests/test_brand3_sqlite_memory_backfill.py::test_archive_import_cli_is_idempotent_and_workspace_isolated \
  -q
```

PostgreSQL integration tests may drop and recreate `b3s_history` and create temporary roles or test objects. They must target a disposable database. CI exports both destructive-test guards: `B3S_ALLOW_SCHEMA_DROP=1` for the history/provenance suites and `B3S_TEST_ALLOW_SCHEMA_DROP=1` for the Vault repository suites.

It also exercises the legacy local/importer migration entry point:

```text
python scripts/import_b3s_reports_postgres.py --migrate-only
```

Fly release commands do not invoke this path; they verify the exact head with
SELECT-only runtime access after a separately authorized external migration.

The first run must apply every packaged migration through
`023_evidence_vault_raw_replay_projection.sql`; the second must apply
none. Current PostgreSQL integration coverage includes base history/import,
Brand3 archive isolation, Vault operational persistence and execution, capture
lineage, verified-raw provenance and roles, bounded C7 shadow readiness, and
populated-schema upgrades. It proves local/CI database contracts, not a Fly
release, a provisioned acquisition worker, or runtime C7 cutover.

## Cutover boundary

For the main scanner, PostgreSQL serves imported history and mirrors completed reports, but is not yet the transactional live scan writer. Vault-specific persistence and provenance integrations do not change that boundary; the Fly worker/socket path is disabled and both C7 runtime-read methods remain fail-closed. The remaining controlled scan-lifecycle cutover is:

1. Add persistent scan lifecycle methods to `PostgresHistoryRepository`.
2. Write scan start, capture completion and evaluation completion transactionally.
3. Save the report snapshot in the same evaluation transaction instead of mirroring after JSON completion.
4. Run a local/staging scan and compare its PostgreSQL projection with the existing JSON.
5. Keep JSON export as rollback until parity is proven, then remove file-backed writes.

RLS should be enabled when authentication introduces a trusted database workspace context. Declarative partitioning is deliberately deferred until measured row counts and query plans justify it.
