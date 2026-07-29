# Evidence Claim Slot Producer v2

## Verdict

Producer v2 extracts a narrow set of explicit mission and vision declarations
for shadow Claim Memory. It is not a general semantic classifier and it never
feeds interpretation, scoring, tiles, report selection, or canonical state.

## Inputs and accepted sources

The producer reads the finalized `BrandEvidencePack` but writes only to:

```text
candidate.claim_memory_evidence
```

It accepts:

- `owned_copy` raw-input records from the scanned domain;
- same-domain `external_proof.owned_confirmation` records only when their
  deterministic `intent=owned_confirmation` and `identity_match=domain`.

The second case permits reuse of an owned page captured through Exa. It does
not admit third-party evidence.

Without explicit `entity_scope`, the URL must be the homepage or an
unambiguous corporate surface such as `/about`, `/company`, `/mission`,
`/vision`, or `/manifesto`. Locale-prefixed equivalents are accepted. Product
routes require an upstream entity scope and otherwise fail closed.

## Explicit declaration grammar

Only `mission.primary` and `vision.primary` are emitted. Accepted forms include:

- exact semantic headings followed by a paragraph;
- semantic heading statements such as `Our Mission to ...`;
- explicit clauses such as `our mission is ...`, `our mission of ...`,
  `nuestra misión es ...`, and the equivalent vision or Portuguese forms.

The clause may follow a brand prefix, as in
`At Example, our mission is ...`. Generic strategic language, `values`,
audiences, offers, `Mission Control`, and third-party descriptions remain
ineligible.

Output content is a normalized verbatim excerpt. No LLM interpretation is
stored as a claim variant.

## Identity and deduplication

The stable key uses only the semantic role:

```text
claim_slot_key = mission.primary | vision.primary
```

Text, URL, source ref, and ordinal remain provenance. Duplicate declarations
with the same role, canonical URL, and normalized content collapse within one
candidate, including web/Exa copies of the same owned page.

Every record pins:

- `claim_slot_producer_version=evidence-claim-slot-producer-v2`;
- `claim_slot_derivation_mode`;
- source candidate schema version;
- original evidence ref;
- `runtime_effect=false`;
- `authority=false`.

Confidence never exceeds the confidence of the source record.

## Historical compatibility

Persisted `sv9-flow-candidate-v2` lanes always take precedence, including an
explicitly empty lane. Missing or malformed v2 lanes are not regenerated.
Only immutable `sv9-flow-candidate-v1` packs are eligible for the separate
versioned historical backfill.

See
[`evidence_claim_historical_backfill_v1.md`](evidence_claim_historical_backfill_v1.md).
