# Visual capture resilience — 2026-08-02

## Scope

This change hardens the Visual Signature capture path without changing the
functional scanner's scoring contract. It is intended for the isolated Vault
deployment first; production remains on the existing capture behavior until a
field comparison is approved.

## Runtime behavior

- Playwright allocates a stable raw-viewport checkpoint before the child
  process starts. If structural capture exceeds the budget, the parent keeps
  the checkpoint only when it is a valid PNG.
- A recovered viewport is returned as `capture_recovery=raw_viewport_checkpoint`
  with `section_capture_status=timeout` and an explicit non-authoritative
  integrity note. The existing visual scoring path treats the limitation as
  unavailable visual analysis, so a partial artifact cannot silently create a
  positive score.
- Structural capture still derives its complete HTML section/segment plan. At
  runtime it captures at most five high-value segments (first viewport, hero,
  strategic labels, footer), preserves the complete plan in the manifest, and
  marks each segment with `selected_for_capture`.
- When no full-page master exists, sections fully contained in captured
  segments are persisted as `capture_source=page_segment`; the manifest is
  `partial`, never `complete`.
- The normalized evidence fingerprint includes recovery and section status, so
  a recovered/partial artifact cannot be confused with a complete capture of
  the same page.

## Guardrails

- No production route, database schema, scorer, or runtime authority flag is
  changed by this branch.
- The parent process validates the checkpoint with Pillow before returning it.
- The checkpoint and structural errors remain traceable in the screenshot
  diagnostic, Visual Signature shadow payload, and evidence contract.

## Verification

- Focused capture/evidence tests: 97 passed (including the wrapper used by the
  main scanner).
- Full repository suite before deployment: 2,409 passed, 5 skipped.
- Vault field validation is recorded below.

## Vault field validation

Deployment commit: `227c433c59ff26fca59a4b3e86c5a893ddff677a`.
Production was not targeted; its health SHA was not changed by this pilot.

| Site | Scan | Visual status | Planned segments | Captured segments | Captured sections | Result |
| --- | --- | --- | ---: | ---: | ---: | --- |
| SoccerSolver | `9ef936960a35` | usable / partial | 8 | 5 | 14/16 | bounded selection; no full-page claim |
| SigmaOS | `4dd7eabbf28a` | usable / partial | 14 | 5 | 9/16 | bounded selection; no full-page claim |

Both reports completed with an acquisition gate of `pass`, a usable first
viewport, and `selection_strategy=priority_first_viewport_and_strategic_labels`.
The raw viewport and the selected section artifacts are present in the Vault
volume; unselected sections remain explicitly uncaptured rather than being
represented as complete evidence. The recovery marker was not needed in these
two runs because Playwright returned before the outer budget; its timeout path
is covered by the local checkpoint test.

Acceptance checks:

1. **Passed locally:** a valid raw viewport is recovered after a structural
   timeout and marked partial/non-authoritative.
2. **Passed in Vault:** pages with more than five structural segments capture
   at most five and never claim a full-page master.
3. **Passed in Vault:** both captures retained usable first-fold evidence and
   persisted section crops from selected segments.
4. **Passed:** production remained untouched; only `b3s-vault` received the
   deployment.
