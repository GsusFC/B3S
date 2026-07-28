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
`B3S_EVIDENCE_REVIEWER_ID`. Both tokens must differ.

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
local corpus contains visual stable slots but no stable semantic `claim_id`
history, so semantic-change recall remains unmeasured.

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
