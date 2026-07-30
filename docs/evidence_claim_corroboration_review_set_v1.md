# Evidence claim-corroboration review set v1

## Verdict

The five Vercel editorial sources that required claim-level review now have a
completed, reproducible review set containing ten literal claim cases: two per
source.

No corroboration decision was inferred automatically. All ten cases now carry
attributable human decisions: seven `disputed`, two `mixed`, and one
`independently_corroborated`.

The stress harness verifies literal provenance, counts
`10 candidates / 5 sources / 10 claims / 10 reviewed / 0 pending`, and keeps
the promotion gate blocked without treating review completion as operational
authority.

## What is frozen

Each candidate includes:

- its parent source-review case;
- a stable claim-scoped review ID;
- a non-canonical claim statement and subject;
- the exact source URL and publisher;
- a literal evidence span from the real capture;
- the capture file hash;
- the captured article-text hash;
- the evidence-span hash;
- Unicode code-point start/end offsets;
- explicit review questions.

The frozen provenance rebuilds byte-for-byte from:

```text
fixtures/vercel/vercel_fresh_capture_envelope.json
```

The set deliberately excludes PR Newswire V6. Its source-level decision is
already `excluded`, so it cannot become independent corroboration by attaching
a claim ID. The corroboration manifest records that skipped generation in
`upstream_coverage`, bound to the stable upstream event
`vercel-v6-pr-newswire-review-001`; the exclusion is therefore distinguishable
from a candidate lost by the pipeline.

## Coverage

The ten cases cover:

- InfoQ: Eve release and durable-runtime capabilities;
- VentureBeat: v0 adoption and repository-import capabilities;
- The Register: Eve portability tension and Connect short-lived tokens;
- SiliconANGLE: agent-commit share and AI Gateway token growth;
- TechInformed: d0 skill count and absence of audited impact measures.

This is one real brand, five real sources, and ten unique claim subjects. It is
enough to complete the first human calibration task, but not enough to
demonstrate cross-brand generalization.

## Review decisions

Use one decision per claim case:

- `independently_corroborated`: the piece provides claim-specific independent
  observation, testing, third-party documentation, or an absence check;
- `mixed`: the exact claim combines independent and first-party support;
- `disputed`: the claim relies on Vercel material or its independence cannot be
  resolved safely;
- `excluded`: the case is ineligible for independent corroboration.

Also select one or more provenance bases:

```text
editorial_observation
independent_testing
third_party_documentation
first_party_statement
first_party_material
mixed_provenance
absence_check
unresolved
```

Every decision requires reviewer ID, rationale, timestamp, version, and an
append-only event ID. Decisions can be revoked without editing history.

Each generated decision row carries the frozen `candidate_fingerprint` and
`review_packet_fingerprint`. The latter binds the normalized manifest, the
complete candidate set, and all applicable schema versions. Loading fails
closed if rows mix fingerprints, if either fingerprint differs from the
manifest, or if the manifest and canonical candidate content no longer
produce the declared packet identity. The reviewer signs decisions for that
exact packet, never an isolated JSONL file.

Mutable lifecycle fields and the completed-event fingerprint are excluded
from packet identity to avoid a self-hash cycle. Completed events are frozen
separately with `review_event_fingerprint`.

Before a queue is frozen, an external review file can be evaluated while
`review_event_fingerprint` is null. Both candidate and packet fingerprints
remain mandatory. The completed v1 events are now stored in
`review_events.jsonl`, and their exact event fingerprint is frozen in the
manifest.

## Prepare the review file

```bash
./.venv/bin/python scripts/evidence_claim_corroboration_review_set.py \
  --verify-provenance \
  --write-review-template tmp/claim_corroboration_reviews.jsonl
```

Evaluate a completed file:

```bash
./.venv/bin/python scripts/evidence_claim_corroboration_review_set.py \
  --reviews tmp/claim_corroboration_reviews.jsonl \
  --format markdown \
  --require-review-ready
```

## Promotion boundary

Completing these ten reviews can close the current Vercel claim-scoping task,
but cannot by itself authorize production corroboration.

Promotion remains blocked by:

- only one reviewed real brand;
- unmeasured semantic-paraphrase recall;
- a reproducible two-axis v2 shadow contract that is not yet authorized for
  operational adoption.

Every candidate and review preserves:

```text
runtime_effect = false
authority = false
```
