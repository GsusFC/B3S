# B3S — Acquisition signal fixes after the Toteemi run (implementation plan)

Status: ready to implement
Motivating evidence: `data/reports/a9dadd8a27bf.json` (Toteemi, first full real
scan through the SV9 flow with `gate_authority=warn`). The interpretation/gate
machinery worked as designed; three acquisition-layer signal problems surfaced.
Each workstream below is an independent PR. Order: WS1 → WS3 → WS2.

Out of scope for all three: the SearchAPI fallback logic
(`src/services/input_collection_external_sources.py:243-270`) — verified
correct in the Toteemi run (its eligible intents all had results, so skipping
was right). Do not change detection gates, warn mode, or scoring.

---

## WS1 — Exa step status must not report "empty" when external proof exists

### Problem (verified)

In the Toteemi run Exa produced 26 useful evidence records (4 `news`,
9 `external_mentions`, 3 `ai_visibility`, 10 `owned_confirmation`); only the
non-critical `external_profiles` intent returned zero results. Yet:

- `src/services/input_collection_exa.py:112-131` labels the whole step
  `status="empty"` whenever `no_result_intents` is non-empty — regardless of
  what the other intents returned.
- `web/scan_runner.py` (`_build_acquisition_gate`, the
  `exa_status in {"empty", "partial"}` branch) propagates that label as a
  gate warning "Exa returned limited external proof", with no intent detail.

Result: the UI/report said "exa: empty" while 16 external-proof records sat in
the evidence pack. This produced a wrong human diagnosis in the first real
test. It is a signal-integrity bug.

### Change

1. `src/services/input_collection_exa.py` — status resolution becomes:
   - `failed_intents` non-empty → `partial` (unchanged).
   - else ALL external-proof intents (`news`, `external_mentions`,
     `ai_visibility`) returned zero results → `empty`. Reuse the exact
     semantics of `_exa_external_proof_empty`
     (`src/services/input_collection_external_sources.py:258-270`); if that
     helper can be imported without a dependency cycle, import it — otherwise
     extract it to a shared module rather than duplicating the rule.
   - else → `ok`, with `details={"no_result_intents": [...]}` preserved so the
     gap stays visible.
   This also keeps the gate consistent with the SearchAPI fallback layer,
   which already uses the all-external-empty rule — today the two layers can
   contradict each other (step says "empty", fallback says "nothing eligible").
2. `web/scan_runner.py` gate branch for exa:
   - Warning only when status is `empty`/`partial` under the new semantics.
   - Include the affected intents in the warning `detail` (read
     `no_result_intents` / `failed_intents` from the step payload), e.g.
     `detail: "no_result_intents: external_profiles"`. The message must let a
     human distinguish "no external proof at all" from "one intent came back
     empty".

### Tests

- Extend `tests/test_input_collection.py` (Exa status matrix):
  a) only `external_profiles` empty → `status="ok"` + `no_result_intents` in
  details (Toteemi regression); b) all of news/mentions/ai_visibility empty →
  `status="empty"`; c) `failed_intents` non-empty → `partial` (unchanged).
- Extend the gate tests in `tests/test_web_lab.py`: case (a) produces NO exa
  warning; case (b) produces the warning with intent detail.

---

## WS2 — Visual capture: dismiss consent overlays, persist the blocked cause

### Problem (verified)

- Toteemi's visual acquisition completed but the visual evidence packet came
  back `blocked` (gate warning detail: `visual_evidence_packet:blocked`) —
  almost certainly the consent overlay ("Valoramos tu privacidad"): the
  obstruction heuristics already match consent/cookie patterns
  (`src/visual_signature/_internal/viewport_obstruction_patterns.py:11,40`).
- The TEXT capture path already dismisses cookie banners
  (`src/collectors/web_collector_capture_runtime.py:59,103`
  `_dismiss_cookie_banners`) — which is why the captured copy is clean (zero
  records contain the banner text). The VISUAL capture path
  (`src/visual_signature/_internal/playwright_capture_helpers_capture_runtime.py`)
  does not.
- The blocked cause is not persisted: the report only carries the bare string
  `visual_evidence_packet:blocked`. The cookie-banner suspicion shown live in
  the scan UI does not survive into the report at all (grep for
  cookie/privacidad in `data/reports/a9dadd8a27bf.json` → zero hits).

### Change

1. Visual capture runtime: before the screenshot, attempt consent dismissal
   reusing the same approach (and ideally the same selector list — extract it
   to a shared constant if imports allow) as
   `web_collector_capture_runtime._dismiss_cookie_banners`. If the obstruction
   heuristics still flag the frame after dismissal, re-capture once, then keep
   whatever the heuristics decide. No retry loops beyond one.
