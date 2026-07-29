# Evidence → claim → tile ledger v1

## Verdict

Gate 2 now has a persistent, versioned shadow foundation. It can reuse
relationships already present in immutable scanner reports without asking an
LLM to regenerate them. It is not a canonical claim system and cannot affect
tiles, points, scores, reports, or canonical selection.

## Mapping rule

A tile maps only when all of these conditions hold:

1. the report contains a semantic claim occurrence with a stable claim slot;
2. the claim resolves to its source evidence record;
3. the tile quote occurs literally in that source, with ordered ellipsis
   fragments allowed;
4. the quote contains the claim text, or the claim text contains the quote;
5. an explicit `evidence_ref`, when present, resolves to that same source;
6. exactly one `(source evidence, claim variant)` candidate survives.

Same page, thematic similarity, shared keywords, and LLM inference are not
enough. Missing, non-literal, and ambiguous relations remain unmapped.

## Identities and versions

The projection keeps separate identities for:

- source evidence;
- claim evidence;
- claim slot;
- claim variant;
- tile key;
- mapping;
- mapping series.

A mapping series pins the mapping and identity policies, source-registry
fingerprint, pipeline, rubric, prompt, evaluator model, and projection
versions. Changing the evaluator or a pinned policy creates another series;
it does not rewrite the old one.

Within one series:

- first observation: `observed`;
- exact reacquisition: `repeated`;
- absent from the latest report in that series: `not_reacquired`.

Repetition increases `observation_count` only. It never increases source
breadth or points.

## Persistence

Migration `006_evidence_claim_tile_ledger.sql` adds:

- `evidence_claim_tile_ledger_states`;
- `evidence_claim_tile_mapping_series`;
- `evidence_claim_tile_mappings`;
- `evidence_claim_tile_mapping_observations`.

Imports rebuild the current projection inside a PostgreSQL savepoint. A shadow
failure is logged and rolled back without failing the immutable report import.
Existing mapping series are upserted but never deleted during a rebuild, so
older evaluator and policy series remain available as history.

Enable automatic persistence:

```bash
B3S_EVIDENCE_CLAIM_TILE_LEDGER_MODE=shadow
```

Backfill existing PostgreSQL histories:

```bash
B3S_EVIDENCE_CLAIM_TILE_LEDGER_MODE=shadow \
  ./.venv/bin/python scripts/import_b3s_reports_postgres.py \
  --migrate-only \
  --rebuild-evidence-claim-tile-ledger-shadow
```

## Read API

```text
GET /api/v1/brands/{domain}/evidence-claim-tile-ledger-shadow
```

The response exposes mapping IDs, versions, polarity, source independence,
first/last observation, persistence state, and quote hashes. It returns no raw
claim or quote text.

`persistence.stored=true` means the PostgreSQL payload fingerprint matches the
current immutable history. Otherwise the API returns a deterministic
`history_derived` projection.

## Safety boundary

These values are enforced throughout the projection and database schema:

```text
runtime_effect = false
authority = false
automatic_scoring_effect = false
```

The real-history replay on 2026-07-29 found 8 conservative mappings across 12
brand histories: 7 in Vercel and 1 in Robin Capital. Liminal produced none.
The separate set documented in
`docs/evidence_claim_tile_review_set_v1.md` freezes all eight with their claim
text and tile contracts. None has a human review yet. Seven reuse one Vercel
mission claim, only two brands and two claim variants are represented, and all
eight have `supports` polarity.

This demonstrates reuse of existing scanner evidence; it does not validate
coverage or correctness for canonical promotion. Gate 2 therefore remains
blocked on completed reviews, broader real coverage, polarity coverage, and an
explicit promotion policy.
