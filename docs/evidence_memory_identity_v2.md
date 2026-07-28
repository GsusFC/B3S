# Evidence memory identity v2

## Verdict

Identity v2 fixes the known URL-locator error in the first shadow ledger, but
it is not a canonical evidence store and has no scoring authority.

The projection separates:

```text
document → passage → optional stable claim slot → identity adjudication
```

Several passages from one page are therefore several passages, not several
versions of one claim. An older passage becomes a `revision_candidate` only
when both versions share a stable slot such as an explicit `claim_id`, visual
tile, checked block, or acquisition-absence type. URL equality alone never
means brand change.

## Runtime contract

`build_evidence_memory_identity_v2()` is deterministic and read-only:

- `runtime_effect=false`;
- `authority=false`;
- an entry remains `proposed` until a durable identity decision is overlaid;
- `accepted` means only that a reviewer accepted the evidence-to-brand
  association, not that the passage is true or scoring-eligible;
- it never changes report selection, report content, tiles, or scores;
- immutable reports remain the source of record;
- the returned projection contains hashes and provenance, not raw evidence
  content.

The current implementation is exercised by the adversarial replay harness and
is exposed as an authenticated API projection:

```text
GET /api/v1/brands/{domain}/evidence-memory-identity-v2-shadow
GET /api/v1/brands/{domain}/evidence-memory-adjudications
POST /api/v1/brands/{domain}/evidence-memory-adjudications
```

Identity v2 is recomputed from immutable history. Adjudication events are held
in a separate append-only PostgreSQL journal and overlaid on the projection.
New scans therefore become visible without creating a second evidence source
of record, while human decisions remain durable and reviewable.

## Identity layers

### Document

A document is identified by normalized URL and source class. Evidence without a
URL falls back to a source-surface identity.

The document answers: where did the evidence come from?

### Passage

A passage is content-addressed inside a document. Exact reacquisition of the
same passage increments persistence; a new passage does not delete or replace
an older one.

The passage answers: what exact material was observed?

### Stable slot

A stable slot is optional. It currently exists only when the acquisition
metadata provides one of:

- `claim_id`;
- visual `tile`;
- `checked_block`;
- an acquisition-absence evidence type.

These methods do not have equal semantic strength. A visual tile is a stable
structural slot, not a semantic brand claim.

The slot answers: which explicitly stable position could these two passages be
variants of?

### Adjudication

Identity adjudication v1 supports:

- `proposed`: no current journal decision exists;
- `accepted`: a reviewer accepts that the evidence refers to the scanned
  brand;
- `disputed`: the association has unresolved conflicting signals;
- `rejected`: a reviewer rejects the association;
- `superseded`: an older journal event was replaced by a later decision;
- `revoked`: the current reviewer decision was explicitly withdrawn.

Each event records the policy version, evaluator version, reviewer, API actor,
reason code, rationale, predecessor, and timestamp. Writes require both an
`Idempotency-Key` and the exact `expected_current_event_id`; `null` explicitly
means that the caller expects no current decision. This prevents silent lost
updates and makes retries deterministic.

The journal is PostgreSQL-only. A write fails closed with `503` when durable
storage is unavailable; it never falls back to mutable JSON files. Events can
be superseded or revoked but are not updated or deleted by the application.

This only resolves evidence-to-brand identity review. Claim truth, semantic
replacement, canonical claim selection, and tile support remain outside Gate
1 and cannot be inferred from `accepted`.

## State semantics

- `observed`: first exact passage observation.
- `repeated`: exact passage was seen again but identity or acquisition
  eligibility is incomplete.
- `validation_candidate`: exact passage was seen in at least two eligible
  captures. This is still not a validated fact.
- `revision_candidate`: an older passage is absent from the latest capture and
  a different passage occupies the same stable slot.
- `not_reacquired`: the passage was not seen in the latest capture; it remains
  remembered.
- `stale_candidate`: the passage was not reacquired and its shadow TTL elapsed;
  automatic retirement remains disabled.

No state implies semantic contradiction or automatic claim replacement.

## External identity policy

Owned evidence is eligible only when its URL belongs to the scanned brand
domain.

External evidence is treated conservatively:

- a persisted strong match whose subject domain, source domain, alias, method,
  score, collector relationship, and review state can be reproduced against
  the evidence: eligible as a candidate;
- a bare upstream `identity_match=domain` label: unverified;
- brand-name-only match: unverified;
- LLM-only domain or name match: unverified;
- deterministic mismatch: mismatch;
- a reproduced strong match contradicted by a deterministic or LLM mismatch:
  disputed.

New Exa captures persist this reproducible attribution contract. The evidence
worker carries it into immutable report evidence, and identity v2 re-runs the
check rather than trusting the summary label. Historic evidence and providers
without this contract remain unverified; they are remembered but cannot become
`validation_candidate`. This closes the bare-strong-label poisoning path in v2,
but it does not resolve homonyms or replace human adjudication.

## Source independence

External passages are joined into one conservative independence cluster when:

- they share a publisher domain; or
- their normalized content is exactly equal across publishers.

This prevents exact syndicated copies from appearing as independent
corroboration. Paraphrased syndication, publisher ownership groups, wire-service
lineage, and copied claims with small edits remain unresolved.

Cluster IDs describe the current projection membership. They are not accepted,
permanent source identities.

## Local replay result — 2026-07-28

The replay covered 12 local brand histories:

- v1 produced 29 `changed_candidate` entries;
- v2 suppressed all 29 because none had a matching stable-slot revision;
- v2 produced 0 real-history `revision_candidate` entries;
- the 49 stable slots in the corpus were all `visual_tile` slots;
- controlled tests prove that an explicit `claim_id` change is surfaced as a
  proposed revision;
- controlled tests prove that a false bare strong label remains unverified and
  that a reproduced Exa attribution can become a validation candidate;
- 185 current external passages collapsed to 162 conservative independence
  clusters.

This demonstrates that v1's locator generated false change pressure. It does
not demonstrate real-world semantic-change recall because the historical corpus
contains no stable semantic claim IDs.

## Promotion blockers

Identity v2 must remain non-authoritative until at least:

1. stable semantic claim IDs are emitted by acquisition;
2. paraphrased syndication and publisher ownership are addressed;
3. evidence is mapped persistently through claim to tile;
4. a reviewed dataset measures both false-change rate and real-change recall;
5. adjudication authentication identifies individual reviewers rather than
   relying on a shared environment-token actor plus a declared reviewer.
