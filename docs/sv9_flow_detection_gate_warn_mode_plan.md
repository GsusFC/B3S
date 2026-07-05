# SV9 Flow — Detection Gate `warn` Mode (implementation plan)

Status: ready to implement
Scope: `src/sv9_flow/interpretation_llm_worker.py`, `src/sv9_flow/orchestrator.py`, `scripts/sv9_flow_sv9_shadow_eval.py`, `src/config.py`, tests. No web/UI changes.
Prod default does NOT change.

## 1. Context (current behavior — verify before coding)

Sensitive blocks: `SENSITIVE_BLOCKS = {"mission", "values", "vision", "magnetism"}`
(`src/sv9_flow/block_detection_worker.py:24`).

For a sensitive block with shortlist-valid refs (`policy_refs`), detection today is
(`src/sv9_flow/interpretation_llm_worker.py:308-315`):

- `use_detection_gates=False` → `detected = llm_detected and bool(refs)` (gate never runs).
- `use_detection_gates=True` → `detected = llm_detected and bool(refs) and (gate_supports or adjudicator_supports)`.

The deterministic gate is `resolve_block_detection(...)` (keyword lists from
`policy_data/calibration_terms.json`; the module docstring itself flags them as
overfit calibration data). Gate authority is **veto-only**: it can reject an LLM
detection, never grant one (comment at `interpretation_llm_worker.py:285-287`).

The adjudicator already exists (`_adjudicate_gate_rejection`,
`interpretation_llm_worker.py:784`): it runs only when *LLM detects + gate
rejects + adjudicator_llm configured*, and it validates a **literal quote**
copied from an allowed evidence record (`_adjudication_validation_error`
rejects with `quote_not_literal_substring` when `quote not in record.content`,
line 858). `supports_detection = state=="ok" and confidence in {medium,high}
and not validation_error`.

Provenance per block is built by `_detection_provenance`
(`interpretation_llm_worker.py:952`), with `final_source` values:
`llm_classified_evidence` (gates off), `llm_rejected_gate_candidate`,
`adjudicator_rescued_gate_rejection`, `llm_confirmed_by_gate`,
`llm_missing_evidence_refs`, `gate_rejected`, `llm`, `insufficient_evidence`.

Debug payload exposes `gate_authority: "veto_only" | "disabled"`
(`interpretation_llm_worker.py:202`).

Downstream dependency to preserve:
`src/sv9_flow/evidence_coverage.py:161` — `_is_implied_not_explicit` requires
`detection_provenance.final_source == "gate_rejected"` and `llm_detected is True`.

Plumbing today: `orchestrator.build_flow_candidate` and
`scripts/sv9_flow_sv9_shadow_eval.build_flow_sv9_shadow_eval` already thread
`adjudicator_llm` (default model `SV9_ADJUDICATOR_MODEL`, env
`BRAND3_SV9_ADJUDICATOR_MODEL`, see `src/config.py:196`). Neither threads
`use_detection_gates` — the flag is only reachable by calling the worker
directly.

## 2. Goal

Add a third gate authority mode, `warn`, in which the overfit keyword gate can
no longer veto an evidenced LLM detection by itself. The binding hard guard in
`warn` mode becomes the adjudicator's literal-quote verification. Every
LLM-vs-gate disagreement is recorded with cause and outcome so Scoring Lab can
compare modes and decide any future prod flip per block with data.

Explicitly NOT goals:
- Do not change the default mode (prod and current callers stay on veto).
- Do not remove or edit the keyword gates / `calibration_terms.json`.
- Do not touch the acquisition gate (`web/scan_runner.py`), evidence classes,
  or tile workers.
- No per-block authority overrides (single global mode; per-block comes later
  only if shadow data demands it).

## 3. Mode semantics

Replace the boolean with a single enum used everywhere (param value == debug
string; no mapping):

```python
GateAuthority = Literal["veto_only", "warn", "disabled"]
```

`use_detection_gates: bool` is removed; all internal callers and tests migrate
to `gate_authority: GateAuthority = "veto_only"` in the same PR
(True → `"veto_only"`, False → `"disabled"`). Grep confirms callers are only
the worker itself and tests.

Decision table for a **sensitive block with non-empty `policy_refs`**, given
`llm_detected=True` and valid `refs`:

