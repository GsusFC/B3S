# Evidence Claim Memory v1

## Verdict

Claim Memory v1 establishes deterministic semantic claim identity, but it is
not a canonical claim store. It is a read-only shadow projection with no
scoring, selection, replacement, or contradiction authority.

## Identity contract

The projection keeps three identities separate:

```text
claim slot → content variant → report occurrence
```

### Claim slot

`claim_slot_id` identifies the stable semantic subject being tracked. Its hash
contains:

- normalized brand domain;
- explicit entity scope, or the brand scope by default;
- normalized semantic slot key.

A slot exists only when evidence metadata contains:

- `claim_slot_key`; or
- legacy `claim_id` together with `claim_id_semantics=stable_slot`.

A slot key is case-normalized, may use lowercase letters, digits, `.`, `_`,
`:`, and `-`, and is limited to 200 characters. Invalid declarations are
reported in the ignored-reason counters rather than silently coerced.

A bare `claim_id` is not accepted because current EvidenceGraph claim IDs are
derived partly from claim text. Changing the text changes that ID, so it cannot
link variants over time. Visual `tile` and `checked_block` values are also
ignored: they identify presentation or pipeline structure, not necessarily one
semantic proposition.

### Claim variant

`claim_variant_id` identifies normalized content within one slot. Its hash
contains the slot ID and the evidence-memory content hash. Moving unchanged
content to another URL therefore preserves the semantic variant while evidence
identity still records both source locations.

The API returns the content hash, evidence IDs, provenance states, and timing,
but never raw claim text.

### Claim occurrence

`claim_occurrence_id` identifies one variant observed through one evidence
identity in one immutable report. Repeated duplicate references inside that
same report increment `duplicate_ref_count`; they do not create artificial
occurrences or variants.

## State and relation proposals

Variant states are descriptive:

- `observed`: present once in the latest report;
- `repeated`: observed more than once and present in the latest report;
- `not_reacquired`: remembered but absent from the latest report.

Multiple variants in one slot can produce:

- `coexistence_candidate` when both appear in the same report;
- `replacement_candidate` when exactly one is current and another is
  historical.

These start as hypotheses. A durable review may change
`adjudication_state` to `accepted`, `disputed`, `rejected`, or `revoked`, but
every relation remains `runtime_effect=false` and `authority=false`. The
projection does not infer semantic contradiction and does not choose a
canonical variant. Reusing one slot with several `claim_type` values is exposed
as `claim_type_conflict=true` and requires review even when the normalized
content did not change.

Historical transition candidates remain addressable when later or backfilled
reports arrive. The projection compares every ordered report pair, so adding C
after A → B preserves A → B while adding B → C and the longer-range A → C
hypothesis. These remain proposals; broader enumeration prevents a journal
subject from disappearing merely because another immutable report was added.

## Runtime and API boundary

The projection is available at:

```text
GET /api/v1/brands/{domain}/evidence-claim-memory-shadow
```

It is rebuilt deterministically from immutable report history. In v1:

- `persistence.stored=false`;
- PostgreSQL evidence-identity decisions may be reflected as variant
  provenance;
- PostgreSQL claim-reconciliation decisions may be overlaid on relation
  candidates while the Claim Memory projection itself remains derived;
- no raw claim content is returned;
- no score, report, tile, or canonical selection changes;
- disabling or removing the endpoint cannot alter scan results.

## Replay result — 2026-07-29

The read-only stress harness replayed 12 local brand histories:

- 32 immutable `sv9-flow-candidate-v1` reports were eligible for historical
  backfill;
- 3 explicit `mission.primary` slots, 3 variants, and 3 occurrences were
  recovered from Liminal, Robin, and Vercel;
- Robin's duplicate web/Exa capture collapsed to one occurrence;
- 2 variants are current, while the older Vercel variant is
  `not_reacquired`;
- 0 relation candidates;
- 184 rows ignored as non-semantic structural metadata: 168 visual-tile rows
  and 16 `checked_block` rows;
- 22 controlled executable invariants passed with 0 failures, including
  immutable historical backfill.

No historical report was edited. The replay proves that explicit historical
evidence can be reused without becoming runtime or canonical authority. It
still does not measure semantic-change recall: none of the three recovered
slots contains more than one historical content variant.

## Durable reconciliation boundary

The separate append-only journal now records relation decision, reviewer and
actor identity, reason, rationale, policy/evaluator versions, optimistic
concurrency predecessor, supersession, and revocation history. See
[`evidence_claim_reconciliation_v1.md`](evidence_claim_reconciliation_v1.md).

Canonical claim selection remains blocked. The journal is intentionally
non-authoritative until reviewed examples measure both false replacement and
missed real changes. Evidence-to-claim-to-tile support and a versioned memory
evaluator are later, separate gates.

## Producer migration

`sv9-flow-candidate-v2` now persists a separate
`candidate.claim_memory_evidence` lane. Producer v2 emits only explicit owned
`mission.primary` and `vision.primary` declarations. The lane is excluded from
the evidence pack used by interpretation and scoring, and Claim Memory policy
v4 reads it only for the shadow projection.

Unscoped declarations are limited to the homepage and unambiguous corporate
surfaces. Product pages require explicit `entity_scope`; external proof,
values, audiences, offers, generic strategic language, and inferred
mission/vision language fail closed.

Historical `sv9-flow-candidate-v1` reports remain unchanged. When their
persisted candidate has no claim-memory field, policy v4 may deterministically
derive the same narrow explicit-claim records from the immutable evidence pack
at read time. A non-empty persisted v2 lane has precedence only when it
reproduces exactly from that evidence pack under Producer v2; changed, partial,
or foreign-version records fail closed. An empty persisted lane remains a
valid conservative decision. A v2 or unknown-schema report with a missing lane
also fails closed instead of being reinterpreted.

The system still must not retrofit keys from generic claim text, URL position,
tile ID, `checked_block`, or EvidenceGraph's text-derived `claim_id`; doing so
would recreate the identity error this layer is designed to prevent.

See [`evidence_claim_slot_producer_v2.md`](evidence_claim_slot_producer_v2.md)
and
[`evidence_claim_historical_backfill_v1.md`](evidence_claim_historical_backfill_v1.md).
