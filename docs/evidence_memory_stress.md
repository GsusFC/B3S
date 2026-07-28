# Evidence memory adversarial validation

## Verdict

The evidence-memory direction is a falsifiable architecture hypothesis, not a
validated scoring system yet.

The current ledger supports a useful foundation: exact evidence identities
survive acquisition loss and evaluator drift. Identity v2 also separates
documents from passages, suppresses URL-only change candidates, and clusters
exact copies and light lexical paraphrases under source-independence policy v3.
New Exa captures also persist enough external-attribution provenance for v2 to
reproduce a strong match instead of trusting its label. It does not yet support
safe promotion because the identity set and production source registry have not
been reviewed. Claim Memory v1 can now separate semantic slots, variants, and
occurrences and propose relations, but it cannot adjudicate those relations,
persist tile support, or evaluate a canonical memory version.

## Read-only harness

Run every controlled probe and replay every locally available brand history:

```bash
./.venv/bin/python scripts/evidence_memory_stress.py \
  --reports-dir data/reports \
  --format markdown
```

Write a JSON artifact:

```bash
./.venv/bin/python scripts/evidence_memory_stress.py \
  --reports-dir data/reports \
  --format json \
  --output tmp/evidence_memory_stress.json
```

The command never changes reports, ledger rows, canonical selection, or scores.
Its normal exit code fails only when an executable invariant fails. Add
`--require-promotion-ready` to also fail while architectural blockers remain;
this is intentionally expected to return exit code `2` today.

## Status meanings

- `pass`: the current implementation satisfied an executable invariant.
- `fail`: the implementation contradicted an invariant; further promotion must
  stop.
- `blocked`: the current data model cannot answer the question safely. This is
  not a failed test and must not be presented as a pass.

## Controlled probes

The executable foundation must prove:

1. report order cannot change the derived ledger;
2. complete acquisition dropout cannot erase remembered identities;
3. evaluator-only changes cannot rewrite evidence identities;
4. exact repetition cannot increase evidence breadth;
5. changed content is surfaced but not silently interpreted as contradiction;
6. TTL expiry cannot retire evidence automatically;
7. several passages from one document are not temporal revisions;
8. weak external identity does not become validation-eligible;
9. a bare strong-looking upstream label does not become validation-eligible;
10. persisted strong external attribution is independently reproducible;
11. exact syndicated copies share a cluster and do not count as independent;
12. a controlled stable-slot change is surfaced as a proposed revision;
13. even a deliberately false manual identity acceptance cannot affect
    eligibility, scoring, or canonical selection;
14. lightly paraphrased copies share a cluster and do not count as independent;
15. unknown publisher ownership cannot count as independence.
16. a bare content-derived claim ID cannot become a semantic slot;
17. claim slot, content variant, and report occurrence identities remain
    separate;
18. simultaneous variants become coexistence candidates, not replacements;
19. report order cannot change Claim Memory v1.

The adversarial probes deliberately expose:

1. repeated false identity metadata can create a v1 `validation_candidate`,
   while v2 still lacks a reviewed identity gold set;
2. source-independence v3 has no production-reviewed source decisions or
   measured semantic-paraphrase recall;
3. proposed claim relations have no durable canonical-resolution path;
4. evidence has no persistent, versioned claim-to-tile mapping;
5. no evaluator consumes a canonical memory version.

## Real-history replay

For every history with material evidence, the harness appends in-memory-only
variants:

- an empty provider capture;
- an evaluator-only score and interpretation change;
- an exact repeated capture;
- the same reports in reverse input order.

It compares unique identity and locator sets, not the mutable shadow state.
Observation counts and states are allowed to change; evidence breadth is not.

Each brand diagnostic also reports:

- historical score range;
- unique evidence identities and locators;
- locators carrying multiple content variants;
- evidence states and source-class counts.

High multi-variant rates are a warning that the v1 locator
`source_class | evidence_type | URL` is too coarse for change authority. They
do not prove that the brand changed.

## Baseline result — local reports, 2026-07-28

The first run replayed 12 histories containing material evidence:

- 12/12 were invariant to report input order;
- 12/12 retained identity memory after complete acquisition dropout;
- 12/12 retained identity memory after evaluator-only drift;
- 12/12 kept evidence breadth unchanged after an exact repeat;
- 0 executable invariant failures;
- 5 promotion blockers.

The replay also found multi-variant locator pressure in 11/12 histories. In the
largest cases, 52%–56% of evidence identities belonged to locators containing
multiple contents. Some occur in single-scan histories, proving that a
multi-variant locator can mean several chunks or claims from one page rather
than temporal brand change. The v1 locator therefore cannot drive change
authority.

