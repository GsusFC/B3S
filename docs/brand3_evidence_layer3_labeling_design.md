# B3S — Layer 3: semantic evidence labeling pass (design)

Status: **IMPLEMENTED — runtime advisory pass.** C1 + C2 landed first, and the
offline measurement gate found latent relevant evidence in blocks still resolving
to `insufficient_acquisition`. Runtime implementation is intentionally advisory:
it enriches evidence metadata, rescues semantically relevant records into
shortlists, and surfaces `counter_refs`; it does not score or directly flip a
block negative.

---

## What gap this closes

C1/C2 are deterministic and lexical. Two failure modes survive them:

- **False negatives (recall).** The block shortlist scores by keyword hits
  (`block_evidence_worker._score_record`); `score <= 0` excludes the record
  entirely (`block_evidence_worker.py:82`). A record that states the mission in
  synonyms or another language than the EN/ES calibration terms never reaches
  the interpreter. The copy is in the pack; the shortlist can't see it.
- **Unipolar evidence.** Evidence can only *support* (positive_evidence) or be
  *absent* (verified/probable_absent). There is no way for a record to *count
  against* a block. A negative verdict can only arise from absence, never from
  evidence that contradicts a claim. `_is_positive_ref`
  (`evidence_coverage.py:166`) counts any cited non-metadata ref as positive
  when `detected=true`.

The labeling pass makes evidence **bipolar** and gives the shortlist **semantic
recall**, without touching the deterministic layers (they remain the fallback).

## The one real fork — cost vs. reach (recommendation, not a blocking question)

Placement is forced by the code: `build_block_evidence_shortlists` runs at
`orchestrator.py:41`, *before* the interpretation LLM at `:46`. So:

- **Full design (recommended).** Labeling runs **before the shortlist** → it can
  feed both semantic recall (shortlist) and stance (coverage). Cost: **one extra
  cheap LLM call per scan**, batched over the whole pack.
- **Reduced design.** Stance-only, piggybacked on the existing interpretation
  call (post-shortlist) → zero extra calls, but **no recall fix** — the synonym/
  other-language false negatives survive.

Recommendation: **full design.** The stated goal is to attack *both* false
positives and false negatives; the reduced design leaves the FN half on the
table. The extra call is the cheap model already used for the flow
(`SV9_FLOW_MODEL` defaults to `LLM_CHEAP_MODEL`, `src/config.py:191`). Tradeoff
taken: +1 call, +one non-determinism surface (mitigated below).

## Architecture

New worker `src/sv9_flow/evidence_labeling_worker.py`, called from the
orchestrator between pack build and shortlist:

```
evidence_pack = build_evidence_pack_from_snapshot(...)          # orchestrator.py:37
evidence_pack = label_evidence_pack(evidence_pack, llm=labeling_llm)   # NEW
shortlists   = build_block_evidence_shortlists(evidence_pack)   # orchestrator.py:41
```

The pass **enriches records in place** (writes to `record.metadata`); it does not
change the `BrandEvidencePack` shape or the interpretation contract. Downstream
consumers read new metadata keys; if the pass didn't run, the keys are absent and
every consumer falls back to today's behavior.

### The `labeling_llm`, mirrored on `adjudicator_llm`

There is no batch/cache LLM infra in `sv9_flow` today (only `lru_cache` on the
calibration JSON, `calibration_terms.py:20`). The faithful pattern to copy is
`adjudicator_llm`:

- Threaded as an optional param through `build_flow_candidate`
  (`orchestrator.py:26`) exactly like `adjudicator_llm`.
- Constructed by callers as `LLMAnalyzer(model=labeling_model)` (see
  `scripts/sv9_flow_model_bakeoff.py:136`).
- Availability gate reuses the `_adjudicator_available` idiom:
  `bool(x is not None and getattr(x, "api_key", None))`
  (`interpretation_llm_worker.py:817`). Unavailable → skip the pass, keep
  deterministic behavior. **The pass is never on the critical path.**
- Model: new env `BRAND3_SV9_FLOW_LABELING_MODEL`, default `LLM_CHEAP_MODEL`.
- Call surface: `labeling_llm._call_json(system, user, json_schema=SCHEMA)` —
  same interface the interpretation and adjudicator workers use.

### Batching

One call per scan: send the whole pack (ref + trimmed content + current
source_class) as a numbered list, get back one label object per ref. Packs are
already bounded (dedup, shortlist caps upstream). If a pack ever exceeds a token
budget, chunk by N records and merge — but that is not MVP; MVP is single-call.

### Cache (explicit NEW infra — not assumed, not MVP)

