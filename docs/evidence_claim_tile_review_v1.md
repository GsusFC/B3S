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
- exact `review_packet_fingerprint`;
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
  "evaluator_version": "manual-review-v1",
  "review_packet_fingerprint": "<sha256>"
}
```

`expected_current_event_id=null` means that the caller expects no current
decision. Later changes must send the current event id. Valid decisions are
`accepted`, `disputed`, `rejected`, and `revoked`; only a current non-revoked
decision can be revoked. Reusing an idempotency key with a different request or
writing against a stale current event returns `409`.

`review_packet_fingerprint` is a required lowercase SHA-256 field on every new
event. It binds the decision to the exact normalized manifest, candidates, and
schema presented to the reviewer. It is deliberately separate from
`evaluator_version`; the latter identifies the reviewing procedure, not the
reviewed data. Migration `009` leaves the field nullable only for legacy v1
events, which remain readable but fail closed when rebuilding reviewed memory.

The server derives the reviewer from the authenticated principal and derives
the human-readable `case_id` plus all mapping metadata from PostgreSQL. A
client cannot invent a mapping snapshot. Writes fail with `503` if durable
storage is unavailable; there is no JSONL, SQLite, or in-memory fallback.

## Relationship to the frozen review set

[`evidence_claim_tile_review_set_v1.md`](evidence_claim_tile_review_set_v1.md)
still freezes the first eight real mappings for calibration. It is not the
operational journal. Those eight reviews are now complete: seven mappings were
accepted and `mission.M2` was rejected. The API provides the durable,
revocable place to store those decisions without granting tile or scoring
authority.

The rejection exposes precision `0.875`, so the narrow mapping gate correctly
remains blocked. Generalization is also unproved: only two brands, two claim
variants, and `supports` polarity are represented. Promotion remains
separately blocked on mapping correction, broader coverage, and policy.

`build_reviewed_claim_tile_memory_shadow()` consumes the persistent ledger and
its current review events. It selects the seven accepted mappings, excludes
the rejected mapping, and derives a versioned shadow evaluation identity
without calculating or changing the operational score.

The ledger read endpoint exposes this as nested `reviewed_memory`. On the local
32-report corpus it reconstructs:

- Robin Capital: 1 candidate, 1 accepted;
- Vercel: 7 candidates, 6 accepted, 1 rejected.

Both selections have `runtime_effect=false`, `authority=false`, and
`automatic_scoring_effect=false`. A current `revoked` event removes its
mapping from the accepted selection and returns that subject to pending; it
does not count as a completed active review.

## Persistence proof

The PostgreSQL integration test:

1. applies migrations through `009`;
2. imports a valid immutable report fixture that produces one claim-to-tile
   mapping;
3. appends `accepted`;
4. recreates the repository and reconstructs the accepted reviewed memory;
5. appends `revoked`;
6. recreates the repository again and verifies that the reviewed memory has no
   accepted mapping and reports the subject as pending;
7. reads `revoked` plus the superseded acceptance from the journal;
8. verifies that the ledger fingerprint did not change.

PR #29 ran the same test against PostgreSQL 16 and completed with
`2298 passed, 1 skipped`. A local isolated PostgreSQL 14 run on 2026-07-29 also
passed. Together they prove migration, restart durability, revocation, and
separation from the ledger on both database majors; they do not prove a
production Fly release.
