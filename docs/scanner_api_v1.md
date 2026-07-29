# B3S Scanner API v1

The B3S Scanner API is the stable application boundary around the evidence-first
scanner. It creates asynchronous scans and exposes normalized status, result,
evidence, and brand-history resources without exposing the scanner's internal
report JSON as a public contract.

## Base and documentation

Local base URL:

```text
http://127.0.0.1:8000/api/v1
```

Production client configuration:

```text
B3S_SCANNER_API_URL=https://b3s.fly.dev/api/v1
```

Interactive documentation and the dedicated OpenAPI document:

```text
GET /api/v1/docs
GET /api/v1/openapi.json
```

`B3S_SCANNER_API_URL` is the canonical API v1 base and includes `/api/v1`.
Clients must read it from configuration rather than hard-code the Fly hostname.
The repository's diagnostic scripts still accept a bare origin such as
`https://b3s.fly.dev` for backwards compatibility and normalize it to the same
v1 base.

## Authentication

Every product endpoint requires a Bearer token. Health, documentation, and the
OpenAPI document are public.

```http
Authorization: Bearer <token>
```

The current server token is configured with `BRAND3_SCANNER_API_TOKEN`.
`B3S_SCANNER_API_TOKEN` is accepted as a forward-compatible alias. Never put a
real token in source code, browser JavaScript, logs, or committed environment
files.

The v1 dependency model separates `scans:read`, `scans:write`, and
`evidence:adjudicate`. The scanner environment token receives the scan scopes
only. Evidence adjudication uses the distinct
`B3S_EVIDENCE_ADJUDICATION_TOKEN`; its reviewer is derived from
`B3S_EVIDENCE_REVIEWER_ID`. The same reviewer credential governs claim
relation reconciliation. Both tokens must differ.

## Create a scan

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $BRAND3_SCANNER_API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: leads-example-com-2026-07-20" \
  -d '{
    "url": "https://example.com",
    "brand_name": "Example",
    "language": "es",
    "allow_degraded_fallback": false
  }' \
  http://127.0.0.1:8000/api/v1/scans
