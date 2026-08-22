# ScrapeCreators social acquisition spike

## Verdict

**Capability verdict: conditionally capable for an isolated, exact-target lab run; not approved for production integration.**

The implementation establishes, without network access, that the client and normalization boundary can:

- address an explicit LinkedIn company URL and explicit X/Twitter and Instagram handles;
- call the six required ScrapeCreators surfaces;
- keep authentication environment-only and out of emitted data and errors;
- retry transient failures within fixed bounds;
- normalize profiles, official posts, and explicitly supported interactions into
  the provider-neutral `b3s-social-community-lab-v1` contract; and
- write a single atomic mode-`0600` artifact, with provider payloads excluded by default.

It does **not** establish live availability, payload completeness, credit cost, private/restricted-account behavior, rate-limit behavior, pagination yield, or response-contract stability. No live request was made and no credential was used while implementing this spike. A representative live capability matrix is required before any integration decision.

The live result contains both `observations` (contract-first records) and a
`capability_matrix` keyed by target and actor role. Matrix entries are always
explicitly one of `acquired`, `partial`, `not_acquired`, or `failed`, and carry
a reason plus endpoint/response provenance. X/Twitter interaction roles remain
`not_acquired` because no verified reply-tree endpoint is used.

## Isolation boundary

This is lab code only:

- core: `src/research/scrapecreators_spike.py`
- CLI: `scripts/scrapecreators_social_spike.py`
- manifest example: `examples/scrapecreators_social_targets.example.json`
- tests: `tests/test_scrapecreators_spike.py`

Nothing imports this module from Scanner, Evidence Vault, workers, the web app, or runtime configuration. The spike does not write databases and does not use the repository's config or API-key-pool surfaces.

## Endpoint contract

The paths and parameters were checked against the ScrapeCreators documentation/OpenAPI surface on 2026-08-13:

| Platform | Record | Request |
| --- | --- | --- |
| LinkedIn | company profile | `GET /v1/linkedin/company?url=<exact-company-url>` |
| LinkedIn | company posts | `GET /v1/linkedin/company/posts?url=<exact-company-url>&page=1` |
| X/Twitter | profile | `GET /v1/twitter/profile?handle=<exact-handle>` |
| X/Twitter | posts | `GET /v1/twitter/user-tweets?handle=<exact-handle>` |
| Instagram | profile | `GET /v1/instagram/profile?handle=<exact-handle>` |
| Instagram | posts | `GET /v2/instagram/user/posts?handle=<exact-handle>` |
| LinkedIn | post comments | `GET /v1/linkedin/post?url=<exact-post-url>` |
| Instagram | post comments | `GET /v2/instagram/post/comments?url=<exact-post-url>[&cursor=...]` |
| Instagram | nested replies | `GET /v1/instagram/post/comment/replies?url=<exact-post-url>&comment_id=<id>[&cursor=...]` |

References:

- <https://docs.scrapecreators.com/v1/linkedin/company>
- <https://docs.scrapecreators.com/v1/linkedin/company/posts>
- <https://docs.scrapecreators.com/v1/twitter/profile>
- <https://docs.scrapecreators.com/v1/twitter/user-tweets>
- <https://docs.scrapecreators.com/v1/instagram/profile>
- <https://docs.scrapecreators.com/v2/instagram/user/posts>

The spike intentionally fetches only the first LinkedIn/Instagram posts page. It performs no person/company lookup, keyword discovery, or name-to-handle resolution.

Interaction requests are opt-in (`--include-interactions`). Instagram nested
replies require a second explicit opt-in (`--include-replies`) together with a
bounded page and credit budget (`--max-interaction-pages` and
`--max-interaction-credits`); the provider documents this path as slower and
more expensive. Ambiguous author identity or missing parent/thread linkage is
admitted only as `unclassified`, never guessed as a community response or brand
reply.

The acquisition worker reserves the full conservative request ceiling before
each interaction request: `15` credits for Instagram comments with
`include_replies=true`, and `1` credit for LinkedIn post detail, Instagram
comments without reply expansion, and Instagram comment replies. Unknown
interaction modes fail closed. Provider charges are still accounted exactly;
they never release a reservation, and an actual charge above its declared
ceiling is surfaced as a diagnostic. Missing, zero, or underreported provider
charge metadata therefore cannot authorize another request.

## Manifest contract

The manifest is strict JSON with `schema_version: 1` and a non-empty `targets` list. Unknown fields are rejected, so a brand `name`, search query, alternate credential, or provider option cannot silently enter the request contract.

```json
{
  "schema_version": 1,
  "targets": [
    {
      "target_id": "example-linkedin",
      "platform": "linkedin",
      "company_url": "https://www.linkedin.com/company/example"
    },
    {
      "target_id": "example-twitter",
      "platform": "twitter",
      "handle": "example"
    },
    {
      "target_id": "example-instagram",
      "platform": "instagram",
      "handle": "example"
    }
  ]
}
```

LinkedIn accepts only an HTTPS `linkedin.com` `/company/<slug>` URL without a query or fragment. X/Twitter and Instagram accept bare handles, not profile URLs, `@` prefixes, names, or search strings. Duplicate IDs and duplicate platform identifiers are rejected.

## Runbook

### 1. Validate the plan without a credential or network

From the repository root:

