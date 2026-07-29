# Evidence claim relation gold set v1

## Verdict

The repository now contains a frozen 13-case review set and a deterministic
evaluator for Claim Memory relation proposals. It measures false replacement,
missed replacement, replacement precision and replacement recall, but it is
not reviewed gold data yet.

The candidate `proposed_review` fields are drafting aids. They are never loaded
as human decisions and cannot make the dataset promotion-ready.

## Contents

```text
fixtures/evidence_claim_relation_gold/v1/
  manifest.json
  candidates.jsonl
```

The manifest pins the candidate fingerprint. A silent edit to any timeline,
claim, source report, case type, or review prompt invalidates evaluation.

The set contains:

- 10 controlled timelines covering repetition, sequential replacement,
  simultaneous coexistence, disappearance, source relocation, claim-type
  conflict, reacquisition, multi-step replacement, coexistence followed by
  narrowing, and simultaneous changes to several slots;
- 3 immutable-history projections for Liminal, Robin Capital, and Vercel.

The real-history cases reuse the report IDs and explicit claims recovered by
historical backfill. None contains an observed replacement.

## Review decisions

Each case accepts one human decision:

- `replacement`: a later variant semantically replaces an earlier variant in
  the same stable slot;
- `coexistence`: the variants are simultaneously valid;
- `no_relation`: the observations justify neither relation;
- `disputed`: the evidence cannot resolve the relation safely.

The deterministic prediction is derived only from Claim Memory v1:

- only replacement candidates → `replacement`;
- only coexistence candidates → `coexistence`;
- no relation candidates → `no_relation`;
- mixed relation types → `disputed`.

Prediction does not adjudicate the case.

## Single-reviewer workflow

Generate an unsigned template outside the repository:

```bash
.venv/bin/python scripts/evidence_claim_relation_gold_set.py \
  --write-review-template tmp/evidence-claim-relation-reviews.jsonl
```

Inspect the matching candidate timeline and replace every `null` field with:

- `decision`;
- the stable reviewer identity configured in
  `B3S_EVIDENCE_REVIEWER_ID`;
- a concrete rationale;
- an ISO-8601 `reviewed_at` timestamp.

Then evaluate:

```bash
.venv/bin/python scripts/evidence_claim_relation_gold_set.py \
  --reviews tmp/evidence-claim-relation-reviews.jsonl \
  --format markdown
```

`--require-ready` exits with status `2` while any gate remains blocked.

## Metrics and promotion boundary

The evaluator reports:

- critical and total false replacements;
- missed replacements;
- replacement precision and recall;
- exact decision agreement;
- reviewed real-history cases;
- reviewed real-history replacements.

Thresholds require complete review, zero critical false replacements,
replacement precision `1.0`, replacement recall at least `0.8`, exact
agreement at least `0.8`, five reviewed real-history cases, and three reviewed
real replacements.

The current dataset has only three real-history cases and zero real
replacements. Therefore even perfect agreement on every controlled case cannot
authorize promotion. New real cases require a new dataset version and
fingerprint.

Before review, the deterministic projection proposes 3 replacement cases, 2
coexistence cases, and 8 cases with no relation. These are model outputs, not
human labels.

All results remain:

```text
runtime_effect=false
authority=false
```

They cannot select a canonical claim, change a score, or alter a report.
