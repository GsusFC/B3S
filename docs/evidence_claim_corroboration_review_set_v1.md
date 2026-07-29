# Evidence claim-corroboration review set v1

## Verdict

The five Vercel editorial sources that previously required claim-level review
now have a concrete, reproducible review queue. The queue contains ten literal
claim cases: two per source.

No corroboration decision has been inferred automatically. All ten cases are
pending human review.

Stress policy v13 consumes this queue as its 28th controlled invariant and its
two-axis shadow projection as the 29th. It verifies the literal provenance,
counts `10 candidates / 5 sources / 10 claims / 0 reviewed / 10 pending`, and
keeps the claim-corroboration promotion gate blocked.

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
a claim ID.

## Coverage

The ten cases cover:

- InfoQ: Eve release and durable-runtime capabilities;
- VentureBeat: v0 adoption and repository-import capabilities;
- The Register: Eve portability tension and Connect short-lived tokens;
- SiliconANGLE: agent-commit share and AI Gateway token growth;
- TechInformed: d0 skill count and absence of audited impact measures.

This is one real brand, five real sources, and ten unique claim subjects. It is
enough to make the pending human task concrete, but not enough to demonstrate
cross-brand generalization.

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

While the queue is pending, an external review file can be evaluated without a
manifest fingerprint. This remains non-authoritative. When the completed
reviews are accepted into a dataset version, their exact fingerprint must be
frozen in the manifest.

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
