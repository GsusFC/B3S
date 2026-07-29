# Evidence Memory Snapshot v1

## Verdict

The snapshot is a deterministic Gate 3 precursor, not canonical memory and not
a scoring evaluator.

It gives the system two identities that can be tested now:

1. `candidate_memory_version`, derived from unique semantic evidence, claim,
   relation, and tile-mapping state; and
2. `shadow_evaluation_identity`, derived from that candidate version plus an
   explicit rubric version and evaluator version.

The authoritative fields remain deliberately empty:

```text
canonical_memory_version = null
evaluation_identity = null
score = null
score_delta = null
```

Every result also preserves:

```text
runtime_effect = false
authority = false
automatic_scoring_effect = false
evaluation_ready = false
```

## Why two identities exist

The Gate 3 target is:

```text
evaluation_identity =
  hash(canonical_memory_version + rubric_version + evaluator_version)
```

Canonical evidence, canonical claims, and promoted claim-to-tile mappings do
not exist yet. Using the word `canonical` for the current projection would
hide that missing work.

An explicit accepted-evidence candidate now exists as a separate Gate 1
projection. It proves that active human acceptances survive acquisition loss
and that newly accepted variants accumulate without replacing earlier ones.
It is still not an adopted canonical-evidence policy and therefore does not
populate `canonical_memory_version`; see
[`evidence_accepted_memory_v1.md`](evidence_accepted_memory_v1.md).

Snapshot v1 therefore tests the same hashing boundary under an explicitly
non-authoritative name:

```text
shadow_evaluation_identity =
  hash(candidate_memory_version + rubric_version + evaluator_version)
```

The shadow identifier can prove determinism and version separation. It cannot
authorize a score or close Gate 3.

## Candidate semantic state

`candidate_memory_version` includes:

- brand name and domain;
- declared identity, adjudication, claim, reconciliation, and tile-mapping
  policy versions;
- content-addressed evidence identity and identity disposition;
- stable claim slots and claim variants;
- proposed or reviewed claim-relation state;
- unique semantic `evidence → claim → tile` bindings and polarity.

The public snapshot contains hashes and stable identifiers, not raw evidence,
claim text, or tile quotes.

## Deliberate exclusions

The candidate version excludes:

- report IDs and timestamps;
- first/last-seen values;
- observation and repeat counts;
- `present_in_latest`, `not_reacquired`, and TTL state;
- acquisition confidence;
- evaluator-specific mapping-series IDs;
- legacy report scores and interpretations.

These exclusions are semantic, not cosmetic. An exact repeat may increase
persistence diagnostics, a provider miss may change acquisition state, and a
new evaluator may create a new mapping series. None of those events adds a new
piece of memory.

The enclosing `state_fingerprint` still covers the complete diagnostic result,
including report count. It may change while `candidate_memory_version` remains
stable.

## Executable guarantees

The v1 regression suite proves:

- report input order does not change the snapshot;
- exact repeats do not change candidate memory breadth or version;
- acquisition dropout does not change candidate memory version;
- evaluator-specific mapping-series drift is deduplicated semantically;
- rubric and evaluator versions change the shadow evaluation identity without
  changing candidate memory;
- a real claim-content change changes candidate memory;
- a manual evidence acceptance changes candidate state but does not grant
  canonical or runtime authority;
- disabled mode and missing version inputs fail closed.

The stress harness adds the same stability boundary as its 27th executable
invariant.

## Remaining promotion blocker

`versioned_memory_evaluator_waiting_for_canonical_memory` remains blocked.

Closing it requires:

1. an adopted canonical evidence policy;
2. an adopted canonical-claim selection policy;
3. reviewed and promoted claim-to-tile mappings;
4. a score evaluator consuming only that canonical memory version;
5. immutable result reuse keyed by the canonical evaluation identity; and
6. a delta ledger explaining every score change by canonical semantic deltas
   or an explicit rubric/evaluator version change.

Until then, Snapshot v1 is suitable for shadow diagnostics and regression
testing only.
