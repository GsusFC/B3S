# Evidence identity gold set v1

## Verdict

The repository contains a frozen 14-case set, 14 attributable human reviews,
and a deterministic evaluation harness. Under identity policy v4, the unchanged
reviews pass the frozen controlled gate with exact agreement `1.0`.

This does not make the identity system production-ready. The fixture is small,
controlled, and shadow-only; it proves that v4 corrects the known semantics
without weakening the threshold, not that it generalizes to real brands.

Candidate `proposed_review` fields are drafting aids only. They are never loaded
as human decisions and cannot make the dataset promotion-ready.

## Contents

```text
fixtures/evidence_memory_gold/v1/
  manifest.json
  candidates.jsonl
  reviews.jsonl
```

The candidate fingerprint is pinned in the manifest. Any silent candidate edit
invalidates evaluation until a deliberate dataset version and fingerprint
change is made.

The cases cover:

- owned domains and subdomains;
- wrong domains;
- homonyms and name-only matches;
- parent/product ambiguity;
- LLM-only and bare upstream labels;
- reproducible external provenance;
- conflicting identity signals;
- first-scan poisoning;
- domain transfer.

Calibration and test cases are separated in the frozen candidate data.

## Reviewed result — 2026-07-29

- reviewed: `14/14`;
- pending: `0`;
- critical false accepts: `0`;
- accepted precision: `1.0`;
- accepted recall: `1.0`;
- exact agreement: `1.0`;
- promotion blockers in this controlled set: none.

Identity policy v4 corrects the three former disagreements by requiring
reproducible evidence of another entity for `rejected`, preserving unresolved
identity as `disputed`, and resolving related domains before using them as
identity evidence. No human label, candidate, fingerprint, or threshold was
changed.

## Policy-v4 boundary regressions

Seven additional controlled cases live separately under:

```text
fixtures/evidence_identity_policy/v4/
  manifest.json
  boundary_cases.jsonl
```

They cover:

- unreproduced name-only identity;
- a sector difference without an identified alternative entity;
- an explicit different company;
- a verified parent with unresolved passage subject;
- a verified unrelated external domain;
- a verified product relation whose subject is exactly the scanned entity;
- an LLM-only conflict.

These are executable policy regressions, not new human gold decisions. The
original 14 reviews remain the sole reviewed set.

## Review workflow for a new dataset version

Generate an unsigned template outside the repository:

```bash
.venv/bin/python scripts/evidence_identity_gold_set.py \
  --write-review-template tmp/evidence-identity-reviews.jsonl
```

For every row, replace the `null` fields with:

- `decision`: `accepted`, `disputed`, or `rejected`;
- `reviewer_id`: the same stable identity configured in
  `B3S_EVIDENCE_REVIEWER_ID`;
- a concrete rationale;
- an ISO-8601 `reviewed_at` timestamp.

Evaluate the committed v1 reviews:

```bash
.venv/bin/python scripts/evidence_identity_gold_set.py \
  --format markdown
```

Use `--require-ready` in the controlled gate. It exits with status `2` when
those thresholds are unmet.

## Promotion thresholds

- every candidate reviewed;
- zero critical false accepts;
- accepted precision `1.0`;
- accepted recall at least `0.8`;
- exact agreement at least `0.8`.

These metrics have no runtime or scoring authority. The small controlled set is
a Gate 1 safety check, not evidence of general production accuracy. Real
reviewed cases and any future policy change require a new dataset/policy
version before production promotion.