The v1/v2 comparison found:

- 29 v1 `changed_candidate` entries;
- 29/29 suppressed by v2 because no matching stable-slot revision existed;
- 0 v2 real-history revision candidates;
- 49 stable slots, all produced by `visual_tile` rather than semantic
  `claim_id`;
- a false bare strong label stayed unverified while a reproduced Exa
  attribution reached `validation_candidate` in the controlled probes;
- 185 current external passages grouped into 160 structural source clusters;
- 19 `same_cluster`, 12 `disputed`, and 129 `unknown` clusters;
- 0 real clusters marked `confirmed_independent`, because the registry has no
  production-reviewed source URLs.

Claim Memory v1 replayed the same 12 histories and found:

- 0 semantic claim slots, variants, occurrences, or relation candidates;
- 184 structural metadata rows correctly ignored as semantic identity:
  168 visual-tile rows and 16 `checked_block` rows;
- 19/19 controlled executable invariants passed, including the four Claim
  Memory invariants.

Verdict: `foundation_supported_promotion_blocked`.

This result validates only the identity-memory foundation and the removal of a
specific v1 false-change mechanism. The controlled explicit-claim probe proves
that the relation-proposal mechanism exists, but the real corpus cannot measure
semantic-change recall because it has no stable semantic claim slots. It does
not validate claim reconciliation, memory scoring, or production real-change
detection.

## Promotion gates

### Gate 0 — identity-memory foundation

Required:

- 100% pass rate for all executable invariants;
- deterministic rebuild from immutable reports;
- no runtime scoring or selection effect.

Current status: passed on the local baseline.

### Gate 1 — identity and claim safety

Required before any claim can become canonical:

- independent entity adjudication rather than trusting one LLM label;
- persisted matched entity/domain data so a strong deterministic identity
  decision can be reproduced instead of trusting its upstream label — now
  implemented for new Exa captures and required before enabling any other
  provider;
- explicit `proposed`, `accepted`, `disputed`, `superseded`, `rejected`, and
  `revoked` states;
- reversible decisions with policy and evaluator versions;
- deterministic syndication/source-independence clusters;
- a manually reviewed gold set containing ambiguous names, parent/product
  confusion, wrong domains, copied press releases, and first-scan poisoning.

Automatic authority must remain disabled if a deliberately false repeated item
can become accepted.

Current status: the PostgreSQL identity-adjudication journal now provides
versioned `accepted`, `disputed`, `rejected`, `superseded`, and `revoked`
decisions with idempotency and optimistic concurrency. A controlled false
acceptance remains `runtime_effect=false`, `authority=false`, `unverified`, and
`repeated`. The single reviewer is now bound server-side to a dedicated
credential. Source-independence policy v3 now fails closed on unknown ownership,
clusters exact and light lexical copies, respects explicit lineage, and disputes
ambiguous similarity. Gate 1 is still blocked by the missing human identity
reviews, the absence of production source reviews, and unmeasured semantic
paraphrase recall.

### Gate 2 — tile evidence ledger

Required:

- persistent `evidence → claim → tile` support;
- polarity, source independence, first/last seen, and mapping version;
- exact repeats increase persistence only, never breadth or points;
- unchanged mappings are reused rather than regenerated by an LLM;
- a new evaluator version creates a new mapping series instead of rewriting
  history.

### Gate 3 — versioned memory evaluator

Evaluation identity:

```text
hash(memory_version + rubric_version + evaluator_version)
```

Required:

- the same identity always reuses the same result;
- no accepted memory change means no score change;
- every score delta is explained by accepted evidence/claim/tile deltas or a
  declared version change;
- repeating the same evidence cannot improve the score;
- acquisition confidence may fall without changing the memory score.

### Gate 4 — dual-run shadow validation

Compare current capture scoring with memory scoring on:

- real repeated histories;
- controlled provider dropouts;
- deliberately altered owned claims;
- temporary and permanent source disappearance;
- contradictory official pages;
- rebrands, mergers, domain transfers, and entity contamination.

The system is not ready for authority if it achieves stability by missing real
changes. Promotion requires both:

- near-zero false score changes when canonical memory is unchanged; and
- reviewed detection of controlled real changes within the declared
  confirmation policy.

## Architecture falsifiers

Stop or redesign the approach if any of these persist:

- report order changes canonical memory;
- an acquisition miss removes accepted support;
- scan count monotonically improves score;
- repeated or syndicated evidence increases independent breadth;
- a poisoned first observation cannot be corrected;
- a real official change remains invisible after sufficient reliable captures;
- evaluator upgrades silently rewrite historical decisions;
- any score delta lacks a complete evidence-to-tile explanation.
