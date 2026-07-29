# Evidence source review set v1

## Verdict

Source identity, publisher independence, and claim corroboration are different
questions. The v1 review set persists them separately and refuses to treat an
editorially independent article as global corroboration for every claim it
contains.

This dataset is an offline, human-reviewed calibration artifact. It has
`runtime_effect=false` and `authority=false`; it does not update the
source-independence registry, scoring, canonical claims, or production
selection.

## Frozen dataset

The versioned files live in:

```text
fixtures/evidence_source_review/v1/
  manifest.json
  candidates.jsonl
  review_events.jsonl
```

The frozen Vercel batch contains six production URLs:

| Case | Publisher | Identity | Publisher independence | Claim corroboration |
| --- | --- | --- | --- | --- |
| V1 | InfoQ | `accepted` | `confirmed_independent` | `mixed` |
| V2 | VentureBeat | `accepted` | `confirmed_independent` | `mixed` |
| V3 | The Register | `accepted` | `confirmed_independent` | `mixed` |
| V4 | SiliconANGLE | `accepted` | `confirmed_independent` | `mixed` |
| V5 | TechInformed | `accepted` | `confirmed_independent` | `disputed` |
| V6 | PR Newswire | `accepted` | `excluded` | `excluded` |

All six decisions are attributable to `reviewer_id=gsus`. The candidate and
event-log fingerprints are frozen in the manifest, so silent changes fail
validation.

## Two independence axes

`publisher_independence_decision` answers who controls and publishes the
piece:

- `confirmed_independent`
- `disputed`
- `excluded`

`claim_corroboration_decision` answers whether evidence independent of the
brand supports a specific claim:

- `independently_corroborated`
- `mixed`
- `disputed`
- `excluded`

The validator will not accept `independently_corroborated` without an explicit
`claim_id`. A source-aggregate `mixed` or `disputed` review with no `claim_id`
must declare `requires_claim_level_review=true`.

This means an interview, reported article, or critical analysis can pass the
publisher gate while remaining ineligible to corroborate product, adoption,
cost, or impact claims globally.

## Versioning and revocation

Review history is append-only per `case_id`:

```text
decision(sequence=1)
  → revocation(sequence=2, revoked_event_id=decision)
  → revised decision(sequence=3)
```

Every event records:

- schema and dataset versions;
- a unique `event_id`;
- sequence and `previous_event_id`;
- reviewer and ISO-8601 timestamp;
- axis-specific rationale;
- `runtime_effect=false`;
- `authority=false`.

A revocation must target the currently active decision. It removes that case
from the active review set without deleting or rewriting the original event.
Any revised frozen batch requires a new dataset version and updated
fingerprints.

## Current gate result

Run:

```bash
./.venv/bin/python scripts/evidence_source_review_set.py --format markdown
```

Current result:

- identity gate: ready, 6/6 accepted;
- publisher-independence gate: ready, 5 independent and 1 excluded;
- claim-corroboration gate: blocked;
- claim-scoped reviews: 0;
- source cases requiring claim-level review: 5;
- semantic-paraphrase recall: not measured;
- runtime and canonical authority: disabled.

The operational
`src/services/evidence_source_registry_v1.json` remains unchanged. Its
single-axis `confirmed_independent` decision cannot represent the distinction
above, so copying V1–V5 into it would overstate corroboration.

## Promotion boundary

The next dataset version must introduce claim-scoped candidates before any
corroboration promotion is considered. Each candidate needs a stable
`claim_id`, the exact supporting passage or locator, its upstream provenance,
and a human decision about whether that particular claim is independently
supported.

Even after claim review, promotion still requires measured semantic-paraphrase
recall and an operational contract that preserves both axes.
