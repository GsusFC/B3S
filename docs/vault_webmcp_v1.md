# Vault WebMCP v1

## Purpose

Vault WebMCP exposes existing B3S Vault browser capabilities as first-party tools for an agent operating inside the authenticated web application. It is an interaction layer, not a new authorization system, API authority, evidence contract, or scoring path.

The browser session remains authoritative. Every tool uses the same routes, forms, and handlers already used by the web interface, with the existing Google OIDC, reviewer-session, same-origin, validation, and persistence boundaries intact.

## Surfaces

### FLOC surface

Entry point: the authenticated Vault index at `/`.

| Tool | Existing boundary | Effect | Risk |
| --- | --- | --- | --- |
| `b3s_list_brand_analyses` | `GET /` | Reads the visible brand-analysis index | Read-only |
| `b3s_get_brand_analysis` | `GET /brand/{domain}` | Reads score, status, evidence links, history, and Vault memory | Read-only |
| `b3s_read_report_markdown` | `GET /report/{report_id}.md` | Reads one persisted report with a bounded output | Read-only |
| `b3s_get_scan_status` | `GET /api/scan/{scan_id}` | Reads current scan phases, acquisition, and gate state | Read-only |
| `b3s_prepare_scan` | Visible `POST /scan` form | Fills URL, brand name, and degraded-fallback choice without submitting | Reversible browser state; human submit required |
| `b3s_continue_degraded_scan` | `POST /api/scan/{scan_id}/continue` | Continues a blocked scan after re-reading its gate | Consequential; requires `confirm=true` |
| `b3s_cancel_scan` | `POST /api/scan/{scan_id}/cancel` | Cancels a running or blocked scan after re-reading its state | Consequential; requires `confirm=true` |

Read results containing captured, page, provider, or user-controlled text are marked as untrusted content.

`b3s_prepare_scan` deliberately does not submit the scan. The ordinary browser `POST /scan` path creates a fresh scan ID for every request and has no durable idempotency key. Allowing an agent to call it directly would make transport or model retries capable of starting duplicate acquisitions and spending provider credits twice. A direct WebMCP launch must wait for a session-authenticated, request-bound, durable idempotency boundary; client-side deduplication is not sufficient.

### Protected reviewer surface

Entry point: an authenticated page under `/vault/review/{domain}`.

| Tool | Existing boundary | Effect | Risk |
| --- | --- | --- | --- |
| `b3s_list_vault_review_cases` | `GET /vault/review/{domain}?state=...` | Reads exact review cases, tile contracts, quotes, sources, and status | Read-only |
| `b3s_prepare_vault_review_decision` | Current review form | Selects a decision and fills its rationale in the visible form | Reversible browser state only |

`b3s_prepare_vault_review_decision` never submits the form. A human reviewer must inspect the prepared decision and press the existing signed-registration button. WebMCP therefore cannot impersonate a reviewer or create an authority event by itself.

## Explicit exclusions

Vault WebMCP v1 cannot:

- bypass Google OIDC, the protected reviewer session, same-origin checks, or server validation;
- read or expose cookies, bearer tokens, API keys, credentials, environment variables, or raw database access;
- call the token-authenticated Scanner API v1 by manufacturing browser credentials;
- directly start a scan through the replay-unsafe browser form;
- accept, reject, revoke, or sign evidence-review decisions automatically;
- modify immutable captures, evidence lineage, fingerprints, historical reports, or append-only journals;
- change tile contracts, SV9 weights, scoring authority, canonical selection, publication gates, or Phase Zero/One/Two boundaries;
- publish, delete, migrate, deploy, change permissions, or operate infrastructure;
- turn shadow, diagnostic, preview, or non-authoritative data into public scoring authority.

These exclusions are enforced by omission from the tool catalog and by reusing the existing application boundaries rather than introducing privileged browser endpoints.

## Loading boundary

The loader activates only on `https://b3s-vault.fly.dev`. Local development is opt-in through `?webmcp=1` on `localhost` or `127.0.0.1`. Other deployments may render the shared templates, but the WebMCP module is not loaded there.

No polyfill or third-party runtime is shipped. If the browser does not expose `navigator.modelContext` or `document.modelContext`, the integration becomes a no-op and the normal B3S interface keeps working.

## Contract rules

- Every input schema is closed with `additionalProperties: false`.
- Execute-time validation independently rejects unknown fields, missing fields, wrong types, invalid enums, and out-of-range integers.
- Outputs are compact JSON-serializable values, never HTML documents, DOM nodes, secrets, cookies, or headers.
- Read-only tools never mutate B3S state.
- Scan preparation reads back the actual form values and reports `submitted=false` and `requiresHumanSubmit=true`.
- Continue and cancel re-read current server state before acting and read it again afterward.
- Review preparation reports the actual state of the visible form and never returns a signed-success shape.

## Validation

Focused repository checks:

```bash
python -m pytest tests/test_vault_webmcp.py -q
node --check web/static/vault_webmcp_loader.js
node --check web/static/vault_webmcp.js
```

Stagehand discovery for the FLOC surface can be run against a local app with WebMCP explicitly enabled:

```bash
node "$ADD_WEBMCP_SKILL_DIR/scripts/validate-stagehand.mjs" \
  --url 'http://localhost:8000/?webmcp=1' \
  --config webmcp.e2e.json \
  --local
```

The FLOC configuration invokes one read-only lookup and the reversible scan-preparation tool. Continue and cancel remain discovery-only and must not be invoked without an explicitly authorized disposable scan. The reviewer configuration is `webmcp.review.e2e.json`; it must be run in an authenticated disposable reviewer session.
