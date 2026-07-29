# Evidence claim reconciliation v1

## Verdict

The reconciliation journal makes human relation decisions durable and
reversible. It does not make any claim canonical and has no runtime or scoring
authority.

## Subject

Each event addresses one stable `relation_candidate_id` emitted by Claim
Memory v1. The server resolves whether that subject is a
`coexistence_candidate` or `replacement_candidate` from immutable report
history; clients cannot supply or override the relation type.

Claim Memory preserves historical transition subjects by comparing every
ordered report pair. If the observed sequence becomes A → B → C, the A → B
subject remains available alongside B → C and the longer-range A → C
hypothesis, so an earlier journal decision still has a deterministic replay
target.

## Event states

The append-only PostgreSQL journal supports:

- `accepted`: the reviewer accepts the proposed relation;
- `disputed`: the relation has unresolved conflicting interpretation;
- `rejected`: the proposed relation is not semantically valid;
- `revoked`: the current decision is explicitly withdrawn;
- `superseded`: read-time state of an older event replaced by a later event.

No event is updated or deleted by the application. Each event records sequence,
predecessor, schema/policy/evaluator versions, reviewer, API actor, reason,
rationale, idempotency fingerprint, and timestamp.

## Concurrency and identity

Writes require:

- `B3S_EVIDENCE_ADJUDICATION_TOKEN`;
- server-configured `B3S_EVIDENCE_REVIEWER_ID`;
- an `Idempotency-Key`;
- explicit `expected_current_event_id`, including `null` when no event is
  expected.

The server rejects reused keys with different payloads and stale preconditions.
Per-subject advisory transaction locks serialize competing decisions. Reviewer
and relation type are server-derived; request payloads cannot impersonate
another reviewer.

## API

```text
GET  /api/v1/brands/{domain}/evidence-claim-reconciliations
POST /api/v1/brands/{domain}/evidence-claim-reconciliations
```

Reads expose both the paginated full journal and the current event per subject.
Writes fail closed with `503` if PostgreSQL is not configured or unavailable.
There is no mutable file fallback.

## Authority boundary

Database constraints and API responses enforce:

```text
runtime_effect=false
authority=false
```

An accepted replacement records that the reviewer believes one variant
replaces another. It does not:

- set a `canonical_claim_variant_id`;
- remove the older variant;
- infer contradiction;
- change evidence eligibility;
- update a tile, report, canonical scan selection, or score.

Promotion requires a separate reviewed policy and a semantic-slot corpus that
measures false replacement and missed real changes. The frozen relation review
set now makes those metrics executable, but its 13 cases remain unreviewed and
its three real-history cases contain no observed replacement. See
[`evidence_claim_relation_gold_set_v1.md`](evidence_claim_relation_gold_set_v1.md).

## Validation limitation

Service, API, migration-contract, idempotency, concurrency, revocation, and
overlay behavior have automated tests. A live PostgreSQL integration test is
also present but requires a disposable `B3S_TEST_DATABASE_URL` plus
`B3S_ALLOW_SCHEMA_DROP=1`; it is skipped when that environment is unavailable.
