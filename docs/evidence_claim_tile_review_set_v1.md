# Evidence claim-to-tile review set v1

## Verdict

The deterministic ledger proves literal provenance, not semantic tile support.
The v1 review set freezes every real mapping currently found in local history
and exposes the claim, tile condition, and tile evidence contract required for
human review.

It is a read-only calibration artifact with `runtime_effect=false` and
`authority=false`. Reviews cannot alter tiles, points, scores, reports, or
canonical selection.

## Frozen real set

The dataset lives in:

```text
fixtures/evidence_claim_tile_review/v1/
  manifest.json
  candidates.jsonl
```

It contains all eight mappings produced by the current real-history replay:

| Brand | Claim | Tile | Mapping risk |
| --- | --- | --- | --- |
| Robin Capital | mission | `mission.M1` | Does the statement constitute a detectable mission? |
| Vercel | mission | `mission.M1` | Detectable mission |
| Vercel | mission | `mission.M2` | Mission specificity |
| Vercel | mission | `mission.M3` | Problem or category tension |
| Vercel | mission | `mission.M4` | Cross-block coherence |
| Vercel | mission | `core_purpose.PR1` | Mission reused as purpose |
| Vercel | mission | `core_purpose.PR2` | Mission reused as explicit purpose |
| Vercel | mission | `core_purpose.PR5` | Mission reused as category/product-anchored purpose |

Seven candidates therefore reuse one Vercel mission variant. Three cross from
the mission component into core-purpose tiles. This concentration is evidence
of limited coverage, not seven independent semantic proofs.

## Review semantics

Each human review must choose:

- `accepted`: the cited evidence and claim satisfy the specific tile contract;
- `disputed`: the frozen context cannot resolve the mapping safely;
- `rejected`: literal overlap exists, but the claim does not satisfy the tile
  contract.

The question is not whether the quote exists. The deterministic policy already
proved literal overlap. The reviewer must decide whether the evidence meets the
tile's semantic requirement.

Examples:

- a mission statement can support `mission.M1` without proving
  `mission.M3`;
- a single mission quote cannot prove cross-block coherence when purpose,
  vision, or value-proposition context is missing;
- a functional mission must not automatically become core purpose.

## Review workflow

Generate an unsigned template:

```bash
./.venv/bin/python scripts/evidence_claim_tile_review_set.py \
  --write-review-template docs/claim_tile_reviews.jsonl
```

Evaluate completed reviews:

```bash
./.venv/bin/python scripts/evidence_claim_tile_review_set.py \
  --reviews docs/claim_tile_reviews.jsonl \
  --format markdown
```

Unsigned templates and deterministic `proposed_review` values are not human
labels.

Every template row carries the manifest's `candidate_fingerprint`. Evaluation
recomputes that fingerprint from `candidates.jsonl`, rejects mixed or stale
review rows, and still joins semantic context exclusively through `case_id`.
Claims, quotes, mappings, and tile contracts remain single-sourced in the
frozen candidate file.

Completed decisions can now be recorded in the append-only PostgreSQL journal:

```text
GET  /api/v1/brands/{domain}/evidence-claim-tile-reviews
POST /api/v1/brands/{domain}/evidence-claim-tile-reviews
```

The API subject is the candidate's `mapping.mapping_id`. PostgreSQL derives and
freezes the evidence, claim-variant, mapping-series, tile, and polarity
identities; clients submit only the decision and review rationale. See
[`evidence_claim_tile_review_v1.md`](evidence_claim_tile_review_v1.md).

## Current gate result

Current state:

- candidates: 8;
- human reviews: 0;
- brands: 2;
- unique claim variants: 2;
- polarities: `supports` only;
- review gate: blocked;
- promotion policy: not adopted;
- runtime and scoring authority: disabled.

The zero above describes committed human decisions, not missing storage
capability. The journal now exists, but no reviewer decision has been inferred
or prefilled.

Even eight accepted reviews would not establish generalization. Promotion also
requires:

- at least 5 reviewed real brands;
- at least 10 reviewed claim variants;
- reviewed `supports`, `weakens`, and `insufficient_evidence` cases;
- confirmed mapping precision `1.0`;
- zero critical non-accepts;
- an explicitly adopted promotion policy.

Those thresholds prevent seven mappings derived from one claim from
masquerading as broad validation.
