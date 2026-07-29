# Evidence Claim Slot Producer v1

Status: superseded by
[`Evidence Claim Slot Producer v2`](evidence_claim_slot_producer_v2.md).
This document records the original strict producer contract.

## Verdict

The producer gives future scans usable semantic claim history without changing
interpretation, scoring, report selection, or canonical state. It is a
conservative shadow extractor, not a general claim classifier.

## Correct integration boundary

Claim Memory rebuilds history from the persisted SV9 Flow candidate, not from
the research `EvidenceGraph`. The candidate contract therefore has a separate
field:

```text
candidate.claim_memory_evidence
```

These records are not appended to `candidate.evidence_pack.evidence`. The
evidence pack is finalized and consumed by labeling, shortlisting,
interpretation, coverage, tile signals, and SV9 ingress independently. The
producer runs after those products have been calculated and its records are
read only by the shadow evidence-memory projections.

`sv9-flow-candidate-v2` introduces this additive lane. Existing v1 reports
remain readable and simply contain no producer records.

## Accepted declarations

Version 1 emits only:

- `mission.primary`;
- `vision.primary`.

The source must be an `owned_copy` raw-input record and the declaration must
be explicit:

- an exact semantic Markdown heading such as `Our Mission`, `Nuestra misión`,
  `Our Vision`, or `Nuestra visión`, followed by a non-empty paragraph; or
- an explicit sentence such as `Our mission is ...` or
  `Nuestra visión es ...`.

The emitted content is a normalized verbatim excerpt. No LLM output becomes a
claim variant.

Without an explicit `entity_scope`, the source must be the homepage or an
unambiguous first-level corporate surface such as `/about`, `/company`,
`/mission`, `/vision`, or `/manifesto`. Product routes fail closed. A product
surface is eligible only when its upstream evidence record explicitly
declares an entity scope such as `product:treasury`.

The source URL must remain on the scanned brand domain or one of its
subdomains. External proof, values lists, audiences, offers, generic strategic
language, approximate headings, and inferred mission/vision language are not
slotted automatically.

## Identity boundary

The producer writes:

```text
claim_slot_key = mission.primary | vision.primary
claim_type     = mission | vision
entity_scope   = explicit upstream scope, otherwise brand domain at read time
```

The slot key depends only on the explicit semantic role. Claim text, URL,
source-reference position, and producer occurrence number are provenance, not
slot identity. Two different mission statements therefore remain two variants
of one mission slot. If observed together they produce a
`coexistence_candidate`; if observed in ordered reports they may produce a
`replacement_candidate`. Neither relation is accepted automatically.

Every producer record declares:

```text
shadow_only=true
runtime_effect=false
authority=false
```

## Fail-closed examples

| Evidence | Result |
|---|---|
| `# Our Mission` on `/about` | emit `mission.primary` |
| `Nuestra visión es ...` on homepage | emit `vision.primary` |
| mission-like product copy without an explicit label | ignore |
| `# Our Mission` on `/products/treasury` without scope | ignore |
| same product declaration with `entity_scope=product:treasury` | emit scoped mission |
| `# Our Values` | ignore in v1 |
| third-party article saying “Our mission is...” | ignore |
| `# Mission Control` | ignore |

## Historical replay

Reports created before this contract have no `claim_memory_evidence` lane.
Under the original v1 producer their replay remained at zero semantic slots.
Historical reuse is now handled by a separate versioned, non-mutating backfill;
it still does not derive identity from generic text, URL, visual tile,
`checked_block`, or EvidenceGraph's text-derived `claim_id`.

See
[`evidence_claim_historical_backfill_v1.md`](evidence_claim_historical_backfill_v1.md).