2. Persist cause end-to-end:
   - When the visual evidence packet is blocked/not_interpretable, include the
     obstruction reason in the visual acquisition step details: pattern kind
     plus a sampled overlay text snippet (≤80 chars), e.g.
     `blocked_reason: "consent_overlay: Valoramos tu privacidad"`. Also record
     whether dismissal was attempted and whether a re-capture happened.
   - `web/scan_runner.py` gate: surface that `blocked_reason` in the
     visual warning `detail`.
   - Web capture step: when the text extractor detects/strips a probable
     consent banner, add `cookie_banner_suspected: true` (plus the snippet) to
     the web step details so it reaches the gate detail and therefore the
     saved report — what the scan UI shows must survive into the report.
3. Do NOT touch the obstruction heuristics' verdict logic — capture hygiene
   and cause reporting only.

### Tests

- `make test-visual` suite: fixture page with a consent overlay → dismissal
  attempted, one re-capture, `blocked_reason` present when still obstructed.
- `tests/test_web_lab.py`: a snapshot whose visual step carries
  `blocked_reason` produces a gate warning whose detail includes it, and the
  saved report JSON contains it.

---

## WS3 — Strategic-surface detection misses Spanish about pages → absence checks never run

### Problem (verified)

Toteemi captured 17 owned URLs including `/sobre-toteemi/` (its about page),
yet the report has `absence_ref_count: 0` — so `values`/`vision` coverage can
only say "insufficient acquisition", never "verified absent".

Cause: `_absence_records_for_owned_page` only runs on URLs matching
`_ABSENCE_SURFACE_MARKERS` (`src/sv9_flow/evidence_worker.py:23-36`,
gate function `_is_strategic_surface` at line 778). The list has partial
Spanish coverage (`nosotros`, `empleo`, `cultura`, `valores`) but nothing
matches `sobre-toteemi`.

### Change

1. Extend `_ABSENCE_SURFACE_MARKERS` conservatively with common Spanish about
   slugs: `"sobre-"`, `"quienes"`, `"filosofia"`, `"manifiesto"`.
   - Use `"sobre-"` (with hyphen), not `"sobre"`, to avoid matching unrelated
     slugs.
   - Deliberately do NOT add `"mision"`: Spanish product/gamification pages
     use it generically (Toteemi's own `/misiones/` is a product feature), and
     a false strategic surface degrades coverage semantics — an absence record
     from a non-strategic page can flip a block from
     `insufficient_acquisition` to `verified_absent`, which is a stronger
     claim than the evidence supports. Leave a short comment stating this
     constraint next to the tuple.
2. When owned capture finishes with ZERO strategic surfaces matched across all
   owned URLs, emit one pack-level attempt record so the gap is explicit
   instead of silent:
   `evidence_type="acquisition.attempt.strategic_surfaces"`, `status="none_found"`,
   metadata listing the crawled URL count. Reuse `_acquisition_attempt_record`
   (`src/sv9_flow/evidence_worker.py:783`) if its shape fits; otherwise build
   the record inline with `source_class: "acquisition_metadata"`. It will
   appear in `coverage_acquisition.external_attempts` automatically
   (`src/sv9_flow/evidence_coverage.py:54`) — verify and accept that placement
   or extend `_attempt_summary` labeling minimally; no other coverage changes.

### Tests

Extend `tests/test_sv9_flow_evidence_worker.py`:
- URL matrix for `_is_strategic_surface`: `/sobre-toteemi/` → True,
  `/quienes-somos/` → True, `/misiones/` → False, `/categoria-producto/x` →
  False, existing English/Spanish markers still True.
- A snapshot fixture with a `/sobre-...` page lacking values/vision terms
  emits `acquisition.absence.values` and `acquisition.absence.vision` records.
- A snapshot with no strategic surfaces emits the single
  `acquisition.attempt.strategic_surfaces` record with `status="none_found"`.

---

## Acceptance (all workstreams)

- `make lint` clean.
- Targeted suites green:
  `./.venv/bin/python -m pytest tests/test_input_collection.py tests/test_input_collection_external_sources.py tests/test_web_lab.py tests/test_sv9_flow_evidence_worker.py -q`
  plus `make test-visual` for WS2.
- `make test` green.
- Manual check for WS1: rebuild the gate from the Toteemi snapshot diagnostics
  (only `external_profiles` empty) → exa step reports `ok` with the intent
  gap in details and the acquisition gate shows no exa warning.