| Case | veto_only (today, unchanged) | warn (new) | disabled (unchanged) |
|---|---|---|---|
| Gate agrees | detected · `llm_confirmed_by_gate` | same as veto | detected · `llm_classified_evidence` (gate never runs) |
| Gate disagrees, adjudicator verifies literal quote | detected · `adjudicator_rescued_gate_rejection` | same as veto | detected (adjudicator never runs) |
| Gate disagrees, adjudicator runs and does NOT support (explicit `no`/`sin_evidencia`, validation error, or failed/empty call) | not detected · `gate_rejected` | same as veto — the quote check is the hard guard | detected |
| Gate disagrees, adjudicator NOT configured (None or no `api_key`) | not detected · `gate_rejected` | **detected** · new `final_source="llm_unadjudicated_gate_disagreement"` · new limitation `{block}_gate_disagreement_unadjudicated` | detected |

Everything else is unchanged in all modes:
- `llm_detected` without valid refs → dropped (`{block}_dropped_missing_evidence_refs`).
- Gate-positive / LLM-negative → stays undetected, recorded as review-queue
  candidate (`{block}_gate_positive_llm_negative`, `llm_rejected_gate_candidate`).
- Non-sensitive blocks → `_detected_from_evidence_policy` path untouched.
- `rejected_content` behavior untouched.

Notes on the table:
- In `warn`, the gate ALWAYS runs for sensitive blocks with policy refs
  (same as veto) — that is the whole point: it produces the disagreement
  telemetry it no longer enforces. `resolve_block_detection` must be called in
  both `veto_only` and `warn` (today's guard at
  `interpretation_llm_worker.py:279-283` only checks the old bool).
- In `warn`, the adjudicator trigger condition is the same as today (LLM
  detects, gate disagrees, adjudicator configured). Its verdict is binding in
  both directions: rescue on support, drop on non-support. A failed/empty
  adjudicator call already coerces to a non-supporting verdict — keep that
  (fail-closed) and do not special-case transport errors.
- The only fail-open path is `warn` + adjudicator unconfigured, and it is
  flagged with its own limitation code so Scoring Lab can count it. This makes
  the adjudicator effectively required for meaningful `warn` runs — document
  that in the worker docstring.
- When `warn` drops a detection because the adjudicator did not support it,
  keep `final_source="gate_rejected"` and keep appending
  `decision.limitation_code` (e.g. `{block}_structural_gate_rejected`) exactly
  as veto does today. This keeps `evidence_coverage._is_implied_not_explicit`
  working with **zero changes** to `evidence_coverage.py`.

## 4. Implementation steps

### 4.1 `src/sv9_flow/interpretation_llm_worker.py`

1. Add `GateAuthority` literal + a small normalizer
   (`_gate_authority(value) -> GateAuthority`, unknown values fall back to
   `"veto_only"`).
2. `build_brand_interpretation_with_llm(..., gate_authority: GateAuthority = "veto_only")`
   — replaces `use_detection_gates`. Debug field stays `"gate_authority"` and
   now emits the enum value verbatim (existing strings `veto_only`/`disabled`
   are preserved; `warn` is new).
3. `normalize_llm_interpretation_response(..., gate_authority=...)`:
   - Compute `decision` when `gate_authority in {"veto_only", "warn"}` and
     block is sensitive with policy refs.
   - Detection resolution per the table in §3. Suggested shape:

     ```python
     gate_supports = bool(decision and decision.supports_detection)
     if gate_authority == "disabled":
         detected = llm_detected and bool(refs)
     elif key in SENSITIVE_BLOCKS and policy_refs:
         if gate_supports or adjudicator_supports:
             detected = llm_detected and bool(refs)
         elif gate_authority == "warn" and adjudication is None and not _adjudicator_available(adjudicator_llm):
             detected = llm_detected and bool(refs)   # unadjudicated fail-open, flagged
         else:
             detected = False
     else:
         detected = _detected_from_evidence_policy(...)
     ```

   - Append `f"{key}_gate_disagreement_unadjudicated"` on the fail-open path.
   - Keep every existing limitation append (`adjudicator_rescued/rejected`,
     `gate_positive_llm_negative`, `decision.limitation_code` when not
     detected, magnetism family limitations, shortlist drops) working in
     `warn` exactly as in `veto_only`.
4. `_detection_provenance(..., gate_authority=...)` — replace the
   `use_detection_gates` param. `gate_applied` is true for
   `{"veto_only", "warn"}`. Add one new `final_source` branch:
   detected + gate disagreed + no adjudication + warn →
   `"llm_unadjudicated_gate_disagreement"`. All existing values unchanged.