```

The response is `202 Accepted` and contains:

- `Location`: canonical status resource.
- `Retry-After`: suggested polling interval.
- `Idempotent-Replayed: true` when the key replays an earlier request.
- `X-Request-ID`: correlation id for support and logs.

Only Spanish output is currently declared. Sending unsupported languages or
unknown request fields returns a contract validation error instead of silently
ignoring them.

### Idempotency

`Idempotency-Key` is optional but strongly recommended for every external
consumer. Reservations are persisted in SQLite and store only a SHA-256 hash of
the supplied key.

- Same key and same normalized request: returns the original scan.
- Same key and different request: `409 idempotency_key_reused`.
- No key: every request creates a new scan.

The normalized request includes URL, brand name, language, and degraded-fallback
policy.

## Poll and control a scan

```text
GET  /api/v1/scans/{scan_id}
POST /api/v1/scans/{scan_id}/continue
POST /api/v1/scans/{scan_id}/cancel
```

Public statuses are:

| Status | Meaning |
| --- | --- |
| `running` | Capture, interpretation, scoring, or report assembly is active. |
| `blocked` | Acquisition requires an explicit degraded-fallback decision. |
| `completed` | The immutable result is available. |
| `failed` | Execution failed or was interrupted by a process restart. |
| `cancelled` | Cancellation reached a terminal state. |

`continue` is only valid for a continuable blocked scan. `cancel` rejects scans
already in a terminal state.

## Read result and evidence

```text
GET /api/v1/scans/{scan_id}/result
GET /api/v1/scans/{scan_id}/evidence
```

Both resources are immutable after completion and return an `ETag`. Clients may
send `If-None-Match`; unchanged resources return `304 Not Modified`.

The result contract (`b3s-scanner-result-v1`) contains:

- Brand identity and canonical domain.
- Score, scale, base average, and reliability status.
- Executive reading and normalized components.
- Tile summaries and per-tile outcomes.
- Evidence references, not raw acquisition blobs.
- Acquisition coverage, limitations, and gate state.
- Scan-time evidence-stability classification and reason codes.
- Pipeline, rubric, prompt, and evaluator metadata.

When acquisition did not cover a component sufficiently, the result exposes
that state explicitly in `insufficient_evidence` (for example,
`["values"]`). This list is separate from `not_detected`: an item in
`not_detected` means the available evidence did not support the component,
whereas an item in `insufficient_evidence` means the scanner could not acquire
enough reliable evidence to make that claim. The component drawer and
`acquisition_summary` retain the detailed coverage status and diagnostics.

Evidence is a separate resource because clients often need citations without
the full editorial result. It exposes normalized references, verified absences,
acquisition attempts, and aggregate counts.

## Brand history

```text
GET /api/v1/brands/{domain}/scans?limit=20&offset=0
```

This returns completed immutable reports for the normalized domain, newest
first, with bounded offset pagination. Each item includes `reliability_status`,
`canonical_status`, `stability_classification`, and
`stability_reason_codes`. The list envelope includes
`canonical_report_id`, `provisional_report_id`, and `selected_report_id`.
Those fields are a current derived projection over immutable scans; they can
change when a later scan supplies comparison evidence.

## Evidence ledger shadow

```text
GET /api/v1/brands/{domain}/evidence-ledger-shadow
```

This experimental read-only resource compares exact normalized evidence across
the complete immutable history for a brand. It reports proposals such as
`observed`, `repeated`, `validation_candidate`, `not_reacquired`,
`changed_candidate`, and `stale_candidate`.

The resource is deliberately non-authoritative:

- `runtime_effect` is always `false`.
- A `validation_candidate` is not a validated fact.
- One missing acquisition never retires earlier evidence.
- Changed content at the same locator is not treated as a semantic
  contradiction.
- Repetition of syndicated external content is not independent corroboration.
- No ledger state changes a scan score, report content, or canonical selection.

`persistence.stored=true` means the returned projection was loaded from the
PostgreSQL shadow tables and matches the current immutable history.
`history_derived` means it was recomputed for the response, usually because
PostgreSQL was unavailable or its projection had not caught up. Set
`B3S_EVIDENCE_LEDGER_MODE=disabled` to stop computing and persisting entries.

## Evidence memory identity v2 shadow

```text
GET /api/v1/brands/{domain}/evidence-memory-identity-v2-shadow
```

This authenticated experimental projection fixes one specific v1 ambiguity:
several passages from one URL are not treated as temporal revisions. It
separates document, passage, optional stable slot, and proposed adjudication.

Its safety boundary is explicit:

- `runtime_effect` and `authority` are always `false`;
- URL equality never implies revision;
- revision proposals require a stable slot;
- weak external identity remains unverified;
- a bare upstream identity label remains unverified, while eligible external
  identity requires reproducible persisted attribution provenance;
- exact copies, reviewed publisher groups, explicit source lineage, and high
  deterministic shingle similarity share a conservative source cluster;
- ambiguous similarity is `disputed`, missing ownership/source review is
  `unknown`, and only `confirmed_independent` may appear in the prospective
  independence count;
- no state changes scores, reports, canonical selection, or v1 ledger rows.

`persistence.stored` is always `false` in this phase. The API recomputes the v2
projection from immutable brand history so every later scan is observable
without adding another authoritative store. Entries contain hashes and
provenance but no raw evidence text.

The top-level `source_independence` object pins the v3 policy, publisher
registry fingerprint, thresholds, and `runtime_effect=false` boundary. Internal
shingle hashes are not returned. The committed registry contains no
production-reviewed source URLs, so real evidence fails closed rather than
gaining independence from missing relationship data.

The resource is not proof that real brand changes are detected. The current
local corpus now yields three explicit semantic slots through deterministic
historical backfill, but none contains multiple variants, so semantic-change
recall remains unmeasured.

## Evidence Claim Memory v1 shadow

```text
GET /api/v1/brands/{domain}/evidence-claim-memory-shadow
```

This authenticated, read-only projection separates three identities:

- `claim_slot_id`: the stable semantic subject for a brand/entity scope;
- `claim_variant_id`: one normalized content variant in that slot;
- `claim_occurrence_id`: one variant observation in one immutable report.

A slot requires `metadata.claim_slot_key`, or a legacy `metadata.claim_id`
explicitly declared with `metadata.claim_id_semantics=stable_slot`. Bare claim
IDs, visual tiles, and `checked_block` metadata are reported as ignored rather
than promoted into semantic claim identity.

New `sv9-flow-candidate-v2` reports may also contain the separate
`candidate.claim_memory_evidence` shadow lane. Its v2 producer emits only
explicit owned mission and vision declarations. Those records are not added
to the runtime evidence pack and cannot affect interpretation, tiles, scores,
or canonical selection.

For immutable `sv9-flow-candidate-v1` reports that predate the lane, Claim
Memory may derive the same narrow records at read time from their persisted
evidence pack. Reports are never edited. A persisted lane has precedence,
including an empty list, and a v2 or unknown-schema report with a missing lane
fails closed. The response exposes derivation-mode and producer-version
counters so persisted and historical records remain distinguishable.

Sequential variants may produce a `replacement_candidate`; variants present
in the same report may produce a `coexistence_candidate`. Both remain
`adjudication_state=proposed`, `runtime_effect=false`, and `authority=false`.
The API never chooses a canonical claim, changes a score, or returns raw claim
text.

`persistence.stored` is always `false` in v1. The projection is rebuilt from
immutable history; existing evidence-identity adjudications are overlaid as
variant provenance and current claim-reconciliation decisions are overlaid on
stable historical relation candidates.

## Evidence → claim → tile ledger shadow

```text
GET /api/v1/brands/{domain}/evidence-claim-tile-ledger-shadow
```

This authenticated resource reuses support relationships already recoverable
from immutable reports. A mapping requires a semantic claim occurrence, a
literal tile quote in that claim's source evidence, a claim-text anchor, and
one unambiguous source/claim candidate. Same-page similarity is rejected.

Every mapping exposes its polarity, source-independence status, first/last
observation, persistence state, mapping version, and evaluator/policy series.
Raw claim and quote text are omitted; only stable identities and quote hashes
are returned.

The resource is persistent but non-authoritative:

- `runtime_effect=false`;
- `authority=false`;
- exact repeats add observations, never breadth or points;
- evaluator or pinned-policy changes create another mapping series;
- no mapping changes existing tiles, scores, reports, or canonical selection.

`persistence.stored=true` means PostgreSQL has a payload matching the current
immutable-history fingerprint. Otherwise the API returns the deterministic
`history_derived` projection. Set
`B3S_EVIDENCE_CLAIM_TILE_LEDGER_MODE=disabled` to stop projection and
persistence.

## Evidence claim reconciliation journal

```text
GET  /api/v1/brands/{domain}/evidence-claim-reconciliations
POST /api/v1/brands/{domain}/evidence-claim-reconciliations
```

Writes require the dedicated evidence reviewer credential, an
`Idempotency-Key`, and an explicit `expected_current_event_id` (`null` means
the caller expects no current event). The request identifies a projected
`relation_candidate_id`; the server resolves its relation type and binds the
configured reviewer rather than trusting client-supplied identity or relation
metadata.

The append-only PostgreSQL journal supports `accepted`, `disputed`, `rejected`,
and `revoked`. Older events remain visible as `superseded`. A write fails with
`503` when durable storage is unavailable; there is no JSON fallback.

Every event and overlay remains `runtime_effect=false` and `authority=false`.
Accepting `replacement_candidate` records a review decision but does not select
the newer variant, alter a tile, or change a score.

## Errors

API errors use one envelope:

```json
{
  "error": {
    "code": "scan_result_not_ready",
    "message": "The scan result is not available yet.",
    "request_id": "9f36ac9ac74f4ab78f9c0e91cf912ba7",
    "details": {
      "scan_id": "12ab34cd56ef",
      "state": "running"
    }
  }
}
```

Validation errors use the same envelope and include a bounded list of field
errors. Authentication failures include `WWW-Authenticate: Bearer`.

## Current durability boundary

Status envelopes and idempotency reservations are durable. Completed results
are immutable and mirrored to the configured report history store.

Execution is still performed by a background thread in the FastAPI process. A
restart cannot resume that thread. At startup, B3S converts orphaned `accepted`,
`running`, or `blocked` jobs into `failed` with code `process_restarted`. This is
deliberate: the API never claims that an unknown or duplicated job continued.

The next infrastructure milestone is a durable worker queue. It can replace the
process-bound executor without changing the v1 HTTP resources.
