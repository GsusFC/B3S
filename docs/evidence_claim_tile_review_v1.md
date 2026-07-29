# Evidence claim-to-tile review journal v1

## Verdict

The literal `evidence → claim → tile` mapping and the human semantic decision
are now durable but remain separate. PostgreSQL stores every review event
without editing the mapping, source report, claim, tile, score, or canonical
selection.

The journal is append-only and enforces:

```text
runtime_effect=false
authority=false
automatic_tile_effect=false
automatic_scoring_effect=false
```

An `accepted` event therefore means only that a human considers the cited
evidence and claim sufficient for that exact tile contract. It does not turn
the mapping into runtime authority.

## Durable subject

The review subject is the ledger's immutable 64-character `mapping_id`. Each
stored event also freezes:

- `mapping_series_id`;
- `source_evidence_id`;
- `claim_variant_id`;
- `component_key`, `tile_id`, and `tile_key`;
- mapping `polarity`;
- reviewer, actor, rationale, reason, evaluator version, and timestamp.

This makes the decision attributable and reproducible without returning raw
claim or quote text.

## API

```text
GET  /api/v1/brands/{domain}/evidence-claim-tile-reviews
POST /api/v1/brands/{domain}/evidence-claim-tile-reviews
```

Writes require the dedicated evidence-reviewer credential and an
`Idempotency-Key`. Example:

```json
{
  "subject_id": "<mapping-id>",
  "decision": "accepted",
  "expected_current_event_id": null,
  "reason_code": "tile_contract_satisfied",
  "rationale": "The cited claim satisfies this exact tile contract.",
  "evaluator_version": "manual-review-v1"
}
```

`expected_current_event_id=null` means that the caller expects no current
decision. Later changes must send the current event id. Valid decisions are
`accepted`, `disputed`, `rejected`, and `revoked`; only a current non-revoked
decision can be revoked. Reusing an idempotency key with a different request or
writing against a stale current event returns `409`.

The server derives the reviewer from the authenticated principal and derives
the human-readable `case_id` plus all mapping metadata from PostgreSQL. A
client cannot invent a mapping snapshot. Writes fail with `503` if durable
storage is unavailable; there is no JSONL, SQLite, or in-memory fallback.

## Relationship to the frozen review set

[`evidence_claim_tile_review_set_v1.md`](evidence_claim_tile_review_set_v1.md)
still freezes the first eight real mappings for calibration. It is not the
operational journal. Those eight reviews remain pending until the reviewer
submits decisions; the API now provides the durable and revocable place to
store them.

Completing those eight reviews can close the narrow review gate, but it cannot
establish generalization: only two brands, two claim variants, and `supports`
polarity are represented. Promotion remains separately blocked on broader
brand, claim-variant, polarity, and policy coverage.

## Persistence proof

The PostgreSQL integration test:

1. applies migrations through `008`;
2. imports a valid immutable report fixture that produces one claim-to-tile
   mapping;
3. appends `accepted`;
4. recreates the repository and reads the same current event;
5. appends `revoked`;
6. recreates the repository again and reads `revoked` plus the superseded
   acceptance;
7. verifies that the ledger fingerprint did not change.

CI is configured to run the same test against PostgreSQL 16. A local isolated
PostgreSQL 14 run on 2026-07-29 passed. This proves migration, restart
durability, revocation, and separation from the ledger on that local database;
it does not yet prove the pending CI run or a production Fly release.