A content-hash cache (`sha256(record.content)` → label) would make re-scans free
and reduce non-determinism, but **no such store exists today**. Mark it as
follow-up sub-work, not part of the first implementation. MVP recomputes.

## Output contract (per record)

```json
{
  "ref": "raw_inputs.3.exa.news.0",
  "relevant_blocks": ["mission", "values"],
  "stance": "supports | contradicts | neutral",
  "identity_match": "domain | brand_name | none | unverified",
  "specificity": "explicit | implied | incidental"
}
```

- `relevant_blocks`: subset of the canonical blocks the record actually speaks
  to — semantic, not keyword. Empty is valid.
- `stance`: relative to the brand's own claims. `contradicts` = the record works
  against the block (e.g. a review contradicting a stated value).
- `identity_match`: the LLM's semantic read of the SAME axis C1a labels
  lexically. Written under a distinct key `identity_match_llm` so it never
  overwrites C1a's deterministic value — divergence between the two is logged
  (see non-determinism). This is where a **true homonym** (different company,
  same name — which C1a cannot catch) finally gets flagged.
- `specificity`: is the mention explicit brand copy, implied, or incidental.

## The three consumption points (must be concrete or the pass is useless)

### 1. Shortlist — rescue the `score <= 0` exclusion (the FN fix)

`block_evidence_worker._score_record`: after the keyword scoring, add

```python
if block in (record.metadata.get("relevant_blocks") or []):
    score += _SEMANTIC_RELEVANCE_BONUS   # e.g. +3, one keyword-hit equivalent
```

This lifts a synonym/other-language record above the `score <= 0` cutoff so it
enters the shortlist. Bonus is additive and advisory — it never *demotes* a
keyword-matched record, it only *rescues* semantically-relevant ones.

### 2. Coverage — make evidence bipolar (the FP fix)

`evidence_coverage._is_positive_ref`: a `contradicts` ref must not count as
positive support:

```python
if record.metadata.get("stance") == "contradicts":
    return False
```

Then, in `block_coverage`, collect **`counter_refs`** = cited refs whose stance
is `contradicts`, and expose them in the block payload next to `positive_refs`
and `absence_refs`. Advisory power (see below): `counter_refs` are surfaced to
the interpreter and the report; they do **not** by themselves flip a status.
This is the concrete mechanism that turns "negative only by absence" into
"negative by contradiction."

### 3. Specificity — feed the existing `implied_not_explicit`, don't reinvent

`_is_implied_not_explicit` (`evidence_coverage.py:144`) already models "read off
owned copy but structurally weak." A block whose best supporting ref carries
`specificity == "implied"` reinforces that path. Wire specificity into that
existing predicate rather than adding a parallel status.

## Stance is advisory (design decision, justified)

Consistent with `gate_authority=warn`: the pass annotates the shortlist and
coverage, and the interpreter/gate/human decides. A mislabeled `contradicts`
must never silently suppress real support — so `counter_refs` are shown, not
subtracted. If, after measurement, advisory proves too weak, promoting stance to
a status-changing signal is a separate, deliberate decision — not a default.

## Non-determinism and fallback

- **Structured output** (`_call_json` with the schema) bounds the shape.
- **Deterministic fallback**: pass unavailable or errors → no metadata written →
  every consumer uses today's lexical behavior. Zero regression by construction.
- **Divergence logging**: when `identity_match_llm` disagrees with C1a's lexical
  `identity_match`, record it in the interpretation debug payload, mirroring
  `_gate_disagreements` (`interpretation_llm_worker.py:1045`). This is how we
  learn whether the pass earns its cost — and how true homonyms surface.

## Tests that would be required (when implemented)

- `label_evidence_pack` with a stub llm: enriches metadata, leaves pack shape
  intact; unavailable llm → pack unchanged (fallback).
- Shortlist: a record with zero keyword hits but `relevant_blocks=[block]`
  enters the shortlist; a keyword record is never demoted by the bonus.
- Coverage: a cited `contradicts` ref does not count as positive_evidence and
  appears in `counter_refs`; `detected=true` + only counter_refs behaves per the
  advisory rule (status unchanged, counter_refs surfaced).
- Divergence: C1a `brand_name` vs llm `none` → logged in debug.

## Decision gate (repeat, because it's the point)

Do not build this until C1 + C2 are in and measured. The metric that justifies
it: count blocks ending `insufficient_acquisition` whose pack contains a record
the labeling pass would have marked `relevant_blocks ∋ block`. If that count is
low, the deterministic layers already did the job and this pass is not worth its
call.
