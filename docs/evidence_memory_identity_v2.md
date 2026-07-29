# Evidence memory identity v2

## Verdict

Identity schema v2 fixes the known URL-locator error in the first shadow
ledger. Its current identity decision contract is policy v4. It is not a
canonical evidence store and has no scoring authority.

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
1 and cannot be inferred from `accepted`. The stricter semantic layer is
documented in [`evidence_claim_memory_v1.md`](evidence_claim_memory_v1.md);
unlike this lower-level projection, it never treats a visual tile,
`checked_block`, or an undeclared `claim_id` as a semantic claim slot.

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

## Identity policy v4

Policy v4 keeps the final internal states `eligible`, `mismatch`, `disputed`,
and `unverified`, but evaluates two independent axes before deciding:

- identity strength: `positive`, `negative`, `unverified`, or `conflicting`;
- entity relation: `same_entity`, `parent`, `subsidiary`, `product`,
  `related_party`, `unrelated`, or `unknown`.

The projection maps these states to the reviewed gold semantics without
changing the gold data:

```text
eligible   → accepted
mismatch   → rejected
disputed   → disputed
unverified → disputed
```

`rejected` now requires reproducible evidence of non-identity. Missing
provenance, a name-only match, or an unresolved corporate relationship is
`unverified`, not a fallback mismatch.

Policy v4 consumes two versioned provenance blocks:

- `entity_conflict_provenance`: conflict type, deterministic method,
  reproducible evidence spans or alternative entity locator, and version;
- `entity_relation_provenance`: verified/candidate status, relation,
  passage-subject role, source and subject domains, reproducible method, and
  version.

Raw evidence spans are never returned by the projection; only counts and
digests are exposed for audit. LLM-only conflicts remain non-authoritative even
when upstream metadata labels them reproducible. Locator-only registry claims
also fail closed until a deterministic external registry verifier exists.

All positive and negative signals are evaluated before a decision:

- reproducible positive plus reproducible negative: `disputed`;
- reproducible negative only: `mismatch`;
- reproducible positive only: `eligible`;
- otherwise: `unverified`.

For `owned_copy` outside the scanned domain:

- verified `unrelated`, an explicit other entity, or a reproducible ownership
  mismatch: `mismatch`;
- candidate/unknown relation or unresolved subject role: `unverified`;
- verified related entity with the scanned entity as the exact passage
  subject: `eligible`;
- verified related entity with another entity as subject: `mismatch`.

External evidence remains conservative:

- reproducible strong external attribution: eligible as a candidate;
- bare upstream domain or brand-name match: unverified;
- LLM-only match or mismatch: unverified;
- deterministic mismatch: mismatch;
- reproduced positive attribution contradicted by another signal: disputed.

New Exa captures persist the positive attribution contract. The evidence worker
carries it into immutable report evidence, and identity v2 re-runs the check
rather than trusting the summary label. Historic evidence and providers without
the v4 contracts remain unverified unless the passage contains one of the
deliberately narrow deterministic conflict rules. This closes the known
homonym, explicit-other-entity, domain-transfer, and parent-domain fallback
errors without replacing human adjudication.

## Source independence

Identity schema v2 now runs source-independence policy v3. Every current
external passage is classified as `confirmed_independent`, `same_cluster`,
`disputed`, or `unknown`.

External passages are joined into one conservative cluster when:

- they share a publisher domain; or
- they belong to the same publisher ownership group in the versioned registry;
- their normalized content is exactly equal across publishers;
- canonical/original-source metadata connects them; or
- deterministic three-word shingle similarity reaches the frozen same-cluster
  threshold.

Ambiguous similarity is `disputed`. Missing publisher ownership or source-level
review is `unknown`; absence of a detected relationship is never treated as
proof of independence. Wire and press-release lineage cannot be confirmed
without source review.

Cluster IDs describe the current projection membership. They are not accepted,
permanent source identities. Only `confirmed_independent` appears in the legacy
`current_independent_external_cluster_count`; a separate
`current_external_cluster_count` exposes structural clusters. Neither count
affects runtime corroboration or scoring.

The committed registry contains controlled `.test` fixtures but no
production-reviewed source URLs, so real sources fail closed. See
[`evidence_source_independence_v3.md`](evidence_source_independence_v3.md) for
the complete contract and limitations.

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
- 185 current external passages collapsed to 160 structural source clusters;
- those clusters classify as 19 `same_cluster`, 12 `disputed`, and 129
  `unknown`;
- 0 real clusters were marked `confirmed_independent` because the registry has
  no production-reviewed source URLs.

This demonstrates that v1's locator generated false change pressure. It does
not demonstrate real-world semantic-change recall because the historical corpus
contains no stable semantic claim IDs.

## Promotion blockers

Identity v2 must remain non-authoritative until at least:

1. stable semantic claim IDs are emitted by acquisition;
2. production source-independence reviews are attributable and reversible;
3. evidence is mapped persistently through claim to tile;
4. a reviewed dataset measures both false-change rate and real-change recall;
5. reviewed real cases measure both false identity acceptance and identity
   recall beyond the controlled 14-case set.
