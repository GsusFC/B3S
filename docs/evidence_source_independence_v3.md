# Evidence source independence v3

## Verdict

Source independence v3 removes the unsafe assumption that two external URLs
are independent merely because no exact relationship was found. It remains a
read-only shadow projection and grants no scoring or corroboration authority.

The identity-v2 schema now uses
`evidence-memory-identity-policy-v4` and embeds the versioned
`evidence-source-independence-v3` contract.

## Status contract

Every current external passage receives exactly one status:

- `confirmed_independent`: brand identity is reproducibly eligible, publisher
  ownership is in the reviewed registry, the exact source URL has a reviewed
  independence decision, the publisher is editorial rather than a wire
  service, and no same-cluster or disputed relation was found;
- `same_cluster`: the passage shares a publisher, reviewed publisher group,
  exact content, explicit original/canonical lineage, or high deterministic
  shingle similarity with another current passage;
- `disputed`: lexical similarity falls inside the ambiguous threshold and
  requires review;
- `unknown`: ownership, source-level review, identity, or distribution lineage
  is incomplete.

Only `confirmed_independent` contributes to
`current_independent_external_cluster_count`. The separate
`current_external_cluster_count` reports structural clusters without implying
corroboration.

This naming is prospective only: both the parent projection and its embedded
source-independence contract have `runtime_effect=false` and
`authority=false`.

## Deterministic relations

The versioned registry is
`src/services/evidence_source_registry_v1.json`. It freezes:

- publisher ownership groups;
- publisher kind: `editorial` or `wire_service`;
- reviewed source URLs;
- three-word shingle settings;
- the `0.65` same-cluster threshold;
- the `0.35` disputed threshold.

Content similarity is evaluated only when both passages contain at least six
distinct three-word shingles. Exact copies are joined before similarity is
evaluated. Similarity at or above `0.65` is collapsed conservatively; similarity
from `0.35` to below `0.65` is disputed. Lower similarity is not evidence of
independence.

The projection also consumes explicit immutable evidence metadata when present:

- `canonical_url` or `canonical_source_url`;
- `original_source_url`, `syndication_source_url`, or `origin_url`;
- `distribution_type`, `source_type`, or `syndication_type`, restricted to
  recognized editorial, press-release, and wire labels.

These fields can reduce apparent independence. They cannot, by themselves,
confirm it.

## Review boundary

The committed registry currently contains only `.test` publisher groups and
source decisions used by controlled tests. It contains zero production-reviewed
source URLs. Therefore real sources remain `unknown`, `disputed`, or
`same_cluster`, and the current local replay produces zero confirmed independent
clusters.

This is deliberate. Publisher ownership alone does not prove original
reporting, and a deterministic lexical matcher cannot detect every semantic
paraphrase. A future production source review must be attributable and
reversible before production URLs are added or persisted. Until then, missing
knowledge fails closed.

The existing PostgreSQL adjudication journal reviews evidence-to-brand identity
only. An `accepted` identity decision must not be reused as a source-independence
decision.

## API fields

The authenticated identity-v2 shadow endpoint returns:

```text
GET /api/v1/brands/{domain}/evidence-memory-identity-v2-shadow
```

Top-level `source_independence` exposes policy and registry versions,
fingerprint, thresholds, allowed states, and the explicit non-authoritative
boundary.

Current external entries expose:

- `publisher_group_id`, `publisher_group_kind`, and
  `publisher_registry_status`;
- `source_independence_review_status`;
- `canonical_urls`, `original_source_urls`, and `distribution_types`;
- `independence_cluster_id` and member count;
- `independence_status`, reason codes, review requirement, and any deterministic
  similarity candidates.

Raw evidence text and internal shingle hashes are not returned.

## Local replay — 2026-07-28

The read-only harness replayed 12 local histories:

- 185 current external passages;
- 160 structural external source clusters;
- 19 `same_cluster`, 12 `disputed`, and 129 `unknown` clusters;
- 0 confirmed independent external clusters;
- 0 executable invariant failures;
- exact and lightly paraphrased controlled copies collapsed;
- ambiguous similarity was disputed;
- unreviewed ownership did not count as independence.

The result demonstrates fail-closed behavior on the available corpus. It does
not measure semantic-paraphrase recall or justify production scoring.
