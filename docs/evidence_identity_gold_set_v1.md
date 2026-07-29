# Evidence identity gold set v1

## Verdict

The repository contains a frozen 14-case set, 14 attributable human reviews,
and a deterministic evaluation harness. Review is complete, but the identity
policy is not promotion-ready: exact agreement is `0.785714`, below the frozen
`0.8` threshold.

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
- exact agreement: `0.785714`;
- blocker: `exact_agreement_below_threshold`.

The reviewer disagreed conservatively on disputed versus rejected cases. No
non-accepted case was falsely accepted. The threshold must not be weakened to
make the result pass; the next step is a versioned policy revision evaluated
against the unchanged human labels.

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

Use `--require-ready` in a promotion gate. It exits with status `2` while
thresholds are unmet.

## Promotion thresholds

- every candidate reviewed;
- zero critical false accepts;
- accepted precision `1.0`;
- accepted recall at least `0.8`;
- exact agreement at least `0.8`.

These metrics have no runtime or scoring authority. The small controlled set is
a Gate 1 safety check, not evidence of general production accuracy. Real
reviewed cases and any policy change require a new dataset/policy version
before promotion.
