# Evidence identity gold set v1

## Verdict

The repository now contains a frozen 14-case candidate set and a deterministic
evaluation harness. It is not a reviewed gold set yet: no decision is
attributed to the configured reviewer until that reviewer creates
`reviews.jsonl`.

Candidate `proposed_review` fields are drafting aids only. They are never loaded
as human decisions and cannot make the dataset promotion-ready.

## Contents

```text
fixtures/evidence_memory_gold/v1/
  manifest.json
  candidates.jsonl
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

## Review workflow

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

Then evaluate it:

```bash
.venv/bin/python scripts/evidence_identity_gold_set.py \
  --reviews tmp/evidence-identity-reviews.jsonl \
  --format markdown
```

Use `--require-ready` in a promotion gate. It exits with status `2` while
reviews or thresholds are incomplete.

## Promotion thresholds

- every candidate reviewed;
- zero critical false accepts;
- accepted precision `1.0`;
- accepted recall at least `0.8`;
- exact agreement at least `0.8`.

These metrics have no runtime or scoring authority. The small controlled set is
a Gate 1 safety check, not evidence of general production accuracy. Real
reviewed cases must be added under a new dataset version before promotion.
