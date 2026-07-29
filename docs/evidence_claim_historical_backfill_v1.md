# Evidence Claim Historical Backfill v1

## Verdict

Historical evidence can be reused safely without editing old reports. The
backfill is a deterministic read-time projection over immutable
`sv9-flow-candidate-v1` evidence packs.

## Resolution contract

For every candidate:

1. if `claim_memory_evidence` exists as a list, use it exactly, including an
   empty list;
2. if that persisted field is malformed, fail closed;
3. if the field is missing and the schema is `sv9-flow-candidate-v1`, run
   historical backfill v1;
4. if the field is missing on v2 or an unknown schema, fail closed;
5. never write the derived records back into the report.

Backfilled records use Producer v2 and declare:

```text
claim_slot_derivation_mode=historical_backfill
historical_backfill_version=evidence-claim-historical-backfill-v1
runtime_effect=false
authority=false
```

Claim Memory policy v4 exposes the derivation mode and producer version on
slots, variants, occurrences, and summary counters. Raw claim text is still
excluded from the API response.

## Local replay — 2026-07-29

The read-only replay examined 32 v1 reports across 12 brand histories:

- 3 reports produced one explicit mission record each;
- 3 semantic slots, 3 variants, and 3 occurrences were recovered;
- the recovered domains were `becomeliminal.com`, `robincap.com`, and
  `vercel.com`;
- Robin Capital's web/Exa duplicate collapsed to one record;
- no explicit vision declaration was recovered;
- no coexistence or replacement relation was proposed;
- 2 variants are current and Vercel's older variant is `not_reacquired`;
- all 184 structural `tile`/`checked_block` rows remain ignored;
- 22/22 controlled invariants passed with 0 failures.

This proves reuse, not semantic-change recall. There are no repeated historical
variants for the recovered slots, so the corpus still cannot measure false
replacement or missed real change.

The recovered cases are frozen into the non-authoritative relation review set
described in
[`evidence_claim_relation_gold_set_v1.md`](evidence_claim_relation_gold_set_v1.md).

## Safety boundary

The backfill does not:

- infer slots from EvidenceGraph's text-derived `claim_id`;
- interpret generic mission-like prose;
- turn absence in a later capture into contradiction;
- alter report JSON, evidence eligibility, scoring, tiles, or canonical
  selection;
- accept any relation automatically.

Removing the backfill changes only the shadow projection.
