# B3S report view-model contract

Status: draft v0.1  
Scope: B3S `/report/{id}` UI, component drawer, markdown handoff  
Last updated: 2026-07-08

## Verdict

The report UI needs a deterministic view-model between `raw.sv9.result` and
`web/templates/report.html.j2`.

This view-model must not change scores, tile states, evidence, prompts, Phase
Zero, Phase One, Phase Two, or SV9 rubric behavior. Its job is only to decide
what the user sees first, what remains as detected basis in the drawer, and
what moves to the drawer as methodology/evidence.

The current report already contains most of the needed data:

- `message`: editorial diagnosis when available.
- `veredicto`: SV9 component-level evaluator reading.
- `detected_content`: what the scanner detected from evidence.
- `tile_profile`: tile/baldosa results.
- `evidence` and `block.refs`: evidence references.
- `acquisition_artifacts` and `acquisition_gate`: acquisition context.

The missing piece is a stable presentation contract.

## 1. Design principle

Each component card has three layers:

| Layer | UI role | User question | Source authority |
|---|---|---|---|
| Principal | Strategic diagnosis | What does this score mean? | `message`, then `veredicto` |
| Detected basis | Drawer support | What did Brand3 see? | `detected_content` / clean `resumen` |
| Drawer | Evidence and method | Why exactly? Show the machinery. | `tile_profile`, `evidence`, `block.refs`, acquisition context |

The visible card must stay compact: one main block maximum. `attributes` and
`values` may replace prose with a deterministic list of 3-5 terms/principles.
The detected basis belongs inside the drawer, not as a second paragraph in the
card.

The card must not open with technical fallback text such as:

- `Componente X detectado...`
- `Síntesis automática...` / `Sintesis automatica...`
- `N/10 baldosas encendidas...`

Those strings are debug/fallback material. They must not render as visible UI
copy in the report card or drawer. They may remain in view-model debug metadata
or markdown/export detail if needed for traceability.

## 2. ReportViewModel

```json
{
  "schema_version": "b3s_report_view_model_v0_1",
  "id": "string",
  "brand_name": "string",
  "url": "string",
  "lang": "es",
  "score": 64,
  "score_scale": 100,
  "reliability": {
    "status": "reliable | usable | shadow | broken | unknown",
    "canonical_status": "canonical | non_canonical | unknown",
    "reason_codes": ["string"]
  },
  "hero": {
    "score_label": "Brand3 Score",
    "most_painful_gap": {
      "key": "magnetism",
      "label": "Magnetism"
    },
    "immediate_margin": 8,
    "executive_reading": "string | null"
  },
  "components": [],
  "acquisition": {},
  "export": {},
  "raw_refs": {}
}
```

Rules:

- The report view-model is derived from persisted report JSON.
- Rendering a report must not call an LLM.
- Missing fields must degrade deterministically.
- Raw SV9 data remains available under the persisted report and markdown export.
- UI labels may be localized, but component keys remain canonical.

## 3. ComponentViewModel

```json
{
  "key": "value_proposition",
  "label": "Propuesta de valor",
  "score": 7,
  "scale": 10,
  "status": "scored",
  "confidence": "alta | media | baja | insuficiente | unknown",
  "card": {
    "primary": {
      "role": "diagnosis",
      "text": "string",
      "source": "message | veredicto | detected_content | fallback",
      "is_fallback": false
    },
    "support": {
      "role": "detected_basis | detected_terms | coherence_pattern | none",
      "text": "string | null",
      "items": ["string"],
      "source": "detected_content | resumen | derived | none",
      "is_fallback": false,
      "show_on_card": false
    },
    "meta": {
      "lit": 7,
      "off": 2,
      "blind": 1,
      "has_drawer": true
    }
  },
  "drawer": {
    "summary": {},
    "tile_summary": {},
    "off_tiles": [],
    "blind_spots": [],
    "evidence": [],
    "coverage": {},
    "debug": {}
  },
  "export": {}
}
```

## 4. Primary text selection

`card.primary.text` is selected deterministically:

1. Use `message` when present, Spanish, and non-empty.
2. Else use `veredicto` when present and not an automatic fallback.
3. Else use `detected_content` / clean `resumen` when present.
4. Else use the component `level_zero` fallback.
5. Else show no prose and mark the card as incomplete.

`primary.role` is usually `diagnosis`.

For `attributes` and `values`, the primary may be a compact term list when the
term extraction is available and stronger than prose.

## 5. Grey/support text selection

The detected-basis support answers: "what did the system detect?"

It must not be a second competing diagnosis. It should be the evidential basis
for the primary reading, and it renders in the drawer.

Selection:

1. Prefer clean `detected_content`.
2. Else use clean `resumen` only if it is not a generated technical fallback.
3. Else use component-specific derived terms when deterministic extraction is
   available.
4. Else hide the detected-basis area.

Forbidden in detected-basis support:

- `Componente {label} detectado...`
- `Síntesis automática...` / `Sintesis automatica...`
- tile count prose as the main sentence
- raw JSON, provider errors, long screenshot URLs

Tile counts belong in `card.meta` or the drawer.

## 6. Drawer contract

The drawer is the place for method and evidence.

```json
{
  "summary": {
    "primary_text": "string",
    "confidence": "string",
    "coverage_status": "positive_evidence | implied_not_explicit | verified_absent | probable_absent | insufficient_acquisition | unknown"
  },
  "detected_basis": {
    "role": "detected_basis | detected_terms | none",
    "text": "string | null",
    "items": ["string"],
    "source": "detected_content | resumen | none"
  },
  "tile_summary": {
    "lit": 7,
    "off": 2,
    "blind": 1,
    "scale": 10
  },
  "off_tiles": [
    {
      "id": "P3",
      "name": "string",
      "motivo": "string",
      "evidencia": "string",
      "contexto_requerido": ""
    }
  ],
  "blind_spots": [
    {
      "id": "P8",
      "name": "string",
      "motivo": "string",
      "contexto_requerido": "string"
    }
  ],
  "evaluator_verdict": "string | null",
  "evidence": [
    {
      "ref": "string",
      "url": "string",
      "snippet": "string",
      "source_class": "owned_copy | external_proof | visual_signal | acquisition_metadata | unknown"
    }
  ],
  "coverage": {
    "status": "string",
    "source_layers": ["string"]
  },
  "debug": {
    "raw_component_key": "string",
    "source_fields_used": ["message", "detected_content"]
  }
}
```

Drawer rules:

- Separate `off_tiles` from `blind_spots`.
- Off tiles are the work plan.
- Blind spots are context/evidence still missing.
- Evidence refs must be short, wrapped, and contained.
- Screenshot/acquisition URLs must not overflow layout.
- Technical fallback strings stay in debug metadata only; the B3S report UI
  does not render them visibly.

## 7. Component-specific contract

### 7.1 Propósito (`core_purpose`)

Question: why does the brand exist beyond the immediate product?

Primary:

- diagnosis of whether the brand has a real reason of existence or only
  functional utility.

Support:

- detected purpose-like statement or functional substitute.
- should distinguish `purpose` from `mission`.

Drawer:

- about/manifesto/founder/category-tension evidence.
- mark absent when evidence only describes functionality.

### 7.2 Magnetism (`magnetism`)

Question: what tension, hook, desire, or emotional charge makes the brand
memorable?

Primary:

- diagnosis of magnetic strength and limitation.

Support:

- detected hook, tension, phrase, desire, or memorable promise.

Drawer:

- memorability, specificity, tension, preference, gravity, belonging, proof.
- blind spots remain visible when owned hook evidence is missing.

### 7.3 Propuesta de valor (`value_proposition`)

Question: what does the brand offer, to whom, and what changes for that
audience?

Primary:

- diagnosis of offer clarity, audience clarity, differentiation, and proof.

Support:

- compact offer statement:
  - offer
  - audience
  - outcome
  - proof if present

Drawer:

- missing audience, missing outcome, missing proof, weak differentiation.

### 7.4 Personalidad / Arquetipo (`personality`)

Question: what character does the brand perform through tone, vocabulary,
visual semantics, and behavior?

Primary:

- diagnosis of distinctiveness and fit.

Support:

- detected personality profile, preferably as a short phrase plus optional
  traits.

Drawer:

- tone, vocabulary, visual signals, audience relationship, contradictions.
- archetypes are lenses, not final truth.

### 7.5 Idea de marca (`brand_idea`)

Question: what strategic concept organizes the brand's expression?

Primary:

- diagnosis of whether the brand has an organizing idea or only a product
  description.

Support:

- detected central idea, metaphor, verbal/visual concept, or category reframe.

Drawer:

- naming, hero concept, visual signature, repeated metaphors, category posture.
- when visual evidence is weak, keep that limitation visible.

### 7.6 Atributos (`attributes`)

Question: which observable qualities describe how the brand appears or behaves?

Primary:

- 3-5 observable qualities.
- These may be adjectives or compound descriptive traits.

Support:

- one short explanation of how those qualities are demonstrated.

Expected shape:

```json
{
  "items": ["segura", "agnóstica", "modular", "operativa"],
  "text": "La marca demuestra estos atributos mediante su arquitectura de endpoint único, su discurso de gobernanza y su promesa de integración sin dependencia de proveedor."
}
```

Rules:

- Do not use generic filler such as `innovadora`, `moderna`, `premium` unless
  evidence makes them specific.
- Attributes are not moral principles.

### 7.7 Valores (`values`)

Question: which principles does the brand declare or demonstrate?

Primary:

- 3-5 principles.
- They may be single words, compound phrases, or adjective-like formulations,
  but they must behave as principles, not mere descriptive attributes.

Support:

- one short explanation of whether the values are declared, performed, or
  inferred.

Expected shape:

```json
{
  "items": ["soberanía de datos", "independencia tecnológica", "control operativo"],
  "text": "Estos valores se ejecutan en la promesa de evitar lock-in, proteger datos y centralizar la gobernanza de IA."
}
```

Rules:

- Do not force values into adjectives if doing so collapses them into
  attributes.
- Declared values without operational proof should not inflate confidence.

### 7.8 Misión (`mission`)

Question: what does the brand do now?

Primary:

- diagnosis of the operating mandate and its limit.

Support:

- detected present-tense mandate.

Drawer:

- literal mission statement if available.
- product/service activity when mission is inferred.
- CTA-only evidence must not create mission.

### 7.9 Visión (`vision`)

Question: what future state or category change does the brand point toward?

Primary:

- diagnosis of future ambition and credibility.

Support:

- detected future-state statement.

Drawer:

- future language, category transformation, roadmap, ecosystem ambition.
- current product description alone is not enough.

### 7.10 Coherencia (`coherencia`)

Question: how well do all components reinforce each other?

Primary:

- transversal diagnosis of alignment and contradiction.

Support:

- coherence pattern detected across components.
- If no clean coherence pattern exists, hide support instead of showing a
  technical fallback.

Drawer:

- critical pairs:
  - Magnetism <-> Personality
  - Purpose <-> Mission / Vision
  - Value Proposition <-> Attributes / Values
  - Brand Idea <-> all other blocks
- contradictory components.
- blind spots affecting confidence.

## 8. Current-field mapping

| View-model field | Current source | Notes |
|---|---|---|
| `primary.text` | `component.message` | Preferred when present |
| `primary.text` fallback | `component.veredicto` | Use when no message |
| `support.text` | `component.detected_content` | Preferred detected basis |
| `support.text` fallback | `component.resumen` | Only if not technical fallback |
| `support.items` | future deterministic extraction | Needed for attributes/values |
| `tile_summary` | `component.tile_profile` | Count `ok`, `no`, `sin_evidencia` |
| `off_tiles` | `component.tiles` where `estado=no` | Work plan |
| `blind_spots` | `component.tiles` where `estado=sin_evidencia` | Context required |
| `evidence` | `component.evidence` + `component.block.refs` | Drawer only |
| `coverage.status` | `component.block.coverage_status` | Drawer chip |
| `executive_reading` | `raw.sv9.result.executive_reading` | Hero narrative |

## 9. Acquisition presentation

Report-level acquisition data remains separate from component scoring.

The view-model should expose:

```json
{
  "acquisition": {
    "state": "ok | warning | blocked | cancelled | degraded_approved",
    "warnings": [],
    "issues": [],
    "artifacts": [
      {
        "source": "screenshot_capture",
        "kind": "screenshot",
        "status": "captured",
        "provider": "playwright",
        "public_url": "/artifacts/screenshots/example.png",
        "display_label": "captura principal",
        "layout_safe_url": "string"
      }
    ],
    "metrics": {
      "owned_url_count": 4,
      "external_proof_count": 14,
      "external_attempt_count": 2,
      "absence_ref_count": 1,
      "evidence_record_count": 33
    }
  }
}
```

Rules:

- Acquisition warnings should be visible but not confused with component
  weakness.
- `blocked` visual evidence affects confidence/context, not score unless SV9
  scoring logic already did so.
- Long URLs and paths must be wrapped or replaced by short labels.
- Artifacts should be inspectable without breaking the report grid.

## 10. Export contract

Markdown export can remain more complete than the UI.

Rules:

- Markdown should keep component messages, verdicts, detected content, tile
  results, evidence, and blind spots.
- UI can hide technical fallback text from the card while markdown still exports
  it in the method/detail section.
- The view-model must not delete data from persisted reports.

## 11. Acceptance tests for implementation

When this contract is implemented, add tests for:

1. A component with `message` uses it as `card.primary.text`.
2. A component without `message` uses `veredicto` as `card.primary.text`.
3. Clean `detected_content` appears as drawer detected basis, not as card prose.
4. Detected basis never shows `Componente X detectado...`.
5. Detected basis never shows `Síntesis automática...` / `Sintesis automatica...`.
6. Coherencia hides support when only technical fallback exists.
7. Attributes expose 3-5 qualities in `support.items` or `primary.items`.
8. Values expose 3-5 principles in `support.items` or `primary.items`.
9. Drawer separates off tiles from blind spots.
10. Drawer contains evidence refs without overflowing long URLs.
11. Report rendering does not instantiate `LLMAnalyzer`.
12. Markdown export remains complete.

## 12. Non-goals

- No scoring changes.
- No prompt rewrite as part of view-model adoption.
- No new LLM call during report read.
- No mutation of `raw.sv9.result`.
- No Phase Zero / Phase One / Phase Two changes.
- No brand-specific exceptions.
