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

Interactive documentation and the dedicated OpenAPI document:

```text
GET /api/v1/docs
GET /api/v1/openapi.json
```

The hostname is deployment configuration. Clients should read it from an
environment variable such as `B3S_SCANNER_API_URL`; they must not hard-code the
current Fly hostname.

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

The v1 dependency model already separates `scans:read` and `scans:write`. The
current environment token receives both scopes; per-client credentials can be
introduced later without changing endpoint contracts.

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
- Pipeline, rubric, prompt, and evaluator metadata.

Evidence is a separate resource because clients often need citations without
the full editorial result. It exposes normalized references, verified absences,
acquisition attempts, and aggregate counts.

## Brand history

```text
GET /api/v1/brands/{domain}/scans?limit=20&offset=0
```

This returns completed immutable reports for the normalized domain, newest
first, with bounded offset pagination.

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