5. New debug key `gate_disagreements`: compact list built from the normalized
   blocks, one row per sensitive block where the gate ran, `llm_detected` was
   true and `gate_detected` false:

   ```json
   {"block": "mission", "gate_reason": "mission_structural_gate_rejected",
    "adjudicator_state": "ok|no|sin_evidencia|null",
    "adjudicator_validation_error": "",
    "final_detected": true, "final_source": "adjudicator_rescued_gate_rejection"}
   ```

   This gives Scoring Lab countable rows without parsing limitation strings.

### 4.2 `src/config.py`

```python
SV9_FLOW_GATE_AUTHORITY = os.environ.get("BRAND3_SV9_FLOW_GATE_AUTHORITY", "veto_only")
```

Place it next to `SV9_ADJUDICATOR_MODEL`. Invalid values are normalized to
`veto_only` by the worker; config does not validate.

### 4.3 `src/sv9_flow/orchestrator.py`

`build_flow_candidate(..., gate_authority: str | None = None)` → forward to
`build_brand_interpretation_with_llm` (None → config default; import from
`src.config` inside the function like the existing pattern, or default at the
shadow-eval layer — follow whichever pattern `adjudicator_llm` already uses).

### 4.4 `scripts/sv9_flow_sv9_shadow_eval.py`

`build_flow_sv9_shadow_eval(..., gate_authority: str | None = None)`; default
resolves to `SV9_FLOW_GATE_AUTHORITY`. Forward to `build_flow_candidate`. This
makes the web lab (`web/scan_runner.py` calls this builder) and shadow batches
switchable via env without further changes.

## 5. Tests (`tests/test_sv9_flow_interpretation_llm_worker.py` + coverage suite)

Use the existing test helpers/fakes in that file (fake LLM + fake adjudicator
already exist from the adjudicator work). Add, for a sensitive block (mission)
whose evidence deliberately contains none of the calibration keywords:

1. `warn` + gate disagrees + adjudicator returns valid literal quote →
   `detected=True`, `final_source="adjudicator_rescued_gate_rejection"`,
   limitation `mission_adjudicator_rescued_gate_rejection`.
2. `warn` + gate disagrees + adjudicator rejects (or returns non-substring
   quote) → `detected=False`, `final_source="gate_rejected"`, limitations
   include `mission_adjudicator_rejected_gate_rejection` and
   `mission_structural_gate_rejected`.
3. `warn` + gate disagrees + `adjudicator_llm=None` → `detected=True`,
   `final_source="llm_unadjudicated_gate_disagreement"`, limitation
   `mission_gate_disagreement_unadjudicated`.
4. `warn` + gate agrees → `detected=True`, `final_source="llm_confirmed_by_gate"`.
5. `warn` + gate positive + LLM negative → undetected,
   `review_queue_reason="gate_positive_llm_negative"` (unchanged from veto).
6. Regression: `veto_only` and `disabled` reproduce today's behavior on the
   same fixtures (reuse/parametrize existing tests migrated off
   `use_detection_gates`).
7. Coverage integration: block from case 2 classifies as
   `implied_not_explicit`/`verified_absent` per existing
   `evidence_coverage.block_coverage` rules — proving no coverage change was
   needed.
8. Debug: `gate_disagreements` contains exactly the disagreement rows for
   cases 1-3 and is empty when the gate agrees; `debug["gate_authority"] == "warn"`.

## 6. Acceptance criteria

- `make lint` clean.
- `./.venv/bin/python -m pytest tests/test_sv9_flow_interpretation_llm_worker.py tests/test_sv9_flow_contracts.py tests/test_sv9_flow_block_evidence_worker.py -q` green.
- `make test` green (no regressions elsewhere; `use_detection_gates` no longer
  referenced anywhere: `grep -rn use_detection_gates src scripts web tests`
  returns nothing).
- With no env var set, a run through `build_flow_sv9_shadow_eval` behaves
  byte-identically to today for detection outcomes (default `veto_only`).
- `BRAND3_SV9_FLOW_GATE_AUTHORITY=warn` flips the mode with no code changes.

## 7. Rollout (context for the reviewer, not code)

- Prod / default: `veto_only`, untouched.
- Shadow batches move from "gates fully disabled" to `warn`: disabled runs
  lose the disagreement telemetry (`resolve_block_detection` never executes),
  which is exactly the data needed to decide any prod flip. `warn` produces
  score deltas AND per-block cause.
- Decision to flip prod (if ever) happens later, per block, from
  `gate_disagreements` + limitation counts in Scoring Lab
  (`web/scoring_store.record_report` already persists reports for counting).
  Mission/vision/values are declarative and expected to flip first; magnetism
  waits for its own numbers — its deep tiles (MG3–MG10) keep their independent
  blind-tile protections regardless of mode.