```bash
.venv/bin/python scripts/scrapecreators_social_spike.py \
  --manifest examples/scrapecreators_social_targets.example.json \
  --dry-run \
  --output out/scrapecreators-social-spike/plan.json

stat -f '%Sp %N' out/scrapecreators-social-spike/plan.json
```

Dry-run does not instantiate the HTTP client, does not read `SCRAPECREATORS_API_KEY`, and makes no request. Inspect the planned paths and exact query parameters before a live run.

### 2. Put the credential in the process environment without echoing it

Do not pass a key as a CLI argument, put it in the manifest, or add it to repository config. One shell-safe interactive option is:

```bash
read -s SCRAPECREATORS_API_KEY
export SCRAPECREATORS_API_KEY
printf '\n'
```

The client has no API-key constructor argument. It creates only the `x-api-key` header and reads its value only from `SCRAPECREATORS_API_KEY`.

### 3. Run the bounded live probe

```bash
umask 077
.venv/bin/python scripts/scrapecreators_social_spike.py \
  --manifest path/to/reviewed-targets.json \
  --output out/scrapecreators-social-spike/result.json
```

Defaults are a 20-second HTTPX operation/inactivity timeout, a 5-second connect timeout, and at most three attempts. Retries apply only to transport failures and HTTP `408`, `425`, `429`, `500`, `502`, `503`, and `504`. Exponential delays and numeric `Retry-After` values are capped at two seconds. CLI bounds prohibit more than four attempts or an operation timeout over 60 seconds. This is **not** a total wall-clock deadline; run the CLI under an external process deadline for an adversarial or representative live matrix.

Each endpoint failure is recorded with a body-free safe reason and acquisition continues, so the artifact retains the rest of the capability matrix. Provider `credits_charged` and `credits_remaining` are captured per successful request when supplied. The CLI prints only counts. It does not print requests, responses, target values, output paths, or credentials.

### 4. Include redacted provider payloads only when required

```bash
.venv/bin/python scripts/scrapecreators_social_spike.py \
  --manifest path/to/reviewed-targets.json \
  --include-raw \
  --output out/scrapecreators-social-spike/result-with-redacted-raw.json
```

Default output contains no provider payload. `--include-raw` adds `raw_payload_redacted`, not an unfiltered wire body. Credential-shaped fields are replaced with `[REDACTED]`, and any occurrence of the active credential in a string is replaced before normalization/output. The exact response is retained only as `raw_response_sha256` (SHA-256 of the response bytes exposed by `httpx`). Treat the mode-`0600` artifact as sensitive even after redaction.

### 5. Remove the credential from the shell

```bash
unset SCRAPECREATORS_API_KEY
```

## Output and normalization

`atomic_write_json` writes a temporary file beside the destination, applies mode `0600`, flushes and `fsync`s it, atomically replaces the destination, and reapplies mode `0600`. Interrupted live runs are never written by the CLI; completed runs may contain a deliberate per-endpoint success/failure matrix.

The CLI accepts destinations only beneath the repository's ignored
`out/scrapecreators-social-spike/` root. Absolute/relative traversal, sibling or
canonical paths, and symlink escapes are rejected before parent creation or
writing. The default and accepted explicit filenames remain atomic mode-`0600`
files inside that root.

Each normalized profile/post/interaction has:

- a fixed semantic schema (`platform`, external ID, canonical URL, text/profile fields, selected metrics);
- `content_id: sha256:<digest>`, calculated by the accepted B1 contract from
  metric-free semantic fields;
- `source_response_sha256`, tying the record to its provider response; and
- `target_id`, tying the observation to the manifest without changing the content address.

Contract observations additionally bind provider and endpoint, target and
manifest fingerprints, request fingerprint, fetch timestamp, page/cursor,
ordinal, exact target identity, normalization version, and linkage evidence in
`provenance`. Metrics remain in `metric_context`; changing likes, followers,
credits, pagination, or fetch metadata cannot change the semantic ID.

Collection metadata, target aliases, the raw response hash, and provider-only fields are excluded from the content digest. A byte-format change in an otherwise equivalent provider response therefore changes `raw_response_sha256` but not `content_id`.

## Verification

All tests are network-free and use `httpx.MockTransport`:

```bash
.venv/bin/pytest -q tests/test_scrapecreators_spike.py
.venv/bin/ruff check \
  src/research/scrapecreators_spike.py \
  scripts/scrapecreators_social_spike.py \
  tests/test_scrapecreators_spike.py
```

The suite verifies endpoint/parameter coverage, exact-target rejection rules,
header injection, response hashing, normalization, raw opt-in/redaction,
bounded retry behavior, safe errors, dry-run without an environment key,
metric-free contract identity, interaction role/linkage classification,
unsupported X interaction status, partial failures, and atomic mode-`0600`
output.

## Live promotion gates

Before considering a production collector, run reviewed public targets across all three platforms and record, without checking raw payloads into git:

1. HTTP success/failure and charged credits per endpoint;
2. normalized profile/post yield and required-field completeness;
3. pagination semantics and duplicate stability across repeated runs;
4. handling of missing, renamed, restricted, and rate-limited targets;
5. provider schema drift against fixtures;
6. terms, privacy, retention, and data-minimization review; and
7. explicit design for provenance, scheduling, failure isolation, and Vault admission.

Until those gates pass, the correct decision is to keep this as an isolated acquisition probe.
